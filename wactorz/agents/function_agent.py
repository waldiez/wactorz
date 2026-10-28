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
takes it as a second parameter, one without a default or annotated
``FunctionAgent``; a second parameter with a default is the function's own.

A function serving a model by its bare name follows that model's ``active``
pointer, and while the model's ``shadow`` pointer is set, runs that version too:
after each message is answered with the active version, the function is called
once more with a view of the actor that reads the shadow version and publishes
under ``shadow/``, and the two answers are kept as a pair for the trial's
comparison (`wactorz.agents.shadow_serving`). A shadow pass is skipped, never
queued, while the previous one is still running, so a trial never slows what
is served. Tasks are answered with the active version only.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core import models
from ..core.actor import Actor, Message, MessageType, run_callable
from ..core.paths import resolve_state_dir
from ..core.subscriptions import ConcurrencyError
from .shadow_serving import PairRecorder, ShadowView, flush_every, now_ms, pair_row, shadow_topic

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


def _names_function_agent(annotation: Any) -> bool:
    """Whether a parameter annotation is :class:`FunctionAgent`, as a class or as text.

    Text, because a module with ``from __future__ import annotations`` keeps
    every annotation as the string it was written as: ``"FunctionAgent"``,
    ``"wactorz.FunctionAgent"``, ``"FunctionAgent | None"``.
    """
    if isinstance(annotation, str):
        return re.search(r"(?:^|[\s.|\[])FunctionAgent\b", annotation) is not None
    return isinstance(annotation, type) and issubclass(annotation, FunctionAgent)


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
    #: The models the function loads, by the key it reads each under:
    #: ``{"model": "imu-detector:3"}``, read with ``me.artifact_path("model")``
    #: or ``await me.load_model("model")``. See `wactorz.core.models`.
    models: dict[str, Any] = field(default_factory=dict)
    #: Whether the agent is started with the system, or only when asked for.
    autostart: bool = True
    #: How many messages, and tasks, the function handles at once. One keeps
    #: messages on a topic in order; more is for a function that waits on a
    #: model or a job and would otherwise queue its topic behind itself.
    concurrency: int = 1

    @property
    def wants_actor(self) -> bool:
        """Whether the function takes the actor as its second parameter.

        It does when that parameter has no default, or when it is annotated as a
        :class:`FunctionAgent`. A second parameter with a default and any other
        annotation -- ``def detect(reading, threshold=4.0)`` -- is the
        function's own and keeps its default.
        """
        try:
            params = [
                p
                for p in inspect.signature(self.fn).parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
        except (TypeError, ValueError):
            return False
        if len(params) < 2:
            return False
        second = params[1]
        return second.default is second.empty or _names_function_agent(second.annotation)

    def build(
        self,
        *,
        name: str | None = None,
        persistence_dir: str | None = None,
        llm_provider: Any = None,
        options: dict[str, Any] | None = None,
        actor_class: type[FunctionAgent] | None = None,
        actor_id: str | None = None,
    ) -> FunctionAgent:
        """The actor for this specification, under ``name`` or the spec's own.

        ``actor_class`` is a subclass of :class:`FunctionAgent` to build
        instead, for a host that adds to what the actor does where it runs.
        ``actor_id`` is the id its spawn registry keeps for it, when it has one.
        """
        return (actor_class or FunctionAgent)(
            self,
            actor_id=actor_id,
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
    models: dict[str, Any] | None = None,
    autostart: bool = True,
    concurrency: int = 1,
) -> Any:
    """Declare a function as an agent. Works bare, ``@agent``, or with arguments.

    The function is returned unchanged, with the specification attached as
    ``__wactorz_spec__``; :mod:`wactorz.plugins` reads it from there, and
    ``spec.build()`` makes the actor. ``concurrency`` above one runs that many
    messages and tasks at once, out of order; see :class:`AgentSpec`.
    ``models`` names the artifacts the function loads -- ``{"model":
    "imu-detector:3"}`` -- which are found before the agent starts, and read
    with ``me.artifact_path("model")`` or ``await me.load_model("model")``.
    """
    if concurrency < 1:
        raise ConcurrencyError(concurrency)

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
            models=dict(models or {}),
            concurrency=concurrency,
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


class MeteredLLM:
    """The system's provider as a decorated function sees it: every call counted on the actor.

    The built-in agents keep their own ledgers; a function has none, so the
    usage a provider returns with each answer would be lost. This wrapper
    hands it to :meth:`~wactorz.core.actor.Actor.record_llm_cost` and passes
    everything else through, so ``me.llm`` reads like the provider itself.
    A stream is not metered: its usage is not reported.
    """

    def __init__(self, provider: Any, actor: Actor) -> None:
        #: The provider itself, for code that needs its class or its settings.
        self.provider = provider
        self._provider = provider
        self._actor = actor

    async def complete(
        self, messages: list[dict], system: str = "", **kwargs: Any
    ) -> tuple[str, dict]:
        text, usage = await self._provider.complete(messages, system, **kwargs)
        self._record(usage)
        return text, usage

    async def complete_with_tools(
        self, messages: list[dict], tools: list[dict[str, Any]], system: str = "", **kwargs: Any
    ) -> Any:
        result = await self._provider.complete_with_tools(messages, tools, system, **kwargs)
        self._record(getattr(result, "usage", None))
        return result

    def _record(self, usage: Any) -> None:
        usage = usage if isinstance(usage, dict) else {}
        self._actor.record_llm_cost(
            float(usage.get("cost_usd", 0.0) or 0.0),
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            model=str(getattr(self._provider, "model", "") or ""),
            provider=type(self._provider).__name__,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


class FunctionAgent(Actor):
    """The actor behind a decorated function; see :func:`agent`."""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        actor_id: str | None = None,
        name: str | None = None,
        persistence_dir: str | None = None,
        llm_provider: Any = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(actor_id=actor_id, name=name or spec.name, persistence_dir=persistence_dir)
        self.spec = spec
        self.description = spec.description
        self.capabilities = list(spec.capabilities)
        #: Whatever the spawn config passed as ``options``: a model path, a
        #: threshold. Read by a function that takes the actor.
        self.options: dict[str, Any] = dict(options or {})
        #: The model the system uses, for a function that wants it, with every
        #: call counted on this actor's card and against the cost limit.
        self.llm: Any = MeteredLLM(llm_provider, self) if llm_provider is not None else None
        #: Bounds the tasks in flight when the spec allows more than one.
        self._slots = asyncio.Semaphore(spec.concurrency)
        #: The version on trial beside each model this agent serves, by the key
        #: the function reads it under, with the path it is read at here; only
        #: keys whose model's ``shadow`` pointer is set. See the module docstring.
        self._shadow_artifacts: dict[str, dict[str, Any]] = {}
        #: What the shadow pass has loaded, by key, so a version is read once.
        self._shadow_loaded: dict[str, Any] = {}
        #: Whether a shadow pass is running: the next input skips its pass while one is.
        self._shadow_busy = False
        #: The pairs of each model on trial, by model name.
        self._pairs: dict[str, PairRecorder] = {}
        self._pairs_flusher: asyncio.Task[Any] | None = None
        #: Said once on the feed when pairs cannot be kept.
        self._said_pairs_unavailable = False
        #: How many inputs got a shadow pass, and how many were passed over.
        self.shadow_passes = 0
        self.shadow_skipped = 0

    def declared_models(self) -> dict[str, Any]:
        """The models the decorator named, resolved before `on_start`."""
        return self.spec.models

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
            self.subscribe(topic, self._listener(topic), concurrency=self.spec.concurrency)
        if self.spec.subscribes:
            await self.log(f"Listening on {', '.join(self.spec.subscribes)}")
        await self._resolve_shadows()

    async def on_stop(self) -> None:
        if self._pairs_flusher is not None:
            self._pairs_flusher.cancel()
            self._pairs_flusher = None
        for recorder in list(self._pairs.values()):
            try:
                await recorder.close()
            except Exception as exc:
                logger.warning(
                    "[%s] cannot write the pairs of %s: %s", self.name, recorder.model, exc
                )
        await super().on_stop()

    # ── The shadow version ────────────────────────────────────────────────

    @property
    def shadow(self) -> dict[str, dict[str, Any]]:
        """Each model on trial beside one this agent serves, by key: name, version, format, size, path."""
        return {key: dict(entry) for key, entry in self._shadow_artifacts.items()}

    async def _resolve_shadows(self) -> None:
        """Find the version on trial beside each model that follows ``active``, on main.

        An agent on a node runs no shadow yet: its pairs would be kept there,
        where the comparison cannot read them until the data package ships
        them to main.
        """
        if self._node or not self._artifacts:
            return
        for key, entry in list(self._artifacts.items()):
            try:
                found = await asyncio.to_thread(models.resolve_shadow, entry, resolve_state_dir())
            except Exception as exc:
                await self.log(f"no shadow for {key}: {exc}", level="warning")
                continue
            if found is not None:
                await self.swap_shadow(key, found)

    async def swap_shadow(self, key: str, entry: Mapping[str, Any] | None) -> None:
        """Try the version ``entry`` names beside the one served as ``key``; None ends the trial.

        ``entry`` is pinned with the ``path`` it is read at here. A model the
        function loads through ``load_model`` is read now, off the loop and
        with the same ``allow_code`` as the served one, so the first shadow
        pass does not wait on the disk; one it opens itself through
        ``artifact_path`` changes path only. Raises what the loader raises,
        and then the trial is as it was. ``None`` drops the version and closes
        the pairs.
        """
        self._artifact(key)
        if entry is None:
            was = self._shadow_artifacts.pop(key, None)
            self._shadow_loaded.pop(key, None)
            if was is not None:
                recorder = self._pairs.pop(str(was["name"]), None)
                if recorder is not None:
                    await recorder.close()
                await self.log(f"no longer trying {was['name']}:{was['version']} as {key}")
            if not self._shadow_artifacts and self._pairs_flusher is not None:
                self._pairs_flusher.cancel()
                self._pairs_flusher = None
            return
        loaded: Any = None
        if key in self._loaded_models:
            loaded = await asyncio.to_thread(
                models.load, entry, allow_code=self._model_load_flags.get(key, False)
            )
            self._shadow_loaded[key] = loaded
        else:
            self._shadow_loaded.pop(key, None)
        self._shadow_artifacts[key] = dict(entry)
        name = str(entry["name"])
        self._pairs.setdefault(name, PairRecorder(name))
        if self._pairs_flusher is None:
            self._pairs_flusher = self.run_detached(
                flush_every(self._pairs), name=f"{self.name}:pairs"
            )
        served = self._artifacts[key]
        await self.log(
            f"trying {name}:{entry['version']} beside {served['name']}:{served['version']} as {key}"
        )

    async def load_shadow_model(self, key: str, *, allow_code: bool = False) -> Any:
        """The shadow version under ``key``, loaded once; the served one for a key with no shadow."""
        entry = self._shadow_artifacts.get(key)
        if entry is None:
            return await self.load_model(key, allow_code=allow_code)
        if key not in self._shadow_loaded:
            self._shadow_loaded[key] = await asyncio.to_thread(
                models.load, entry, allow_code=allow_code
            )
        return self._shadow_loaded[key]

    async def call_shadow(self, payload: Any) -> Any:
        """Run the function on ``payload`` against the shadow version; see `ShadowView`."""
        view = ShadowView(self)
        args = (payload, view) if self.spec.wants_actor else (payload,)
        return await run_callable(self.spec.fn, *args)

    async def _shadow_pass(
        self, payload: Any, topic: str, active_result: Any, active_ms: float, arrived: float
    ) -> None:
        """The shadow's turn on one input, after the active answer went out; one pair per model on trial."""
        started = time.perf_counter()
        error: str | None = None
        result: Any = None
        try:
            try:
                result = await self.call_shadow(payload)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "[%s] %s() failed on the shadow pass: %s", self.name, self.spec.fn.__name__, exc
                )
            shadow_ms = now_ms(started)
            if result is not None and self.spec.publishes:
                await self.publish(shadow_topic(self.spec.publishes), result)
            self.shadow_passes += 1
            for key, entry in list(self._shadow_artifacts.items()):
                recorder = self._pairs.get(str(entry["name"]))
                if recorder is None:
                    continue
                row = pair_row(
                    payload=payload,
                    topic=topic,
                    arrived=arrived,
                    active_version=int(self._artifacts[key]["version"]),
                    shadow_version=int(entry["version"]),
                    active_output=active_result,
                    shadow_output=result,
                    active_ms=active_ms,
                    shadow_ms=shadow_ms,
                    shadow_error=error,
                )
                try:
                    unavailable = await recorder.record(row)
                except Exception as exc:
                    unavailable = f"pairs are not kept: {exc}"
                if unavailable and not self._said_pairs_unavailable:
                    self._said_pairs_unavailable = True
                    await self.log(unavailable, level="warning")
        finally:
            self._shadow_busy = False

    def _current_task_description(self) -> str:
        return self.spec.description or f"running {self.spec.fn.__name__}()"

    async def call(self, payload: Any) -> Any:
        """Run the function on ``payload``: on the loop when it is async, else on a thread.

        A plain function that hands back a coroutine -- an async function behind
        a plain decorator -- has that coroutine awaited, so its result is what
        is published, not the coroutine object.
        """
        args = (payload, self) if self.spec.wants_actor else (payload,)
        return await run_callable(self.spec.fn, *args)

    async def log(self, message: str, level: str = "info") -> None:
        """Say something on the dashboard feed, under this agent's name."""
        getattr(logger, level, logger.info)("[%s] %s", self.name, message)
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": message, "timestamp": time.time()},
        )

    def _listener(self, topic: str) -> Callable[[Any], Any]:
        """The callback for one topic: :meth:`_on_message`, knowing the topic for the feed."""

        async def on_message(payload: Any) -> None:
            await self._on_message(payload, topic)

        return on_message

    async def _on_message(self, payload: Any, topic: str = "") -> None:
        """A message on a subscribed topic: run the function, publish what it returns.

        Counted as a processed message, so the dashboard card shows the agent
        working; a subscription does not pass through the mailbox the base
        class counts. What is published is also said on the feed, since a data
        topic is not shown there and a detector that only publishes looks idle,
        and so is a failure, which the hub otherwise reports only in the log.
        """
        arrived = time.time()
        started = time.perf_counter()
        try:
            result = await self.call(payload)
        except Exception as exc:
            self.metrics.tasks_failed += 1
            await self.log(
                f"{self.spec.fn.__name__}() failed on {topic or 'a message'}: {exc}", level="error"
            )
            raise
        active_ms = now_ms(started)
        self.metrics.messages_processed += 1
        self.metrics.tasks_completed += 1
        if result is not None and self.spec.publishes:
            await self.publish(self.spec.publishes, result)
            await self.log(f"→ {self.spec.publishes}: {_summary(result)}")
        if not self._shadow_artifacts:
            return
        if self._shadow_busy:
            # Skipped, not queued: the trial never slows what is served.
            self.shadow_skipped += 1
            return
        # Taken here, before the loop turns, so the next message sees it.
        self._shadow_busy = True
        task = self.run_detached(
            self._shadow_pass(payload, topic, result, active_ms, arrived),
            name=f"{self.name}:shadow",
        )
        if task.cancelled():
            self._shadow_busy = False

    async def handle_message(self, msg: Message) -> None:
        """A task from chat or another agent: run the function, answer with the result.

        With ``concurrency`` above one the task runs beside the mailbox, so the
        next task is not held behind it; the semaphore keeps the number in
        flight to what the spec allows, and the actor owns the task, so a stop
        still waits for it.
        """
        if msg.type != MessageType.TASK:
            return
        if self.spec.concurrency > 1:
            self.run_detached(self._answer(msg), name=f"{self.name}:task")
            return
        await self._answer(msg)

    async def _answer(self, msg: Message) -> None:
        async with self._slots:
            await self._answer_now(msg)

    async def _answer_now(self, msg: Message) -> None:
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
