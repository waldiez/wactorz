"""
HomeAssistantActuatorAgent — reactive actuator for external automations.

Subscribes to one or more MQTT topics, filters incoming detection payloads,
evaluates optional Home Assistant entity conditions, and calls HA services via
a persistent WebSocket connection.  One instance per automation.

Designed to be the actuator end of the pipeline:

    DynamicAgent (sensor) → MQTT topic → HomeAssistantActuatorAgent → HA service call
"""

from __future__ import annotations

import asyncio
import logging
import operator as _op
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from wactorz.config import CONFIG

from ..core.actor import Actor, ActorState, Message, MessageType
from ..core.integrations.home_assistant.ha_helper import normalize_ha_ws_url
from ..core.integrations.home_assistant.ha_web_socket_client import HAWebSocketClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------

_OPERATORS: dict[str, Any] = {
    "eq":  _op.eq,
    "ne":  _op.ne,
    "gt":  _op.gt,
    "lt":  _op.lt,
    "gte": _op.ge,
    "lte": _op.le,
}


@dataclass
class ActuatorAction:
    """A single HA service call to execute when a detection triggers the actuator."""
    domain: str        # e.g. "light", "climate", "cover"
    service: str       # e.g. "turn_on", "set_temperature", "open_cover"
    entity_id: str     # e.g. "light.living_room_lamp"
    service_data: dict = field(default_factory=dict)  # e.g. {"color_name": "red"}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ActuatorAction":
        return cls(
            domain=d["domain"],
            service=d["service"],
            entity_id=d["entity_id"],
            service_data=d.get("service_data", {}),
        )


@dataclass
class ActuatorCondition:
    """A condition checked against a live HA entity state before actuating."""
    entity_id: str   # e.g. "sun.sun"
    attribute: str   # "state" or dotted path like "attributes.elevation"
    operator: str    # one of: eq, ne, gt, lt, gte, lte
    value: Any       # e.g. "above_horizon" or 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ActuatorCondition":
        return cls(
            entity_id=d["entity_id"],
            attribute=d["attribute"],
            operator=d["operator"],
            value=d["value"],
        )

    def evaluate(self, entity_state: dict) -> bool:
        """Return True if the condition holds for the given HA state object."""
        cmp = _OPERATORS.get(self.operator)
        if cmp is None:
            logger.warning("Unknown condition operator %r — treating as True", self.operator)
            return True

        # Navigate dotted attribute path
        value = entity_state
        for part in self.attribute.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break

        try:
            return bool(cmp(value, self.value))
        except TypeError:
            logger.warning(
                "Condition comparison failed: %r %s %r", value, self.operator, self.value
            )
            return False


