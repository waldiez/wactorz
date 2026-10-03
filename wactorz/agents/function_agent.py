"""An agent made from one function.

The smallest thing a developer brings to Wactorz is a callable: a model's
`predict`, a pipeline over a reading, a lookup. :func:`agent` turns it into an
actor with a manifest, subscriptions, an output topic and a task handler, and
leaves the function as it was, so it can still be called and tested on its own.

    import wactorz

    @wactorz.agent(
        name="imu-anomaly",
        subscribes="sensors/imu/#",
        publishes="anomalies/imu",
        description="Flags IMU readings the trained model calls abnormal.",
    )
    def detect(reading: dict) -> dict | None:
        return reading if MODEL.predict(reading) == -1 else None

The function is called once per message with the decoded payload, and once per
task sent to the agent by chat or by another agent. What it returns is
published to the output topic, or sent back as the task's result; ``None``
publishes nothing. A plain function runs on a worker thread, so a model that
takes a while does not hold the event loop; a coroutine function runs on the
loop. A function that also wants the actor -- to persist, recall or publish --
takes it as a second parameter.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)

#: The attribute a decorated function carries its specification on.
SPEC_ATTRIBUTE = "__wactorz_spec__"


def _topics(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """``value`` as a tuple of topic filters: one string, several, or none."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    return tuple(str(topic) for topic in value if str(topic).strip())


def _summary(value: Any, limit: int = 160) -> str:
    """``value`` on one line, cut short for a feed row."""
    text = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def agent_name_from(identifier: str) -> str:
    """A topic-safe agent name from a function or class name: ``ImuAnomaly`` → ``imu-anomaly``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", identifier)
    return re.sub(r"[^a-z0-9]+", "-", spaced.lower()).strip("-") or "agent"


@dataclass(frozen=True)
class AgentSpec:
    """What :func:`agent` recorded about a function: the agent it describes."""

    fn: Callable[..., Any]
    name: str
    subscribes: tuple[str, ...] = ()
    publishes: str | None = None
    description: str = ""
    capabilities: tuple[str, ...] = ()
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    #: What the agent needs from the machine it runs on: RAM, packages, devices.
    #: Carried in its spawn record and manifest for whoever places it.
    requires: dict[str, Any] = field(default_factory=dict)
    #: Whether the agent is started with the system, or only when asked for.
    autostart: bool = True

    @property
    def wants_actor(self) -> bool:
        """Whether the function takes the actor as its second parameter."""
        try:
            params = [
                p
                for p in inspect.signature(self.fn).parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
        except (TypeError, ValueError):
            return False
        return len(params) >= 2

    def build(
        self,
        *,
        name: str | None = None,
        persistence_dir: str | None = None,
        llm_provider: Any = None,
        options: dict[str, Any] | None = None,
    ) -> FunctionAgent:
        """The actor for this specification, under ``name`` or the spec's own."""
        return FunctionAgent(
            self,
            name=name or self.name,
            persistence_dir=persistence_dir,
            llm_provider=llm_provider,
            options=options,
        )


def agent(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    subscribes: str | Iterable[str] | None = None,
    publishes: str | None = None,
    description: str = "",
    capabilities: Iterable[str] = (),
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    requires: dict[str, Any] | None = None,
    autostart: bool = True,
) -> Any:
    """Declare a function as an agent. Works bare, ``@agent``, or with arguments.

    The function is returned unchanged, with the specification attached as
    ``__wactorz_spec__``; :mod:`wactorz.plugins` reads it from there, and
    ``spec.build()`` makes the actor.
    """

    def _declare(target: Callable[..., Any]) -> Callable[..., Any]:
        spec = AgentSpec(
            fn=target,
            name=name or agent_name_from(target.__name__),
            subscribes=_topics(subscribes),
            publishes=publishes,
            description=description or (inspect.getdoc(target) or "").split("\n")[0],
            capabilities=tuple(capabilities),
            input_schema=dict(input_schema or {}),
            output_schema=dict(output_schema or {}),
            requires=dict(requires or {}),
            autostart=autostart,
        )
        setattr(target, SPEC_ATTRIBUTE, spec)
        return target

    if fn is not None:
        return _declare(fn)
    return _declare


