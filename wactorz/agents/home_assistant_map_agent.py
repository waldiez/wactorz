from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from ..config import CONFIG
from ..core.actor import Actor, ActorState, Message, MessageType
from ..core.integrations.home_assistant.ha_helper import (
    fetch_devices_entities_with_location,
    normalize_ha_ws_url,
)
from ..core.integrations.home_assistant.ha_web_socket_client import HAWebSocketClient

logger = logging.getLogger(__name__)

ENTITY_REGISTRY_UPDATED_EVENT = "entity_registry_updated"
DEFAULT_OUTPUT_TOPIC = "homeassistant/map/entities_with_location"


class MapUpdateDispatcher:
    """Small delivery abstraction so the final destination can change later."""

    def __init__(
        self,
        agent: Actor,
        mqtt_topic: str | None = None,
        target_actor_name: str | None = None,
    ) -> None:
        self._agent = agent
        self._mqtt_topic = (mqtt_topic or "").strip()
        self._target_actor_name = (target_actor_name or "").strip()

    async def dispatch(self, payload: dict[str, Any]) -> None:
        if self._target_actor_name and self._agent._registry is not None:
            target = self._agent._registry.find_by_name(self._target_actor_name)
            if target is not None:
                await self._agent.send(target.actor_id, MessageType.TASK, payload)
                return
            logger.warning(
                "[%s] Target actor '%s' not found. Falling back to MQTT.",
                self._agent.name,
                self._target_actor_name,
            )

        if self._mqtt_topic:
            await self._agent._mqtt_publish(self._mqtt_topic, payload)
            return

        logger.info("[%s] No output configured; dropping payload.", self._agent.name)


class HomeAssistantMapAgent(Actor):
    """Listens for Home Assistant entity registry updates and republishes device maps."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "home-assistant-map-agent")
        super().__init__(**kwargs)
        self.protected = False
        self.ha_url = (os.getenv("HOME_ASSISTANT_URL") or CONFIG.ha_url or "").rstrip("/")
        self.ha_ws_url = normalize_ha_ws_url(self.ha_url)
        self.ha_token = (os.getenv("HOME_ASSISTANT_TOKEN") or CONFIG.ha_token or "").strip()
        self._output_topic = (
            os.getenv("HA_MAP_AGENT_OUTPUT_TOPIC")
            or os.getenv("HOME_ASSISTANT_MAP_OUTPUT_TOPIC")
            or DEFAULT_OUTPUT_TOPIC
        ).strip()
        self._target_actor_name = (
            os.getenv("HA_MAP_AGENT_TARGET_ACTOR")
            or os.getenv("HOME_ASSISTANT_MAP_TARGET_ACTOR")
            or ""
        ).strip()
        self._dispatcher = MapUpdateDispatcher(
            agent=self,
            mqtt_topic=self._output_topic,
            target_actor_name=self._target_actor_name,
        )
        self._events_seen = 0
        self._last_event_at = 0.0
        self._last_error = ""

    async def on_start(self) -> None:
        self._events_seen = int(self.recall("events_seen", 0))
        self._last_event_at = float(self.recall("last_event_at", 0.0))
        self._last_error = str(self.recall("last_error", ""))
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {
                "agentId": self.actor_id,
                "agentName": self.name,
                "agentType": "home-assistant-map",
                "timestamp": time.time(),
            },
        )
        self._tasks.append(asyncio.create_task(self._entity_registry_listener()))
        logger.info("[%s] started", self.name)

    async def on_stop(self) -> None:
        self.persist("events_seen", self._events_seen)
        self.persist("last_event_at", self._last_event_at)
        self.persist("last_error", self._last_error)

    async def handle_message(self, msg: Message) -> None:
        if msg.type != MessageType.TASK:
            return

        command = self._extract_command(msg.payload)
        if command == "status":
            payload = self._build_status_payload()
        elif command == "refresh":
            payload = await self._build_map_update_payload(event=None)
            self.metrics.tasks_completed += 1
        else:
            payload = {
                "error": "Unsupported command. Use 'status' or 'refresh'.",
                "supported_commands": ["status", "refresh"],
            }
            self.metrics.tasks_failed += 1

        if msg.sender_id:
            await self.send(msg.sender_id, MessageType.RESULT, payload)

    def _extract_command(self, payload: Any) -> str:
        if isinstance(payload, dict):
            text = payload.get("text") or payload.get("task") or payload.get("command") or ""
        else:
            text = payload
        return str(text or "").strip().lower()

    async def _entity_registry_listener(self) -> None:
        if not self.ha_url or not self.ha_ws_url or not self.ha_token:
            self._last_error = "HA_URL/HOME_ASSISTANT_URL or HA_TOKEN/HOME_ASSISTANT_TOKEN is not configured"
            logger.warning("[%s] %s", self.name, self._last_error)
            return

        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                async with HAWebSocketClient(self.ha_ws_url, self.ha_token) as ha:
                    subscription_id = await ha.subscribe_events(ENTITY_REGISTRY_UPDATED_EVENT)
                    self._last_error = ""
                    logger.info(
                        "[%s] subscribed to Home Assistant '%s' events",
                        self.name,
                        ENTITY_REGISTRY_UPDATED_EVENT,
                    )

                    while self.state not in (ActorState.STOPPED, ActorState.FAILED):
                        event_message = await ha.receive_event(subscription_id)
                        await self._handle_entity_registry_event(event_message)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("[%s] listener error: %s", self.name, exc)
                if self.state not in (ActorState.STOPPED, ActorState.FAILED):
                    await asyncio.sleep(5)

    async def _handle_entity_registry_event(self, event_message: dict[str, Any]) -> None:
        payload = await self._build_map_update_payload(event_message.get("event"))
        await self._dispatcher.dispatch(payload)
        self._events_seen += 1
        self._last_event_at = payload["timestamp"]
        self.metrics.tasks_completed += 1

    async def _build_map_update_payload(self, event: dict[str, Any] | None) -> dict[str, Any]:
        devices = await fetch_devices_entities_with_location(
            self.ha_url,
            self.ha_token,
            include_states=True,
        )
        return {
            "type": "home_assistant_map_update",
            "event_type": ENTITY_REGISTRY_UPDATED_EVENT,
            "timestamp": time.time(),
            "event": event or {},
            "devices": devices,
        }

    def _build_status_payload(self) -> dict[str, Any]:
        return {
            "configured": bool(self.ha_url and self.ha_token),
            "event_type": ENTITY_REGISTRY_UPDATED_EVENT,
            "events_seen": self._events_seen,
            "last_event_at": self._last_event_at,
            "last_error": self._last_error,
            "output_topic": self._output_topic,
            "target_actor_name": self._target_actor_name,
        }

    def _current_task_description(self) -> str:
        if self._last_error:
            return f"waiting for HA events (error: {self._last_error})"
        return f"watching {ENTITY_REGISTRY_UPDATED_EVENT} ({self._events_seen} seen)"
