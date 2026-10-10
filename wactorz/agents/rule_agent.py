"""A rule: when a message arrives on a topic and a condition holds, act.

The glue between the stages of a pipeline, without a program per rule. A rule
subscribes to one or more trigger topics, checks conditions against the
payload, waits out a cooldown, and runs its actions: publish a message, send a
task to an agent, or call a webhook. A Home Assistant service call stays with
the ``ha_actuator`` type; a rule is for everything that is not Home Assistant.

A spawn config, a pipeline definition and chat all describe a rule the same way:

    {
      "type": "rule",
      "name": "imu-alert",
      "triggers": ["anomalies/imu"],
      "conditions": [{"field": "score", "op": "gt", "value": 10}],
      "actions": [
        {"type": "publish", "topic": "alerts/imu", "payload": {"level": "high"}},
        {"type": "task", "agent": "notifier", "payload": {"text": "IMU anomaly {score}"}},
        {"type": "webhook", "url": "https://hooks.example/imu"}
      ],
      "cooldown_seconds": 30
    }

An action's payload may name fields of the trigger payload in braces, by the
same dotted path a condition uses (``{reading.score}``), and carries the
trigger payload itself under ``trigger`` unless told not to.
"""

from __future__ import annotations

import logging
import operator
import string
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)

#: How long a webhook call may take. Generous: the cost of a slow hook is a
#: late action, the cost of a tight limit is an action that never happens.
WEBHOOK_TIMEOUT_S = 30.0

_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "in": lambda value, expected: value in expected,
    "contains": lambda value, expected: expected in value,
}
#: Spellings accepted for the operators above, so a rule written by hand reads naturally.
_ALIASES = {"==": "eq", "=": "eq", "!=": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}
#: Operators that need no value: whether the field is there at all.
_PRESENCE = ("exists", "absent")

ACTION_TYPES = ("publish", "task", "webhook")


def field_value(payload: Any, path: str) -> tuple[bool, Any]:
    """The value at a dotted ``path`` into ``payload``, and whether it was there."""
    value = payload
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


@dataclass(frozen=True)
class RuleCondition:
    """One check against the trigger payload."""

    field: str
    op: str = "eq"
    value: Any = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RuleCondition:
        op = str(raw.get("op", raw.get("operator", "eq")))
        op = _ALIASES.get(op, op)
        if op not in _OPERATORS and op not in _PRESENCE:
            raise ValueError(f"unknown condition operator {op!r}")
        if "field" not in raw:
            raise ValueError("a condition needs a 'field'")
        return cls(field=str(raw["field"]), op=op, value=raw.get("value"))

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "op": self.op, "value": self.value}

    def holds(self, payload: Any) -> bool:
        present, value = field_value(payload, self.field)
        if self.op == "exists":
            return present
        if self.op == "absent":
            return not present
        if not present:
            return False
        try:
            return bool(_OPERATORS[self.op](value, self.value))
        except (TypeError, ValueError):
            # A comparison that does not apply -- a string against a number --
            # is a condition that does not hold, not a crashed rule.
            return False


@dataclass(frozen=True)
class RuleAction:
    """What a rule does once its conditions hold."""

    type: str
    topic: str = ""
    agent: str = ""
    url: str = ""
    method: str = "POST"
    payload: dict[str, Any] = field(default_factory=dict)
    #: Whether the trigger payload travels with the action, under ``trigger``.
    include_trigger: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RuleAction:
        kind = str(raw.get("type", "")).strip()
        if kind not in ACTION_TYPES:
            raise ValueError(f"unknown action type {kind!r}; one of {', '.join(ACTION_TYPES)}")
        action = cls(
            type=kind,
            topic=str(raw.get("topic", "")),
            agent=str(raw.get("agent", "")),
            url=str(raw.get("url", "")),
            method=str(raw.get("method", "POST")).upper(),
            payload=dict(raw.get("payload") or {}),
            include_trigger=bool(raw.get("include_trigger", True)),
        )
        if kind == "publish" and not action.topic:
            raise ValueError("a publish action needs a 'topic'")
        if kind == "task" and not action.agent:
            raise ValueError("a task action needs an 'agent'")
        if kind == "webhook" and not action.url.startswith(("http://", "https://")):
            raise ValueError("a webhook action needs an http(s) 'url'")
        return action

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "payload": dict(self.payload)}
        if self.topic:
            out["topic"] = self.topic
        if self.agent:
            out["agent"] = self.agent
        if self.url:
            out["url"] = self.url
            out["method"] = self.method
        if not self.include_trigger:
            out["include_trigger"] = False
        return out

    def body(self, trigger: Any) -> dict[str, Any]:
        """The payload to send: the action's, with trigger fields filled in."""
        rendered = {key: _render(value, trigger) for key, value in self.payload.items()}
        if self.include_trigger:
            rendered.setdefault("trigger", trigger)
        return rendered


class _Unfilled:
    """A placeholder the trigger has no value for, written back as it was."""

    def __init__(self, field_name: str) -> None:
        self.field_name = field_name

    def __format__(self, spec: str) -> str:
        return "{" + self.field_name + (":" + spec if spec else "") + "}"

    def __str__(self) -> str:
        return "{" + self.field_name + "}"

    __repr__ = __str__


class _TriggerFormatter(string.Formatter):
    """Fills ``{field}`` and ``{dotted.path}`` the way a condition reads a field.

    A placeholder names a path into the trigger payload, as a condition's
    ``field`` does, so ``{reading.score}`` is the score inside ``reading``
    rather than an attribute lookup on a dict. A path the payload does not have
    is left in the text as written.
    """

    def get_field(self, field_name: str, args: Any, kwargs: Any) -> tuple[Any, str]:
        found, value = field_value(kwargs, field_name)
        return (value if found else _Unfilled(field_name)), field_name