def spec_of(obj: Any) -> AgentSpec | None:
    """The specification a decorated function carries, or None for anything else."""
    spec = getattr(obj, SPEC_ATTRIBUTE, None)
    return spec if isinstance(spec, AgentSpec) else None


class FunctionAgent(Actor):
    """The actor behind a decorated function; see :func:`agent`."""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        name: str | None = None,
        persistence_dir: str | None = None,
        llm_provider: Any = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(name=name or spec.name, persistence_dir=persistence_dir)
        self.spec = spec
        self.description = spec.description
        self.capabilities = list(spec.capabilities)
        #: Whatever the spawn config passed as ``options``: a model path, a
        #: threshold. Read by a function that takes the actor.
        self.options: dict[str, Any] = dict(options or {})
        #: Kept for a function that wants to call the model the system uses.
        self.llm = llm_provider

    async def on_start(self) -> None:
        publishes = [self.spec.publishes] if self.spec.publishes else []
        await self.publish_manifest(
            description=self.spec.description,
            publishes=publishes,
            capabilities=list(self.spec.capabilities),
            input_schema=self.spec.input_schema,
            output_schema=self.spec.output_schema,
            subscribes=list(self.spec.subscribes),
        )
        for topic in self.spec.subscribes:
            self.subscribe(topic, self._on_message)
        if self.spec.subscribes:
            await self.log(f"Listening on {', '.join(self.spec.subscribes)}")

    def _current_task_description(self) -> str:
        return self.spec.description or f"running {self.spec.fn.__name__}()"

    async def call(self, payload: Any) -> Any:
        """Run the function on ``payload``, on a thread when it is not a coroutine function."""
        fn = self.spec.fn
        args = (payload, self) if self.spec.wants_actor else (payload,)
        if inspect.iscoroutinefunction(fn):
            return await fn(*args)
        return await asyncio.to_thread(fn, *args)

    async def log(self, message: str, level: str = "info") -> None:
        """Say something on the dashboard feed, under this agent's name."""
        getattr(logger, level, logger.info)("[%s] %s", self.name, message)
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": message, "timestamp": time.time()},
        )

    async def _on_message(self, payload: Any) -> None:
        """A message on a subscribed topic: run the function, publish what it returns.

        Counted as a processed message, so the dashboard card shows the agent
        working; a subscription does not pass through the mailbox the base
        class counts. What is published is also said on the feed, since a data
        topic is not shown there and a detector that only publishes looks idle.
        """
        result = await self.call(payload)
        self.metrics.messages_processed += 1
        self.metrics.tasks_completed += 1
        if result is None or not self.spec.publishes:
            return
        await self._mqtt_publish(self.spec.publishes, result)
        await self.log(f"→ {self.spec.publishes}: {_summary(result)}")

    async def handle_message(self, msg: Message) -> None:
        """A task from chat or another agent: run the function, answer with the result."""
        if msg.type != MessageType.TASK:
            return
        payload = msg.payload
        task_id = payload.get("_task_id") if isinstance(payload, dict) else None
        if isinstance(payload, dict):
            payload = {k: v for k, v in payload.items() if k != "_task_id"}
        try:
            result = await self.call(payload)
        except Exception as exc:
            self.metrics.tasks_failed += 1
            logger.exception("[%s] %s() failed on a task", self.name, self.spec.fn.__name__)
            reply: dict[str, Any] = {"error": str(exc)}
        else:
            self.metrics.tasks_completed += 1
            reply = dict(result) if isinstance(result, dict) else {"result": result}
        if task_id is not None:
            reply["_task_id"] = task_id
        await self.send(msg.reply_to or msg.sender_id, MessageType.RESULT, reply)