@dataclass
class ActuatorConfig:
    """Full configuration for one HomeAssistantActuatorAgent."""
    automation_id: str
    description: str
    mqtt_topics: list[str]
    actions: list[ActuatorAction]
    conditions: list[ActuatorCondition] = field(default_factory=list)
    detection_filter: dict | None = None   # key-value match on incoming payload
    cooldown_seconds: float = 10.0

    def to_dict(self) -> dict:
        return {
            "automation_id": self.automation_id,
            "description": self.description,
            "mqtt_topics": self.mqtt_topics,
            "actions": [a.to_dict() for a in self.actions],
            "conditions": [c.to_dict() for c in self.conditions],
            "detection_filter": self.detection_filter,
            "cooldown_seconds": self.cooldown_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ActuatorConfig":
        return cls(
            automation_id=d["automation_id"],
            description=d.get("description", ""),
            mqtt_topics=d["mqtt_topics"],
            actions=[ActuatorAction.from_dict(a) for a in d.get("actions", [])],
            conditions=[ActuatorCondition.from_dict(c) for c in d.get("conditions", [])],
            detection_filter=d.get("detection_filter"),
            cooldown_seconds=float(d.get("cooldown_seconds", 10.0)),
        )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class HomeAssistantActuatorAgent(Actor):
    """
    Reactive actuator that subscribes to MQTT topics and calls HA services.

    One instance per external automation.  Lifecycle:
      on_start → open persistent HA WebSocket + subscribe to MQTT topics
      detection → filter → cooldown → conditions → call_service × N
      on_stop  → persist counters

    Spawn via actor.spawn(HomeAssistantActuatorAgent, config=config).
    """

    def __init__(self, config: ActuatorConfig, **kwargs: Any) -> None:
        kwargs.setdefault("name", f"actuator-{config.automation_id[:20]}")
        super().__init__(**kwargs)

        self.config = config
        self.ha_url = (CONFIG.ha_url or "").rstrip("/")
        self.ha_token = (CONFIG.ha_token or "").strip()
        self.ha_ws_url = normalize_ha_ws_url(self.ha_url)

        self._ha: HAWebSocketClient | None = None
        self._ws_ready: asyncio.Event = asyncio.Event()  # set when HA WS is connected
        self._last_actuation_time: float = 0.0
        self._actuations_count: int = 0

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_start(self) -> None:
        self._last_actuation_time = float(self.recall("last_actuation_time", 0.0))
        self._actuations_count = int(self.recall("actuations_count", 0))
        self.persist("config", self.config.to_dict())

        await self.publish_manifest(
            description=self.config.description or f"Actuator for {self.config.automation_id}",
            capabilities=["ha_actuator"],
            input_schema={"payload": "dict — detection event from sensor agent"},
        )

        self._tasks.append(asyncio.create_task(self._ws_keepalive()))
        self._tasks.append(asyncio.create_task(self._mqtt_listener()))

        logger.info(
            "[%s] started — topics=%r actions=%d conditions=%d",
            self.name,
            self.config.mqtt_topics,
            len(self.config.actions),
            len(self.config.conditions),
        )

    async def on_stop(self) -> None:
        self.persist("last_actuation_time", self._last_actuation_time)
        self.persist("actuations_count", self._actuations_count)

    # ── HA WebSocket keepalive ─────────────────────────────────────────────────

    async def _ws_keepalive(self) -> None:
        """Maintain a persistent HA WebSocket connection for service calls and state checks."""
        if not self.ha_ws_url or not self.ha_token:
            logger.warning("[%s] HA URL/token not configured — conditions will be skipped", self.name)
            return

        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                async with HAWebSocketClient(self.ha_ws_url, self.ha_token) as ha:
                    self._ha = ha
                    self._ws_ready.set()
                    logger.info("[%s] HA WebSocket connected", self.name)
                    # Hold the connection open until the actor stops or the WS drops
                    while self.state not in (ActorState.STOPPED, ActorState.FAILED):
                        await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._ha = None
                self._ws_ready.clear()
                logger.warning("[%s] HA WebSocket error: %s — reconnecting in 5s", self.name, exc)
                if self.state not in (ActorState.STOPPED, ActorState.FAILED):
                    await asyncio.sleep(5)
        self._ha = None

    # ── MQTT listener ──────────────────────────────────────────────────────────

    async def _mqtt_listener(self) -> None:
        """Subscribe to configured MQTT topics and dispatch each message."""
        try:
            import aiomqtt
        except ImportError:
            logger.error("[%s] aiomqtt not installed — MQTT listener disabled", self.name)
            return

        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    for topic in self.config.mqtt_topics:
                        await client.subscribe(topic)
                    logger.info("[%s] subscribed to %r", self.name, self.config.mqtt_topics)

                    async for message in client.messages:
                        if self.state in (ActorState.STOPPED, ActorState.FAILED):
                            break
                        if self.state == ActorState.PAUSED:
                            continue
                        try:
                            import json
                            payload = json.loads(message.payload.decode())
                            await self._on_detection(payload)
                        except Exception as exc:
                            logger.error("[%s] Failed to process message: %s", self.name, exc)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self.state not in (ActorState.STOPPED, ActorState.FAILED):
                    logger.warning("[%s] MQTT listener error: %s — reconnecting in 5s", self.name, exc)
                    await asyncio.sleep(5)

    # ── Detection processing ───────────────────────────────────────────────────

    async def _on_detection(self, payload: dict) -> None:
        """Process one incoming MQTT message through filter → cooldown → conditions → actions."""
        if not self._matches_filter(payload):
            return

        now = time.time()
        if now - self._last_actuation_time < self.config.cooldown_seconds:
            logger.debug("[%s] Cooldown active — skipping", self.name)
            return

        if not await self._all_conditions_met():
            logger.debug("[%s] Conditions not met — skipping", self.name)
            return

        for action in self.config.actions:
            await self._call_service(action)

        self._last_actuation_time = now
        self._actuations_count += 1
        self.metrics.tasks_completed += 1

        await self._mqtt_publish(
            f"agents/{self.actor_id}/actuations",
            {
                "automation_id": self.config.automation_id,
                "actions": [a.to_dict() for a in self.config.actions],
                "timestamp": now,
                "trigger_payload": payload,
            },
        )

    def _matches_filter(self, payload: dict) -> bool:
        """Return True if all detection_filter entries match the payload.

        Values can be literals (equality) or operator dicts e.g. {"gt": 0.8}.
        Supported operators: eq, ne, gt, lt, gte, lte.
        """
        if not self.config.detection_filter:
            return True
        for key, expected in self.config.detection_filter.items():
            actual = payload.get(key)
            if isinstance(expected, dict):
                op_name, op_val = next(iter(expected.items()))
                cmp = _OPERATORS.get(op_name)
                if cmp is None or actual is None:
                    return False
                try:
                    if not cmp(actual, op_val):
                        return False
                except TypeError:
                    return False
            else:
                if actual != expected:
                    return False
        return True

    async def _all_conditions_met(self) -> bool:
        """Return True if all ActuatorConditions pass (AND logic)."""
        if not self.config.conditions:
            return True
        if self._ha is None:
            logger.warning("[%s] No HA connection — skipping condition checks (fail-open)", self.name)
            return True

        for condition in self.config.conditions:
            try:
                entity_state = await self._ha.get_entity_state(condition.entity_id)
                if entity_state is None:
                    logger.warning("[%s] Entity %r not found — condition fails", self.name, condition.entity_id)
                    return False
                if not condition.evaluate(entity_state):
                    return False
            except Exception as exc:
                logger.error("[%s] Condition check error for %r: %s", self.name, condition.entity_id, exc)
                return False

        return True

    async def _call_service(self, action: ActuatorAction) -> None:
        """Call a HA service via the persistent WebSocket connection.
        Waits up to 10 seconds for the connection to be ready before giving up.
        """
        if self._ha is None:
            try:
                await asyncio.wait_for(self._ws_ready.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.error(
                    "[%s] No HA connection after 10s — cannot call service %s.%s",
                    self.name, action.domain, action.service,
                )
                return
        try:
            await self._ha.call_service(
                action.domain,
                action.service,
                action.entity_id,
                **action.service_data,
            )
            logger.info(
                "[%s] Called %s.%s on %s (data=%r)",
                self.name,
                action.domain,
                action.service,
                action.entity_id,
                action.service_data,
            )
        except Exception as exc:
            logger.error("[%s] Service call failed: %s", self.name, exc)
            self.metrics.tasks_failed += 1

    # ── Actor overrides ────────────────────────────────────────────────────────

    async def handle_message(self, msg: Message) -> None:
        if msg.type != MessageType.TASK:
            return
        status = {
            "automation_id": self.config.automation_id,
            "description": self.config.description,
            "mqtt_topics": self.config.mqtt_topics,
            "actuations_count": self._actuations_count,
            "last_actuation_time": self._last_actuation_time,
            "ha_connected": self._ha is not None,
        }
        if msg.sender_id:
            await self.send(msg.sender_id, MessageType.RESULT, status)

    def _current_task_description(self) -> str:
        topics = ", ".join(self.config.mqtt_topics)
        return f"listening on [{topics}] ({self._actuations_count} actuations)"