_FORMATTER = _TriggerFormatter()


def _render(value: Any, trigger: Any) -> Any:
    """A string with ``{field}`` placeholders filled from a dict trigger; anything else as is.

    A string that cannot be rendered -- a stray brace, a format spec the value
    does not take -- is sent as written: the action still runs.
    """
    if isinstance(value, str) and "{" in value and isinstance(trigger, dict):
        try:
            return _FORMATTER.vformat(value, (), trigger)
        except (ValueError, TypeError, IndexError, KeyError, AttributeError):
            return value
    return value


@dataclass(frozen=True)
class RuleConfig:
    """Everything a rule is: what it listens to, when it holds, what it does."""

    triggers: tuple[str, ...]
    actions: tuple[RuleAction, ...]
    conditions: tuple[RuleCondition, ...] = ()
    cooldown_seconds: float = 0.0
    description: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RuleConfig:
        triggers = raw.get("triggers", raw.get("trigger", raw.get("mqtt_topics", ())))
        if isinstance(triggers, str):
            triggers = (triggers,)
        triggers = tuple(str(t) for t in triggers if str(t).strip())
        if not triggers:
            raise ValueError("a rule needs at least one trigger topic")
        actions = tuple(RuleAction.from_dict(a) for a in raw.get("actions") or ())
        if not actions:
            raise ValueError("a rule needs at least one action")
        conditions = tuple(RuleCondition.from_dict(c) for c in raw.get("conditions") or ())
        return cls(
            triggers=triggers,
            actions=actions,
            conditions=conditions,
            cooldown_seconds=float(raw.get("cooldown_seconds", 0.0) or 0.0),
            description=str(raw.get("description", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggers": list(self.triggers),
            "conditions": [c.to_dict() for c in self.conditions],
            "actions": [a.to_dict() for a in self.actions],
            "cooldown_seconds": self.cooldown_seconds,
            "description": self.description,
        }

    @property
    def publishes(self) -> tuple[str, ...]:
        """The topics this rule's publish actions write to, for wiring."""
        return tuple(a.topic for a in self.actions if a.type == "publish")


class RuleAgent(Actor):
    """Runs one :class:`RuleConfig`; see the module docstring."""

    def __init__(self, config: RuleConfig, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.description = config.description or self._describe()
        self._fired_at: float | None = None
        self.fired = 0

    def _describe(self) -> str:
        what = ", ".join(a.type for a in self.config.actions)
        return f"when {', '.join(self.config.triggers)} → {what}"

    def _current_task_description(self) -> str:
        return self.description

    async def on_start(self) -> None:
        await self.publish_manifest(
            description=self.description,
            publishes=list(self.config.publishes),
            capabilities=["rule"],
            input_schema={"trigger payload": "any"},
            subscribes=list(self.config.triggers),
        )
        for topic in self.config.triggers:
            self.subscribe(topic, self.evaluate)

    async def evaluate(self, payload: Any) -> bool:
        """Check the conditions and cooldown; run the actions when both pass. True if fired."""
        self.metrics.messages_processed += 1
        if not all(c.holds(payload) for c in self.config.conditions):
            return False
        now = time.monotonic()
        if self._fired_at is not None and now - self._fired_at < self.config.cooldown_seconds:
            return False
        self._fired_at = now
        self.fired += 1
        for action in self.config.actions:
            try:
                await self._run(action, payload)
            except Exception:
                # One failing action must not stop the others, or the rule.
                self.metrics.tasks_failed += 1
                logger.exception("[%s] %s action failed", self.name, action.type)
        self.metrics.tasks_completed += 1
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": f"fired: {self._describe()}", "timestamp": time.time()},
        )
        return True

    async def _run(self, action: RuleAction, trigger: Any) -> None:
        body = action.body(trigger)
        if action.type == "publish":
            await self.publish(action.topic, body)
        elif action.type == "task":
            await self._send_task(action.agent, body)
        elif action.type == "webhook":
            await self._call_webhook(action, body)

    async def _send_task(self, agent_name: str, body: dict[str, Any]) -> None:
        target = self._registry.find_by_name(agent_name) if self._registry else None
        if target is None:
            logger.warning("[%s] task action: no agent named %r is running", self.name, agent_name)
            return
        await self.send(target.actor_id, MessageType.TASK, body)

    async def _call_webhook(self, action: RuleAction, body: dict[str, Any]) -> None:
        timeout = aiohttp.ClientTimeout(total=WEBHOOK_TIMEOUT_S)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.request(action.method, action.url, json=body) as response,
        ):
            if response.status >= 400:
                logger.warning(
                    "[%s] webhook %s answered %s", self.name, action.url, response.status
                )

    async def handle_message(self, msg: Message) -> None:
        """A task is a trial run: the payload is treated as a trigger and the verdict returned."""
        if msg.type != MessageType.TASK:
            return
        payload = msg.payload
        task_id = payload.get("_task_id") if isinstance(payload, dict) else None
        if isinstance(payload, dict):
            payload = {k: v for k, v in payload.items() if k != "_task_id"}
        fired = await self.evaluate(payload)
        reply: dict[str, Any] = {"fired": fired, "rule": self.name}
        if task_id is not None:
            reply["_task_id"] = task_id
        await self.send(msg.reply_to or msg.sender_id, MessageType.RESULT, reply)
