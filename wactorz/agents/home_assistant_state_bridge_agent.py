from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from ..config import CONFIG
from ..core.actor import Actor, ActorState, Message, MessageType
from ..core.integrations.home_assistant.ha_helper import normalize_ha_ws_url
from ..core.integrations.home_assistant.ha_web_socket_client import HAWebSocketClient

logger = logging.getLogger(__name__)

STATE_CHANGED_EVENT = "state_changed"
DEFAULT_OUTPUT_TOPIC = "homeassistant/state_changes"


def _parse_domains(raw: str) -> set[str]:
    """Parse a comma-separated domain filter string into a set of lowercased domain names."""
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


class HomeAssistantStateBridgeAgent(Actor):
    """Subscribes to Home Assistant state_changed events and publishes them to MQTT.

    Configuration (environment variables):
        HA_URL / HOME_ASSISTANT_URL          — Home Assistant base URL
        HA_TOKEN / HOME_ASSISTANT_TOKEN      — Long-lived access token
        HA_STATE_BRIDGE_OUTPUT_TOPIC         — Base MQTT topic (default: homeassistant/state_changes)
        HA_STATE_BRIDGE_DOMAINS              — Comma-separated domain allow-list
                                               (e.g. "light,switch,sensor"). Empty = all domains.
        HA_STATE_BRIDGE_PER_ENTITY           — "1" (default) publishes to
                                               {base_topic}/{domain}/{entity_id};
                                               "0" sends all events to {base_topic}.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "home-assistant-state-bridge")
        super().__init__(**kwargs)
        self.protected = False

        self.ha_url = (os.getenv("HOME_ASSISTANT_URL") or CONFIG.ha_url or "").rstrip("/")
        self.ha_ws_url = normalize_ha_ws_url(self.ha_url)
        self.ha_token = (os.getenv("HOME_ASSISTANT_TOKEN") or CONFIG.ha_token or "").strip()

        self._output_topic = (
            os.getenv("HA_STATE_BRIDGE_OUTPUT_TOPIC")
            or CONFIG.ha_state_bridge_output_topic
            or DEFAULT_OUTPUT_TOPIC
        ).strip()

        _raw_domains = (
            os.getenv("HA_STATE_BRIDGE_DOMAINS")
            or CONFIG.ha_state_bridge_domains
            or ""
        )
        self._domain_filter: set[str] = _parse_domains(_raw_domains)

        _per_entity_raw = os.getenv("HA_STATE_BRIDGE_PER_ENTITY")
        self._per_entity_topics: bool = (
            _per_entity_raw.strip() not in ("0", "false", "no")
            if _per_entity_raw is not None
            else CONFIG.ha_state_bridge_per_entity
        )

        self._events_seen: int = 0
        self._last_event_at: float = 0.0
        self._last_error: str = ""

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_start(self) -> None:
        self._events_seen = int(self.recall("events_seen", 0))
        self._last_event_at = float(self.recall("last_event_at", 0.0))
        self._last_error = str(self.recall("last_error", ""))

        await self.publish_manifest(
            description="Bridges Home Assistant state_changed events to MQTT topics.",
            capabilities=["ha_state_bridge", "mqtt_publisher"],
            publishes=[self._output_topic],
            output_schema={
                "type": "str — always 'home_assistant_state_change'",
                "entity_id": "str — full entity ID (domain.name)",
                "domain": "str — entity domain (light, sensor, switch, …)",
                "new_state": "object — new HA state object",
                "old_state": "object|null — previous HA state object",
                "context": "object — HA event context",
                "timestamp": "float — unix epoch when the event was received",
            },
        )
        self._tasks.append(asyncio.create_task(self._state_change_listener()))
        logger.info(
            "[%s] started (domain_filter=%r, per_entity_topics=%s)",
            self.name,
            self._domain_filter,
            self._per_entity_topics,
        )

    async def on_stop(self) -> None:
        self.persist("events_seen", self._events_seen)
        self.persist("last_event_at", self._last_event_at)
        self.persist("last_error", self._last_error)

    # ── Message handling ───────────────────────────────────────────────────────

    async def handle_message(self, msg: Message) -> None:
        if msg.type != MessageType.TASK:
            return

        command = self._extract_command(msg.payload)
        if command == "status":
            payload: dict[str, Any] = self._build_status_payload()
        else:
            payload = {
                "error": "Unsupported command. Use 'status'.",
                "supported_commands": ["status"],
            }
            self.metrics.tasks_failed += 1

        if msg.sender_id:
            await self.send(msg.sender_id, MessageType.RESULT, payload)

    # ── HA WebSocket listener ──────────────────────────────────────────────────

    async def _state_change_listener(self) -> None:
        if not self.ha_url or not self.ha_ws_url or not self.ha_token:
            self._last_error = (
                "HA_URL/HOME_ASSISTANT_URL or HA_TOKEN/HOME_ASSISTANT_TOKEN is not configured"
            )
            logger.warning("[%s] %s", self.name, self._last_error)
            return

        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                async with HAWebSocketClient(self.ha_ws_url, self.ha_token) as ha:
                    subscription_id = await ha.subscribe_events(STATE_CHANGED_EVENT)
                    self._last_error = ""
                    logger.info(
                        "[%s] subscribed to Home Assistant '%s' events",
                        self.name,
                        STATE_CHANGED_EVENT,
                    )

                    while self.state not in (ActorState.STOPPED, ActorState.FAILED):
                        event_message = await ha.receive_event(subscription_id)
                        await self._handle_state_change(event_message)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("[%s] listener error: %s", self.name, exc)
                if self.state not in (ActorState.STOPPED, ActorState.FAILED):
                    await asyncio.sleep(5)

    async def _handle_state_change(self, event_message: dict[str, Any]) -> None:
        event_data: dict[str, Any] = event_message.get("event", {}).get("data", {})
        entity_id: str = event_data.get("entity_id", "")
        domain: str = entity_id.split(".")[0] if "." in entity_id else ""

        if self._domain_filter and domain not in self._domain_filter:
            return

        payload = self._build_state_change_payload(event_data, entity_id, domain)
        topic = (
            f"{self._output_topic}/{domain}/{entity_id}"
            if self._per_entity_topics
            else self._output_topic
        )
        await self._mqtt_publish(topic, payload)

        self._events_seen += 1
        self._last_event_at = payload["timestamp"]
        self.metrics.tasks_completed += 1

    # ── Payload builders ───────────────────────────────────────────────────────

    def _build_state_change_payload(
        self, event_data: dict[str, Any], entity_id: str, domain: str
    ) -> dict[str, Any]:
        return {
            "type": "home_assistant_state_change",
            "entity_id": entity_id,
            "domain": domain,
            "new_state": event_data.get("new_state"),
            "old_state": event_data.get("old_state"),
            "context": event_data.get("context", {}),
            "timestamp": time.time(),
        }

    def _build_status_payload(self) -> dict[str, Any]:
        return {
            "configured": bool(self.ha_url and self.ha_token),
            "event_type": STATE_CHANGED_EVENT,
            "events_seen": self._events_seen,
            "last_event_at": self._last_event_at,
            "last_error": self._last_error,
            "output_topic": self._output_topic,
            "domain_filter": sorted(self._domain_filter),
            "per_entity_topics": self._per_entity_topics,
        }

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _extract_command(self, payload: Any) -> str:
        if isinstance(payload, dict):
            text = payload.get("text") or payload.get("task") or payload.get("command") or ""
        else:
            text = payload
        return str(text or "").strip().lower()

    def _current_task_description(self) -> str:
        if self._last_error:
            return f"waiting for HA state changes (error: {self._last_error})"
        domains = ", ".join(sorted(self._domain_filter)) if self._domain_filter else "all"
        return f"watching {STATE_CHANGED_EVENT} domains={domains} ({self._events_seen} seen)"
