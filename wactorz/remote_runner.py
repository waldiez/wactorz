#!/usr/bin/env python3
"""remote_runner.py — Wactorz edge node runner.

Deploy this single file to any machine (Raspberry Pi, VM, edge device).
It connects to the shared MQTT broker, listens for spawn commands from main,
and runs DynamicAgents locally. Those agents heartbeat back to the same broker
so they appear in the central dashboard exactly like local agents.

Usage on the remote machine (a virtualenv keeps agent dependencies off the
host; without one the runner installs into the system interpreter and says so):
    python3 -m venv ~/wactorz/venv && . ~/wactorz/venv/bin/activate
    pip install aiomqtt paho-mqtt psutil aiohttp
    python3 remote_runner.py --broker 192.168.1.10 --name rpi-livingroom

From the main Wactorz chat (automatic, once the node is a configured deploy
target — see DEPLOY_TARGETS in .env.template):
    /deploy rpi-livingroom

Or manually in the chat spawn block:
    <spawn>
    {
      "name": "temp-sensor-agent",
      "node": "rpi-livingroom",
      "type": "dynamic",
      "description": "Reads temperature from DHT22 sensor",
      "poll_interval": 30,
      "max_restarts": 5,
      "restart_delay": 3.0,
      "code": "
        async def setup(agent):
            await agent.log('DHT22 sensor agent ready')

        async def process(agent):
            import random   # replace with real adafruit_dht read
            temp = round(20 + random.uniform(-2, 2), 1)
            await agent.publish('sensors/temperature', {'value': temp, 'unit': 'C'})
            await agent.log(f'Temperature: {temp}C')
      "
    }
    </spawn>

Architecture:
    [Main machine]                    [Raspberry Pi / Edge node]
    main_actor ──MQTT──► nodes/{name}/spawn ──► remote_runner.py
                                                  │ compiles + runs DynamicAgent
                                                  │ heartbeats via MQTT
    dashboard  ◄──MQTT── agents/{id}/heartbeat ◄──┘

The remote runner is intentionally self-contained — it reimplements just enough
of the Actor/DynamicAgent contract to run user code without needing the full
wactorz package installed on the edge device.

Each agent runs under a local supervisor (mirroring the main machine's OTP-style
ONE_FOR_ONE strategy). If an agent crashes, the supervisor restarts it with
exponential back-off (3s → 6s → 12s … capped at 60s). After max_restarts
consecutive failures the agent is marked failed and removed from the registry.
Compile errors and setup() fatals are never retried — broken code won't fix itself.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import importlib
import inspect
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import traceback
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Generator, Sequence
from pathlib import Path
from typing import Any

# aiomqtt, psutil and paho stay imported inside the functions that use them:
# _bootstrap_deps_async pip-installs them at runtime, so a node starting before
# that finishes must still be able to import this module.


def _missing_deps() -> list[str]:
    needed = []
    for module, pkg in [
        ("aiomqtt", "aiomqtt"),
        ("paho.mqtt.client", "paho-mqtt"),
        ("psutil", "psutil"),
    ]:
        try:
            importlib.import_module(module)
        except ImportError:
            needed.append(pkg)
    return needed


def _is_root() -> bool:
    """Whether this process may write where the system package manager keeps its files.

    Windows has no uid and the equivalent question is whether the token is
    elevated, which shell32 answers. A failure to reach it is read as "not
    privileged", so the install takes the contained path rather than assuming
    it may write anywhere.
    """
    if os.name == "nt":
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0  # pyright: ignore[reportAttributeAccessIssue]
        except Exception:
            return False
    return os.getuid() == 0


def _normalised_contract(
    publishes: str | list[str] | None,
    subscribes: str | list[str] | None,
    produces_schema: dict[str, Any] | None,
    consumes_schema: dict[str, Any] | None,
    kwargs: dict[str, Any],
) -> tuple[list[str], list[str], dict[str, Any], dict[str, Any]]:
    """Resolve the aliases generated agent code reaches for, and normalise types.

    Accepting them keeps a real contract from arriving empty because the author
    guessed `schema` where the parameter is `produces_schema`. A lone topic is
    widened to a list for the same reason: both spellings appear in practice.
    """
    if produces_schema is None:
        produces_schema = (
            kwargs.get("schema")
            or kwargs.get("output_schema")
            or kwargs.get("produce_schema")
            or {}
        )
    if consumes_schema is None:
        consumes_schema = kwargs.get("input_schema") or kwargs.get("consume_schema") or {}
    if publishes is None:
        publishes = kwargs.get("topics") or kwargs.get("publish")
    if subscribes is None:
        subscribes = kwargs.get("subscribe")
    if isinstance(publishes, str):
        publishes = [publishes]
    if isinstance(subscribes, str):
        subscribes = [subscribes]
    return (
        list(publishes or []),
        list(subscribes or []),
        produces_schema or {},
        consumes_schema or {},
    )


def _validated_callback(
    topic: str, callback: Callable[..., Awaitable[Any]] | None
) -> Callable[..., Awaitable[Any]]:
    """Return the callback, or refuse one that could not receive a payload.

    Checked when the subscription is declared rather than when the first
    message lands, so the author of a generated agent hears about it at
    start-up. The signature check sits outside the try that tolerates an
    un-inspectable callable: inside it, the very except meant to let such a
    callable through swallowed this error instead, and the check never fired.
    """
    if callback is None or not callable(callback):
        raise SubscribeCallbackError(topic, callback)
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback  # not introspectable — let the runtime catch it
    required = [p for p in sig.parameters.values() if p.default is inspect.Parameter.empty]
    if not required:
        raise SubscribeCallbackError(topic, callback)
    return callback


def _tolerant_invoker(
    callback: Callable[..., Awaitable[Any]], agent_name: str
) -> Callable[[Any], Awaitable[None]]:
    """Wrap a callback so a generated `await None` is reported once, not raised.

    The same protection DynamicAgent.subscribe() applies. Warned a single time
    per subscription, because the mistake repeats on every message and the log
    is the only place anyone would see it.
    """
    warned = False

    async def _invoke(payload: Any) -> None:
        nonlocal warned
        try:
            await callback(payload)
        except TypeError as e:
            if "NoneType" in str(e) and "await" in str(e):
                if not warned:
                    logger.warning(
                        "[%s] subscribe callback has 'await None' error (suppressed): %s",
                        agent_name,
                        e,
                    )
                    warned = True
            else:
                raise

    return _invoke


#: How long the broker keeps a node's session. Mirrors
#: SERVER_SESSION_EXPIRY_SECONDS in wactorz/core/mqtt.py.
NODE_SESSION_EXPIRY_SECONDS = 86400

#: How many messages may wait in memory before telemetry starts giving way.
#: Large enough that an ordinary reconnect queues and drains without losing
#: anything; small enough that an absent broker costs a Pi megabytes rather than
#: its memory. Mirrors MQTTPublisher.MAX_QUEUED on the server.
MAX_QUEUED = 10_000

#: Topics that are purely telemetry: the next one replaces the last, so the
#: oldest is what gives way when the queue is full. Mirrors
#: MQTTPublisher._TELEMETRY_TOPIC_SUFFIXES.
TELEMETRY_TOPIC_SUFFIXES = ("/logs", "/metrics", "/status", "/heartbeat")


def _new_pub_queue() -> asyncio.Queue[tuple[str, bytes, bool, bool]]:
    """The publish queue, bounded.

    Unbounded, a broker outage on a node that keeps publishing grows this until
    the machine runs out of memory -- and these run on Raspberry Pis. Built here
    rather than inline so the bound itself can be tested, instead of only the
    eviction that depends on it.
    """
    return asyncio.Queue(maxsize=MAX_QUEUED)


def _is_critical(topic: str) -> bool:
    """Whether losing this message would lose something the system needs.

    Everything that is not plain telemetry: results, errors, manifests, and the
    migration replies that decide where an agent lives.
    """
    return not topic.endswith(TELEMETRY_TOPIC_SUFFIXES)


def _session_kwargs(aiomqtt: Any, expiry_seconds: int) -> dict[str, Any]:
    """Connect arguments that hold this session for a bounded time.

    Mirrors `session_kwargs` in wactorz/core/mqtt.py; spelled out because this
    file is deployed to a node with no wactorz package beside it.
    """
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    properties = Properties(PacketTypes.CONNECT)
    properties.SessionExpiryInterval = expiry_seconds
    return {
        "protocol": aiomqtt.ProtocolVersion.V5,
        "clean_start": False,
        "properties": properties,
    }


def _topic_matches(pattern: str, topic: str) -> bool:
    """Match a topic against an MQTT filter with `#` and `+` wildcards.

    Mirrors `topic_matches` in wactorz/core/topic_bus.py. Spelled out rather
    than imported: this file is deployed to a node on its own, with no wactorz
    package beside it.
    """
    if pattern == topic:
        return True
    parts, actual = pattern.split("/"), topic.split("/")
    while True:
        if not parts and not actual:
            return True
        if parts and parts[0] == "#":
            return True
        if not parts or not actual:
            return False
        if parts[0] != "+" and parts[0] != actual[0]:
            return False
        parts, actual = parts[1:], actual[1:]


class _NodeBinding:
    """One topic filter on a node agent, and the queue that serialises it."""

    def __init__(self, topic: str, invoke: Callable[[Any], Awaitable[None]], maxsize: int) -> None:
        self.topic = topic
        self.invoke = invoke
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.worker: asyncio.Task | None = None
        self.dropped = 0

    def offer(self, payload: Any) -> None:
        """Queue a payload, discarding the oldest when the callback is behind."""
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped += 1
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self.queue.put_nowait(payload)


class _NodeSubscriptionHub:
    """Every subscription one node agent holds, on a single MQTT connection.

    The server-side twin of this is `SubscriptionHub` in
    wactorz/agents/dynamic/listener.py, and the reasoning is the same: an agent
    used to open a connection per `subscribe()`, agent code is model-authored,
    and nothing stopped a generated loop from opening as many as it liked.

    One worker per binding rather than a task per message, because the
    per-subscription connection this replaces awaited each callback -- a
    topic's messages were serialised, and generated code is stateful and not
    written to be re-entrant. The queue is bounded for the same reason the
    runner's publish queue should be: unbounded, it is just a backlog waiting
    for a broker outage.
    """

    RECONNECT_DELAY = 5.0
    QUEUE_MAX = 100
    #: How long the broker keeps a durable agent's session. Mirrors
    #: SubscriptionHub.SESSION_EXPIRY_SECONDS in the server-side twin.
    SESSION_EXPIRY_SECONDS = 3600

    def __init__(
        self, agent_name: str, actor_id: str, broker: str, port: int, durable: bool = False
    ) -> None:
        self._agent_name = agent_name
        #: Only an agent whose name was chosen can hold a session: an unnamed
        #: one is given a random name per spawn, so its id changes with it and a
        #: session kept for the old one could never be resumed.
        self._durable = durable
        self._actor_id = actor_id
        self._broker = broker
        self._port = port
        #: A list, not keyed by topic: two callbacks may watch the same filter,
        #: and keying by topic would silently drop the first.
        self._bindings: list[_NodeBinding] = []
        self._client: Any = None
        self._task: asyncio.Task | None = None

    def bind(self, topic: str, invoke: Callable[[Any], Awaitable[None]]) -> asyncio.Task | None:
        """Register a subscription; returns the hub task if this call started it."""
        binding = _NodeBinding(topic, invoke, self.QUEUE_MAX)
        self._bindings.append(binding)
        binding.worker = asyncio.create_task(self._drain(binding))
        if self._client is not None:
            asyncio.create_task(self._subscribe_now(topic))
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
            return self._task
        return None

    def _ensure_workers(self) -> None:
        """Give every binding a live worker, reviving any that were cancelled.

        Mirrors SubscriptionHub._ensure_workers: cancelling this task cancels
        the workers with it, and `bind` revives the task once it has ended, so
        a revived hub would otherwise re-subscribe and queue into queues nobody
        drains.
        """
        for binding in self._bindings:
            if binding.worker is None or binding.worker.done():
                binding.worker = asyncio.create_task(self._drain(binding))

    def _session_kwargs(self, aiomqtt: Any) -> dict[str, Any]:
        """Connect arguments that make the broker keep this session, or not."""
        if not self._durable:
            return {}
        return _session_kwargs(aiomqtt, self.SESSION_EXPIRY_SECONDS)

    async def _subscribe_now(self, topic: str) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.subscribe(topic, qos=1 if self._durable else 0)
        except Exception:
            logger.debug("[%s] Deferred subscribe of %s to reconnect", self._agent_name, topic)

    async def run(self) -> None:
        """Hold one connection open for every subscription, reconnecting for ever."""
        try:
            import aiomqtt
        except ImportError:
            logger.warning("[%s] aiomqtt not installed", self._agent_name)
            return
        while True:
            try:
                self._ensure_workers()
                async with aiomqtt.Client(
                    self._broker,
                    self._port,
                    username=os.environ.get("MQTT_USERNAME") or None,
                    password=os.environ.get("MQTT_PASSWORD") or None,
                    # Mirrors core/mqtt.py client_id.
                    identifier=f"wactorz-agent-{self._actor_id}",
                    **self._session_kwargs(aiomqtt),
                ) as client:
                    self._client = client
                    topics = list(dict.fromkeys(b.topic for b in self._bindings))
                    for topic in topics:
                        await client.subscribe(topic, qos=1 if self._durable else 0)
                    logger.info(
                        "[%s] Subscribed to %d topic(s) on one connection",
                        self._agent_name,
                        len(topics),
                    )
                    async for message in client.messages:
                        self._dispatch(message)
            except asyncio.CancelledError:
                self._client = None
                for binding in list(self._bindings):
                    if binding.worker is not None and not binding.worker.done():
                        binding.worker.cancel()
                break
            except Exception as e:
                self._client = None
                logger.warning(
                    "[%s] MQTT subscribe error: %s — retrying in %ss",
                    self._agent_name,
                    e,
                    self.RECONNECT_DELAY,
                )
                await asyncio.sleep(self.RECONNECT_DELAY)

    def _dispatch(self, message: Any) -> None:
        """Queue one message for every binding whose filter matches it."""
        topic = str(message.topic)
        try:
            payload = json.loads(message.payload.decode())
        except Exception:
            payload = {"raw": message.payload.decode()}
        for binding in list(self._bindings):
            if _topic_matches(binding.topic, topic):
                binding.offer(payload)

    async def _drain(self, binding: _NodeBinding) -> None:
        """Run one binding's callbacks, strictly one message at a time."""
        while True:
            payload = await binding.queue.get()
            try:
                await binding.invoke(payload)
            except Exception:
                logger.exception(
                    "[%s] subscribe callback error (topic=%s)", self._agent_name, binding.topic
                )
            finally:
                binding.queue.task_done()


def _close_mqtt_client(client: Any, what: str) -> None:
    """Stop and disconnect a paho client, whatever state it is in.

    Called on paths that are discarding the client either way, so a failure to
    close it changes nothing that follows and is recorded rather than raised.
    """
    try:
        client.loop_stop()
        client.disconnect()
    except Exception:
        logger.debug("[runner] %s did not close cleanly", what, exc_info=True)


def _in_virtualenv() -> bool:
    """Whether this interpreter is an environment of its own, not the system one.

    `real_prefix` is what the legacy virtualenv package set; everything since
    moves `prefix` away from `base_prefix`. Both are resolved before comparison,
    because a symlinked environment otherwise looks unequal to itself.
    """
    if hasattr(sys, "real_prefix"):  # pragma: no cover - legacy virtualenv only
        return True
    base = getattr(sys, "base_prefix", sys.prefix)
    return os.path.realpath(base) != os.path.realpath(sys.prefix)


def _pip_install_command(packages: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """A pip command for this node, and the environment it should run in.

    Ordered by how little of the host it disturbs. Inside a virtualenv nothing
    special is needed and nothing outside it is touched. Outside one, an
    unprivileged install is directed at the user's own site-packages, which
    leaves the distribution's tree alone; only root ends up writing where the
    system package manager expects to be in charge.

    A distribution may refuse either of those under PEP 668, and the override
    goes through the environment rather than `--break-system-packages` because
    no pip before 23.0.1 knows that argument — an edge node running an older one
    would fail on the flag instead of installing. It is passed to the child
    rather than set on this process, so two installs cannot race over it.
    """
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
    ]
    env: dict[str, str] = {}
    if not _in_virtualenv():
        if not _is_root():
            cmd.append("--user")
        env["PIP_BREAK_SYSTEM_PACKAGES"] = "1"
    cmd += [*packages, "-q"]
    return cmd, env


def _install_destination() -> str:
    """Where an install would land, for a log line that says so."""
    if _in_virtualenv():
        return f"the virtualenv at {sys.prefix}"
    if _is_root():
        return f"the system interpreter at {sys.executable}"
    return "this user's site-packages"


async def _bootstrap_deps_async(ready: asyncio.Event) -> None:
    """Install missing deps in a thread pool, then signal the event."""
    needed = _missing_deps()
    if not needed:
        ready.set()
        return
    logger.info("[runner] Auto-installing %s into %s.", needed, _install_destination())

    cmd, extra_env = _pip_install_command(needed)

    def _pip() -> tuple[int, str]:
        env = {**os.environ, **extra_env} if extra_env else None
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        return r.returncode, r.stderr[:300]

    loop = asyncio.get_running_loop()
    rc, err = await loop.run_in_executor(None, _pip)
    if rc != 0:
        logger.warning("[runner] pip reported: %s", err)
    else:
        logger.info("[runner] Dependencies installed.")
    importlib.invalidate_caches()
    ready.set()


class SubscribeCallbackError(TypeError):
    """A subscribe callback that cannot receive the payload it would be sent.

    Raised where the subscription is declared rather than when a message
    arrives, so the author of a generated agent hears about it at start-up
    instead of through an opaque failure on the first publish.
    """

    def __init__(self, topic: str, callback: object) -> None:
        if callback is None or not callable(callback):
            super().__init__(
                f"agent.subscribe('{topic}', callback) requires a callable callback. "
                f"Got: {type(callback).__name__}. "
                f"Define: async def on_msg(payload): ... then call "
                f"agent.subscribe('{topic}', on_msg). "
                f"For a one-shot read use: data = await agent.mqtt_get('{topic}')"
            )
        else:
            name = getattr(callback, "__name__", "on_msg")
            super().__init__(
                "Subscribe callback must accept one argument (the payload dict). "
                "Got a function with no required parameters. "
                f"Fix: async def {name}(payload): ..."
            )


class ProcessEscalated(RuntimeError):
    """process() failed often enough that a clean restart beats another attempt."""

    def __init__(self, consecutive: int, last: BaseException) -> None:
        super().__init__(f"process() failed {consecutive} times in a row, last error: {last}")


logger = logging.getLogger("remote_runner")


# ── What a pip package name may look like ─────────────────────────────────────
# A deliberate copy of `agents/installer_agent.py`'s rule, not an oversight:
# this module is deployed as a single file to machines where wactorz is not
# installed, so it cannot import it. Keep the two in step by hand; the module
# docstring's "deploy this single file" is what forbids the obvious fix.
#
# PEP 508 names are letters, digits, and `-`/`_`/`.` as internal separators,
# optionally followed by extras and version specifiers. Nothing else — no path,
# no URL, no whitespace, and no leading `-`, which is what makes an option an
# option. An allow-list because the set of legitimate names is describable and
# the set of harmful strings is not.
_PACKAGE_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9,._-]+\])?"
    r"(?:"
    r"(?:[=<>!~]=|[<>])[A-Za-z0-9._*+!-]+"
    r"(?:,(?:[=<>!~]=|[<>])[A-Za-z0-9._*+!-]+)*"
    r")?"
)


def _is_installable_name(package: str) -> bool:
    """Whether `package` is a package name and not an instruction to pip.

    Two exposures, one answer. The command below is now a *list*, so no shell
    reads it — but pip reads its own options from positional arguments, so
    `--index-url=http://…` or a bare URL is honoured as configuration rather
    than treated as a name. That is fetch-and-execute from an attacker-chosen
    index with no shell involved, and it arrives in a spawn payload off the
    broker.
    """
    candidate = (package or "").strip()
    return bool(candidate) and _PACKAGE_NAME.fullmatch(candidate) is not None


# ── Sentinel: awaitable None ──────────────────────────────────────────────────
# Mirror of dynamic.api.AwaitableNone. Returned from sync methods like
# subscribe() and declare_contract() so LLM-generated code that mistakenly
# writes `await agent.subscribe(...)` doesn't blow up.


class _AwaitableNone:
    def __await__(self) -> Generator[Any, None, None]:
        # Completes immediately with None; a generator rather than
        # `iter([])` so this actually types as Awaitable.
        yield from ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "None"


_AWAITABLE_NONE = _AwaitableNone()


# ── Minimal StreamWindow ──────────────────────────────────────────────────────
# Self-contained port of core.topic_bus.StreamWindow so agent.window() works on
# remote nodes without depending on the wactorz package. Kept intentionally
# small — only the methods agents actually call.


class _RemoteStreamWindow:
    """Sliding time window over an MQTT topic. Background task fills a buffer;
    queries are synchronous and operate on the in-memory buffer.
    """

    def __init__(
        self, topic: str, broker: str, port: int, seconds: float = 300, max_size: int = 1000
    ) -> None:

        self.topic = topic
        self.seconds = float(seconds)
        self.max_size = int(max_size)
        self._broker = broker
        self._port = port
        self._buffer: deque[dict] = deque(maxlen=self.max_size)
        self._task: asyncio.Task | None = None

    def start(self) -> _RemoteStreamWindow:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._listen())
        return self

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _listen(self) -> None:
        try:
            import aiomqtt
        except ImportError:
            logger.warning("[StreamWindow] aiomqtt not installed")
            return
        while True:
            try:
                async with aiomqtt.Client(
                    self._broker,
                    self._port,
                    username=os.environ.get("MQTT_USERNAME") or None,
                    password=os.environ.get("MQTT_PASSWORD") or None,
                ) as client:
                    await client.subscribe(self.topic)
                    async for msg in client.messages:
                        try:
                            payload = json.loads(msg.payload.decode())
                        except Exception:
                            payload = {"value": msg.payload.decode()}
                        if not isinstance(payload, dict):
                            payload = {"value": payload}
                        payload["_ts"] = time.time()  # pyright: ignore[reportArgumentType]
                        self._buffer.append(payload)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5)

    # ── Trimming + queries ────────────────────────────────────────────────────
    def _trim(self) -> None:
        cutoff = time.time() - self.seconds
        while self._buffer and self._buffer[0].get("_ts", 0) < cutoff:
            self._buffer.popleft()

    def values(self, key: str = "value") -> list[Any]:
        self._trim()
        return [e[key] for e in self._buffer if key in e]

    def count(self) -> int:
        self._trim()
        return len(self._buffer)

    def latest(self, key: str = "value") -> Any:
        self._trim()
        for e in reversed(self._buffer):
            if key in e:
                return e[key]
        return None

    def mean(self, key: str = "value") -> float | None:
        vs = [v for v in self.values(key) if isinstance(v, (int, float))]
        return sum(vs) / len(vs) if vs else None

    def min(self, key: str = "value") -> int | float | None:
        vs = [v for v in self.values(key) if isinstance(v, (int, float))]
        return min(vs) if vs else None

    def max(self, key: str = "value") -> int | float | None:
        vs = [v for v in self.values(key) if isinstance(v, (int, float))]
        return max(vs) if vs else None

    def rising(self, key: str = "value", threshold: float = 0.0) -> bool:
        vs = [v for v in self.values(key) if isinstance(v, (int, float))]
        return len(vs) >= 2 and (vs[-1] - vs[0]) > threshold

    def falling(self, key: str = "value", threshold: float = 0.0) -> bool:
        vs = [v for v in self.values(key) if isinstance(v, (int, float))]
        return len(vs) >= 2 and (vs[0] - vs[-1]) > threshold

    def stable(self, key: str = "value", tolerance: float = 0.0) -> bool:
        vs = [v for v in self.values(key) if isinstance(v, (int, float))]
        return bool(vs) and (max(vs) - min(vs)) <= tolerance

    def absent_for(self, seconds: float) -> bool:
        self._trim()
        if not self._buffer:
            return True
        return (time.time() - self._buffer[-1].get("_ts", 0)) >= seconds

    def event_count(
        self, key: str | None = None, value: Any = None, seconds: float | None = None
    ) -> int:
        self._trim()
        cutoff = time.time() - (seconds if seconds is not None else self.seconds)
        count = 0
        for e in self._buffer:
            if e.get("_ts", 0) < cutoff:
                continue
            if key is None or (key in e and (value is None or e[key] == value)):
                count += 1
        return count


# ── LLM namespace exposed as agent.llm ────────────────────────────────────────
# Mirror of dynamic.api.LLMInterface, but the actual LLM call happens on
# main via the existing main/llm_request bridge. This means:
#   - The same agent code (`agent.llm.chat(...)` / `agent.llm.complete(...)`)
#     works on both local and remote nodes — no migration breakage.
#   - The API key stays on main; the edge device never needs it.
#
# Cost tracking caveat: locally the LLMInterface increments the agent's
# token / cost counters from the LLM response's usage dict. The LLM bridge
# currently returns only {"text": ...} — usage is not propagated back, so
# remote LLM cost is currently attributed to main, not to the agent that
# spent it. Fixing that needs the bridge to ship usage in the reply; left
# as a follow-up so this fix stays minimal.


class _RemoteLLMInterface:
    """Drop-in equivalent of AgentAPI.llm on the remote side."""

    def __init__(self, api: _RemoteAgentAPI) -> None:
        self._api = api

    async def chat(self, prompt: str, system: str = "", timeout: float = 60.0) -> str:
        """Single-turn LLM call — same shape as local agent.llm.chat()."""
        return await self._api.ask_llm(prompt, system=system, timeout=timeout)

    async def complete(self, messages: list, system: str = "", timeout: float = 60.0) -> str:
        """Multi-turn LLM call — same shape as local agent.llm.complete().
        `messages` is a list of {role, content} dicts.
        """
        # Reuse the top-level chat() implementation (routes to main/llm_request
        # with a 'messages' field). The name collision is unfortunate — local
        # naming wins because agent code references agent.llm.complete.
        return await self._api.chat(messages, system=system, timeout=timeout)

    async def converse(self, user_message: str, system: str = "", timeout: float = 60.0) -> str:
        """Stateful multi-turn chat — mirrors local LLMInterface.converse().
        Maintains history in agent.state['_chat_history'].
        """
        history = self._api.state.setdefault("_chat_history", [])
        history.append({"role": "user", "content": user_message})
        reply = await self.complete(messages=history, system=system, timeout=timeout)
        history.append({"role": "assistant", "content": reply})
        return reply


# ── Minimal Actor API exposed to generated code ───────────────────────────────


class _RemoteAgentAPI:
    """Mirrors the agent API that DynamicAgent provides to generated code.
    All methods that touch MQTT go through the shared client.
    """

    def __init__(self, agent: _RemoteAgent) -> None:
        self._agent = agent
        self._published_topics: set = set()
        # Observed payload schemas captured from real publish() calls. Maps
        # topic → {"fields": {name: type_str}, "example": dict}. Mirrors the
        # local DynamicAgent behaviour so the planner sees real field names
        # for remote agents, not just LLM-declared guesses.
        self._observed_samples: dict[str, dict] = {}
        # (topic, id(callback)) → callback — dedup guard against double
        # subscribe() when setup() runs more than once (e.g. on reconnect).
        # A dict rather than a set of keys: id() is unique only among *live*
        # objects, so holding the callback is what stops its address being
        # recycled by a later one and that subscription silently skipped.
        self._subscribed_topics: dict[str | tuple[str, int], Any] = {}
        # Background subscriber tasks, kept so they can be cancelled on stop()
        # and not garbage-collected while running.
        self._subscriber_tasks: list[asyncio.Task] = []
        # Declared contract surface (subscribes / triggers_when / schemas)
        # populated by declare_contract(). Folded into the manifest by
        # _publish_manifest() so main can register a complete TopicContract.
        self._declared_subscribes: list[str] = []
        self._declared_triggers_when: dict[str, Any] = {}
        self._declared_produces_schema: dict[str, Any] = {}
        self._declared_consumes_schema: dict[str, Any] = {}
        # Active stream windows by topic, so window() is idempotent per topic
        # and tasks are reachable for shutdown.
        self._windows: dict[str, _RemoteStreamWindow] = {}
        # Shared mutable namespace exposed as agent.state to user code (mirrors
        # dynamic.api.AgentAPI.state). The remote runner historically pointed
        # this at the agent's _state dict via a @property — keep that working.
        # LLM namespace — exposed as agent.llm.chat / .complete / .converse so
        # the SAME agent code that uses agent.llm on a local DynamicAgent works
        # unchanged on a remote node. Routes to main via the LLM bridge.
        self.llm = _RemoteLLMInterface(self)

    # ── Identity ──────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return self._agent.name

    @property
    def actor_id(self) -> str:
        return self._agent.actor_id

    @property
    def state(self) -> dict:
        return self._agent._state

    @property
    def node(self) -> str:
        return self._agent.node_name

    # ── MQTT ──────────────────────────────────────────────────────────────────
    async def publish(self, topic: str, data: Any) -> None:
        await self._agent._publish(topic, data)
        is_new_topic = topic not in self._published_topics
        # Capture observed payload schema (field names + Python type names) so
        # the planner gets the SAME accuracy for remote agents that it gets
        # for local ones via DynamicAgent.publish().
        schema_changed = False
        if isinstance(data, dict):
            new_fields = {k: type(v).__name__ for k, v in data.items()}
            prev = self._observed_samples.get(topic, {}).get("fields", {})
            if new_fields != prev:
                self._observed_samples[topic] = {
                    "fields": new_fields,
                    "example": {k: data[k] for k in list(data)[:8]},  # bound size
                }
                schema_changed = True
        if is_new_topic:
            self._published_topics.add(topic)
        if is_new_topic or schema_changed:
            await self._publish_manifest()

    async def _publish_manifest(self) -> None:
        """Advertise this agent's full topic contract so main can register it
        with the TopicBus and the planner can auto-wire it correctly.

        The shape matches DynamicAgent._publish_manifest() exactly, so main's
        _manifest_listener can treat local and remote agents uniformly.
        """
        cfg = self._agent._config
        # Merge declared values (from declare_contract / subscribe) with the
        # spawn config, so the manifest reflects everything the running code
        # has actually wired up — not just what was requested at spawn time.
        subscribes = sorted(set(self._declared_subscribes) | set(cfg.get("subscribes", []) or []))
        triggers_when = {**(cfg.get("triggers_when", {}) or {}), **self._declared_triggers_when}
        produces_schema = {
            **(cfg.get("produces_schema", cfg.get("output_schema", {})) or {}),
            **self._declared_produces_schema,
        }
        consumes_schema = {
            **(cfg.get("consumes_schema", cfg.get("input_schema", {})) or {}),
            **self._declared_consumes_schema,
        }
        manifest = {
            "name": self.name,
            "actor_id": self.actor_id,
            "node": self.node,
            "description": cfg.get("description", ""),
            "capabilities": cfg.get("capabilities", []),
            "input_schema": cfg.get("input_schema", {}),
            "output_schema": cfg.get("output_schema", {}),
            # ── TopicContract surface ────────────────────────────────────
            # publishes is authoritative — driven by real publish() calls,
            # merged with anything pre-declared in the spawn config so the
            # planner sees pre-declared topics even before the first publish.
            "publishes": sorted(set(self._published_topics) | set(cfg.get("publishes", []) or [])),
            "subscribes": subscribes,
            "triggers_when": triggers_when,
            "produces_schema": produces_schema,
            "consumes_schema": consumes_schema,
            # ── Observed payload schemas (auto-captured) ─────────────────
            "observed_samples": dict(self._observed_samples),
            "timestamp": time.time(),
        }
        await self._agent._runner.publish(f"agents/{self.actor_id}/manifest", manifest, retain=True)

    async def publish_result(self, data: Any) -> None:
        """Publish agent result to agents/{id}/results — mirrors DynamicAgent API."""
        await self._agent._publish(
            f"agents/{self.actor_id}/results",
            {"agent": self.name, "node": self.node, "result": data, "timestamp": time.time()},
        )

    async def publish_detection(self, data: Any) -> None:
        """Publish detection results to agents/{id}/detections — mirrors DynamicAgent API."""
        await self._agent._publish(
            f"agents/{self.actor_id}/detections",
            {"agent": self.name, "node": self.node, "detections": data, "timestamp": time.time()},
        )
        # Also publish to a human-friendly topic for easy MQTT subscription
        await self.publish(f"{self.node}/{self.name}/detections", data)

    # ── Subscriptions ─────────────────────────────────────────────────────────
    def subscribe(
        self, topic: str, callback: Callable[..., Awaitable[Any]] | None
    ) -> _AwaitableNone:
        """Subscribe to an MQTT topic and call callback(payload_dict) for each
        message. Runs as a background task — setup() returns immediately.

        IMPORTANT: callback is REQUIRED and must be an async function.
        subscribe() is NOT awaitable and does NOT return data.
        For a one-shot read use: data = await agent.mqtt_get(topic)

        Mirrors dynamic.api.AgentAPI.subscribe(). The remote node has no
        TopicBus, so the subscription is also recorded on the API so that the
        next _publish_manifest() includes it — main then registers it on the
        central TopicBus and the planner can wire it.
        """
        checked = _validated_callback(topic, callback)

        # Dedup — same topic+callback pair only registers one listener.
        sub_key = (topic, id(callback))
        if sub_key in self._subscribed_topics:
            logger.debug("[%s] Already subscribed to %s — skipping duplicate", self.name, topic)
            return _AWAITABLE_NONE
        self._subscribed_topics[sub_key] = checked

        # One connection per agent carries every subscription, so only the first
        # bind starts a task.
        task = self._hub().bind(topic, _tolerant_invoker(checked, self.name))
        if task is not None:
            self._subscriber_tasks.append(task)
            # Also let the agent's task list see it so stop() cancels cleanly.
            try:
                self._agent._tasks.append(task)
            except Exception:
                # Without this the task still runs; it just will not be cancelled
                # by stop(), which is worth knowing about but not worth failing
                # over.
                logger.debug(
                    "[%s] Could not register listener task", self._agent.name, exc_info=True
                )

        # Record the subscription on the contract surface and re-publish the
        # manifest so main learns about it and updates the central TopicBus.
        if topic not in self._declared_subscribes:
            self._declared_subscribes.append(topic)
            asyncio.create_task(self._publish_manifest())

        # Return an awaitable no-op so `await agent.subscribe(...)` doesn't crash.
        return _AWAITABLE_NONE

    def _hub(self) -> _NodeSubscriptionHub:
        """This agent's subscription hub, created on first subscribe."""
        hub = getattr(self, "_sub_hub", None)
        if hub is None:
            runner = self._agent._runner
            hub = _NodeSubscriptionHub(
                self.name,
                str(self.actor_id),
                runner.broker,
                runner.port,
                # A spawn config without a name gets a random one, so the id is
                # stable only within an incarnation -- nothing to resume.
                durable=bool(self._agent._config.get("name")),
            )
            self._sub_hub = hub
        return hub

    # ── One-shot reads / time windows / world state ──────────────────────────
    async def mqtt_get(self, topic: str, timeout: float = 10.0) -> Any:
        """Wait for one MQTT message on topic and return its parsed payload.
        Useful for reading retained world-state topics or one-off queries.
        Returns None on timeout.
        """
        try:
            import aiomqtt
        except ImportError:
            return None
        broker = self._agent._runner.broker
        port = self._agent._runner.port
        result: list = []

        async def _fetch() -> None:
            try:
                async with aiomqtt.Client(
                    broker,
                    port,
                    username=os.environ.get("MQTT_USERNAME") or None,
                    password=os.environ.get("MQTT_PASSWORD") or None,
                ) as client:
                    await client.subscribe(topic)
                    async for msg in client.messages:
                        try:
                            result.append(json.loads(msg.payload.decode()))
                        except Exception:
                            result.append(msg.payload.decode())
                        return
            except Exception:
                logger.debug("[mqtt_get] Read of %s failed", topic, exc_info=True)

        try:
            await asyncio.wait_for(_fetch(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return result[0] if result else None

    def window(self, topic: str, seconds: float = 300, max_size: int = 1000) -> _RemoteStreamWindow:
        """Create a sliding time window over an MQTT topic stream.

        IMPORTANT: window() is synchronous — do NOT use await.
        Correct:  agent.state['w'] = agent.window('sensors/temp', seconds=60)

        Returns a window with methods: mean, min, max, rising, falling,
        stable, absent_for, event_count, latest, count, values.
        """
        # Idempotent per topic — repeated calls return the existing window
        # rather than spawning duplicate listeners.
        existing = self._windows.get(topic)
        if existing is not None:
            return existing
        broker = self._agent._runner.broker
        port = self._agent._runner.port
        w = _RemoteStreamWindow(topic, broker, port, seconds=seconds, max_size=max_size)
        w.start()
        self._windows[topic] = w
        return w

    async def publish_world_state(self, key: str, data: Any, retain: bool = True) -> None:
        """Publish a piece of world state to the shared retained state hub.
        Other agents can read this without making a request — it's always there.

        Topic: agents/{agent_name}/data/{key}
        """
        await self.publish(f"agents/{self.name}/data/{key}", data)

    async def read_world_state(self, topic: str, timeout: float = 2.0) -> Any | None:
        """Read a retained world-state topic — returns the cached value if the
        broker has one, otherwise waits up to `timeout` seconds for it.
        """
        return await self.mqtt_get(topic, timeout=timeout)

    # ── Topic contract declaration ────────────────────────────────────────────
    def declare_contract(
        self,
        publishes: str | list[str] | None = None,
        subscribes: str | list[str] | None = None,
        triggers_when: dict[str, Any] | None = None,
        produces_schema: dict[str, Any] | None = None,
        consumes_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> _AwaitableNone:
        """Declare this agent's topic contract — what it produces and consumes.

        Call from setup() to make this agent discoverable by the planner and
        other agents via topic-based auto-wiring. Same signature and aliases
        as dynamic.api.AgentAPI.declare_contract().

        On a remote node there's no local TopicBus, so the declared values are
        stored on the API and folded into the next _publish_manifest() — main
        then registers a complete TopicContract on the central bus.
        """
        pubs, subs, produces, consumes = _normalised_contract(
            publishes, subscribes, produces_schema, consumes_schema, kwargs
        )

        # Fold declared values into our tracking — _publish_manifest() picks
        # them up next time it fires.
        self._published_topics.update(pubs)
        for t in subs:
            if t not in self._declared_subscribes:
                self._declared_subscribes.append(t)
        if triggers_when:
            self._declared_triggers_when.update(triggers_when)
        if produces:
            self._declared_produces_schema.update(produces)
        if consumes:
            self._declared_consumes_schema.update(consumes)

        asyncio.create_task(self._publish_manifest())
        # Safe to await — return an awaitable sentinel because LLM code often
        # writes `await agent.declare_contract(...)`.
        return _AWAITABLE_NONE

    def wiring_opportunities(self) -> list[dict[str, Any]]:
        """Remote agents can't query the central TopicBus directly — that runs in
        the main process. Returns an empty list. Use `/agents` from main or
        ask the planner if you need wiring info.
        """
        return []

    # ── Introspection ─────────────────────────────────────────────────────────
    # These mirror the LOCAL helpers' shape but only see what's reachable from
    # this remote node. Cross-cluster introspection lives on main; remote code
    # that needs the global view should send a task there.

    def nodes(self) -> list[dict[str, Any]]:
        """List of nodes visible to this remote runner — only itself."""
        return [
            {
                "node": self.node,
                "online": True,
                "agents": [a.name for a in self._agent._runner._agents.values()],
            }
        ]

    def topics(self, keyword: str = "") -> list[dict[str, Any]]:
        """Topics this remote node has observed locally — built from its own
        published topics and the topics it actively subscribes to. The
        cluster-wide view lives on main; this is the best a remote node can
        do without an RPC round-trip.
        """
        seen: set = set(self._published_topics) | set(self._declared_subscribes)
        kw = keyword.lower().strip()
        out = []
        for t in sorted(seen):
            if kw and kw not in t.lower():
                continue
            out.append({"topic": t, "agents": [{"name": self.name, "node": self.node}]})
        return out

    def capabilities(self, keyword: str = "") -> list[dict[str, Any]]:
        """Single-element list describing this agent's own capability profile.
        Cluster-wide capability search lives on main.
        """
        cfg = self._agent._config
        desc = cfg.get("description", "")
        kw = keyword.lower().strip()
        if kw and kw not in desc.lower() and kw not in self.name.lower():
            return []
        return [
            {
                "name": self.name,
                "description": desc,
                "capabilities": cfg.get("capabilities", []),
                "input_schema": cfg.get("input_schema", {}),
                "output_schema": cfg.get("output_schema", {}),
            }
        ]

    # ── Logger shim ───────────────────────────────────────────────────────────
    @property
    def logger(self) -> Any:
        """Compatibility shim — allows agent.logger.info/warning/error in
        generated code, mirroring dynamic.api.AgentAPI.logger.
        """
        api = self

        class _LoggerShim:
            def info(self, msg: str) -> None:
                asyncio.ensure_future(api.log(msg, "info"))

            def warning(self, msg: str) -> None:
                asyncio.ensure_future(api.log(msg, "warning"))

            def error(self, msg: str) -> None:
                asyncio.ensure_future(api.log(msg, "error"))

            def debug(self, msg: str) -> None:
                asyncio.ensure_future(api.log(msg, "debug"))

        return _LoggerShim()

    async def set_status(self, status: str) -> None:
        """Update agent task status string visible in dashboard."""
        self._agent._status = status

    # ── Logging ───────────────────────────────────────────────────────────────
    async def log(self, message: str, level: str = "info") -> None:
        """Add a message to the event log. Signature mirrors DynamicAgent.log()
        so generated code that passes `level=` works on both local and remote.
        """
        # Encode safely for terminals that can't handle all unicode
        safe_msg = str(message).encode("ascii", errors="replace").decode("ascii")
        getattr(logger, level, logger.info)(f"[{self.name}] {safe_msg}")
        await self._agent._publish(
            f"agents/{self.actor_id}/logs",
            {
                "type": "log",
                "message": message,
                "agent": self.name,
                "level": level,
                "timestamp": time.time(),
            },
        )

    async def alert(self, message: str, severity: str = "warning") -> None:
        logger.warning("[%s] ALERT(%s): %s", self.name, severity, message)
        await self._agent._publish(
            f"agents/{self.actor_id}/alert",
            {
                "message": message,
                "severity": severity,
                "agent": self.name,
                "timestamp": time.time(),
            },
        )

    # ── Persistence ───────────────────────────────────────────────────────────
    def persist(self, key: str, value: Any) -> None:
        self._agent._persistent_state[key] = value
        self._agent._save_state()

    def recall(self, key: str, default: Any = None) -> Any:
        return self._agent._persistent_state.get(key, default)

    # ── LLM access (routed back to main node — API key stays there) ──────────
    async def ask_llm(self, prompt: str, system: str = "", timeout: float = 60.0) -> str:
        """Send a prompt to the LLM via the main node's LLM bridge.
        The API key never needs to be on the edge device — main handles the call
        and returns the text response over MQTT.

        Usage in agent code:
            reply = await agent.ask_llm("Summarise this reading: 42.3C")
            reply = await agent.ask_llm("Is this anomalous?", system="You are a sensor analyst.")
        """
        reply_topic = f"nodes/{self._agent.node_name}/reply/{uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._agent._pending_replies[reply_topic] = future

        await self._agent._publish(
            "main/llm_request",
            {
                "prompt": prompt,
                "system": system,
                "_reply_topic": reply_topic,
                "agent": self.name,
                "node": self.node,
            },
        )

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result.get("text", "") if isinstance(result, dict) else str(result)
        except asyncio.TimeoutError:
            logger.warning("[%s] ask_llm timed out after %ss", self.name, timeout)
            return ""
        finally:
            self._agent._pending_replies.pop(reply_topic, None)

    async def chat(self, messages: list, system: str = "", timeout: float = 60.0) -> str:
        """Multi-turn LLM call. messages is a list of {"role": "user"/"assistant", "content": "..."}.
        Useful for conversational agents that maintain their own history.
        """
        reply_topic = f"nodes/{self._agent.node_name}/reply/{uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._agent._pending_replies[reply_topic] = future

        await self._agent._publish(
            "main/llm_request",
            {
                "messages": messages,
                "system": system,
                "_reply_topic": reply_topic,
                "agent": self.name,
                "node": self.node,
            },
        )

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result.get("text", "") if isinstance(result, dict) else str(result)
        except asyncio.TimeoutError:
            logger.warning("[%s] chat() timed out after %ss", self.name, timeout)
            return ""
        finally:
            self._agent._pending_replies.pop(reply_topic, None)

    async def send_to(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Any:
        """Send a task to any agent (local or remote) via MQTT and wait for reply.
        Uses a reply-to topic unique to this call so responses can be correlated.
        """
        reply_topic = f"nodes/{self._agent.node_name}/reply/{uuid.uuid4().hex[:8]}"
        request = {
            "_remote_task": True,
            "_reply_topic": reply_topic,
            "payload": payload,
        }
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._agent._pending_replies[reply_topic] = future

        await self._agent._publish(f"agents/by-name/{agent_name}/task", request)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[%s] send_to '%s' timed out", self.name, agent_name)
            return None
        finally:
            self._agent._pending_replies.pop(reply_topic, None)

    # Alias used in DynamicAgent code
    async def delegate(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Any:
        return await self.send_to(agent_name, payload, timeout)

    def agents(self) -> list[dict[str, Any]]:
        """Return list of known agents on this node."""
        return [
            {"name": a.name, "actor_id": a.actor_id, "node": a.node_name}
            for a in self._agent._runner._agents.values()
        ]


# ── Remote agent (lightweight DynamicAgent equivalent) ───────────────────────


class _RemoteAgent:
    """Lightweight equivalent of DynamicAgent that runs on the edge node.
    Holds compiled user code and drives setup/process/handle_task.
    """

    def __init__(self, config: dict, runner: _RemoteRunner, state_dir: str | None = None) -> None:
        self.name = config.get("name", f"remote-agent-{uuid.uuid4().hex[:6]}")
        # Mirrors core/actor.py `derive_actor_id`. The one copy that has to
        # stay: this file is deployed to a node alone. If the two drift, nothing
        # raises -- main and the node simply disagree about which agent is which.
        self.actor_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.actor.{self.name}"))
        self.node_name = runner.node_name
        self._runner = runner
        self._config = config
        self._code = config.get("code", "")
        self._poll = float(config.get("poll_interval", 5.0))
        self._ns: dict = {}  # shared namespace for user code
        self._state: dict = {}  # agent.state dict for user code
        self._persistent_state: dict = {}
        # P3: use the runner's persistent state directory (~/wactorz/state/) so
        #     state survives Pi reboots rather than being wiped from /tmp.
        safe_name = self.name.replace("/", "_").replace("\\", "_")
        if not state_dir:
            state_path = Path.home() / "wactorz" / "state"
            state_dir = str(state_path)
        self._state_path: Path = Path(state_dir) / f"{safe_name}_state.json"
        self._pending_replies: dict[str, asyncio.Future] = {}
        self._api = _RemoteAgentAPI(self)
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._status = ""

        self._fn_setup: Callable[..., Awaitable[None]] | None = None
        self._fn_process: Callable[..., Awaitable[None]] | None = None
        self._fn_handle_task: Callable[..., Awaitable[None]] | None = None

        # ── Supervisor state ──────────────────────────────────────────────────
        self._max_restarts = int(config.get("max_restarts", 5))
        self._restart_delay = float(config.get("restart_delay", 3.0))
        self._restart_count = 0
        self._failed = False  # True = budget exhausted, do not restart

        # Migration / restart state handling.
        #
        # Two cases need to be distinguished:
        #
        #   1. Plain start or restart (no `_initial_state` in config).
        #      The agent ran here before and was restarted in place. We must
        #      preserve whatever is on disk so counters, calibration, chat
        #      history etc. survive the restart.
        #
        #   2. Migration arrival (`_initial_state` IS in config).
        #      The source node has just shipped this agent's authoritative
        #      state via MQTT. Any state file we find on disk is from a
        #      PREVIOUS incarnation that lived here before being migrated
        #      away — it is stale by definition, because the source node was
        #      the one running the agent moments ago. Letting the stale file
        #      win produces "ghost memory" and duplicate conversation entries
        #      when the user migrates an agent back to a node it once lived on.
        #
        # The fix is to make migration explicit: when `_initial_state` is
        # present, treat it as ground truth and overwrite any stale local
        # file. When it's not, fall back to loading from disk as before.
        initial = config.pop("_initial_state", None)
        if initial and isinstance(initial, dict):
            # Migration arrival path — initial state wins, period.
            if self._state_path.exists():
                logger.info(
                    "[%s] Migration: overwriting stale local state file at %s with %s key(s) shipped from the source node",
                    self.name,
                    self._state_path,
                    len(initial),
                )
                try:
                    self._state_path.unlink()
                except Exception as e:
                    logger.warning(
                        "[%s] Could not remove stale state file before applying migration snapshot: %s",
                        self.name,
                        e,
                    )
            self._persistent_state = dict(initial)
            self._save_state()
            logger.info(
                "[%s] Restored %s state key(s) from migration: %s",
                self.name,
                len(initial),
                list(initial.keys()),
            )
        else:
            # Normal start/restart — pick up whatever is already on disk.
            self._load_state()

    # ── State persistence (JSON, not pickle — portable across Python versions) ─

    def _save_state(self) -> None:
        try:
            with self._state_path.open("w", encoding="utf-8") as f:
                json.dump(self._persistent_state, f)
        except Exception as e:
            logger.warning("[%s] State save failed: %s", self.name, e)

    def _load_state(self) -> None:
        """Read the agent's state, or start empty if it cannot be read.

        A file that will not parse is moved aside rather than left in place: the
        next _save_state would write straight over it, so the only copy of
        whatever the agent remembered would be gone. This used to swallow the
        exception entirely, leaving no record anywhere that anything was lost.

        Deliberately not shared with wactorz.core.atomic_io — this module runs
        standalone on a remote node with nothing but the stdlib.
        """
        if not self._state_path.exists():
            return
        try:
            with self._state_path.open(encoding="utf-8") as f:
                self._persistent_state = json.load(f)
            logger.info("[%s] Loaded persistent state.", self.name)
        except Exception:
            kept = f"{self._state_path}.corrupt.{int(time.time())}"
            try:
                self._state_path.replace(kept)
            except Exception:
                kept = ""
            preserved = f"kept at {kept}" if kept else "the file could not be preserved"
            logger.exception("[%s] State load failed — %s", self.name, preserved)

    def _delete_state(self) -> bool:
        """Permanently remove the agent's on-disk JSON state file.

        This is what makes a delete (vs a stop) actually irreversible — without
        it, the next runner startup would reload state.json via _load_state()
        and the agent's full state would silently come back even after the
        spawn registry entry was cleared.

        Also wipes in-memory state so any post-stop publishes can't accidentally
        rewrite the file. Returns True iff the file existed and was removed.
        """
        # Wipe in-memory state first so a late _save_state() call (e.g. from
        # the supervisor loop) can't re-create the file we're about to delete.
        self._persistent_state = {}
        removed = False
        try:
            if self._state_path.exists():
                self._state_path.unlink()
                removed = True
                logger.info("[%s] Deleted persistent state file: %s", self.name, self._state_path)
        except Exception as e:
            logger.warning(
                "[%s] Failed to delete state file %s: %s", self.name, self._state_path, e
            )
        return removed

    # ── MQTT publish helper ───────────────────────────────────────────────────

    async def _publish(self, topic: str, data: Any) -> None:
        await self._runner.publish(topic, data)

    # ── Code compilation ──────────────────────────────────────────────────────

    def _compile(self) -> str | None:
        """Compile user code into self._ns. Returns error string or None."""
        try:
            exec(compile(self._code, f"<{self.name}>", "exec"), self._ns)
            self._fn_setup = self._ns.get("setup")
            self._fn_process = self._ns.get("process")
            self._fn_handle_task = self._ns.get("handle_task")
        except Exception as e:
            return f"Compile error: {e}\n{traceback.format_exc()}"
        else:
            return None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the agent under a supervision loop.
        The supervisor restarts the agent on unexpected crashes up to
        max_restarts times, with exponential back-off between attempts.
        Compile errors and deliberate stop() calls are never retried.
        """
        self._running = True
        asyncio.create_task(self._supervisor_loop())

    async def _supervisor_loop(self) -> None:
        """Supervisor that mirrors the local OTP ONE_FOR_ONE strategy.
        Runs _run_lifecycle() in a loop; on crash, waits and retries.
        """
        while self._running and not self._failed:
            try:
                await self._run_lifecycle()
            except asyncio.CancelledError:
                break  # deliberate stop() — do not restart
            except Exception as e:
                if not self._running:
                    break  # stop() was called mid-crash, don't restart

                self._restart_count += 1
                if self._restart_count > self._max_restarts:
                    self._failed = True
                    logger.exception(
                        "[%s] Crashed %s times — giving up (max_restarts=%s).",
                        self.name,
                        self._restart_count,
                        self._max_restarts,
                    )
                    await self._publish(
                        f"agents/{self.actor_id}/errors",
                        {
                            "phase": "supervisor",
                            "severity": "fatal",
                            "error": f"Restart budget exhausted after {self._restart_count} crashes: {e}",
                            "restart_count": self._restart_count,
                            "agent": self.name,
                            "timestamp": time.time(),
                        },
                    )
                    await self._publish_heartbeat("failed")
                    # Remove from runner registry so /nodes shows it as gone
                    self._runner._agents.pop(self.name, None)
                    break

                delay = min(self._restart_delay * (2 ** (self._restart_count - 1)), 60.0)
                logger.warning(
                    "[%s] Crashed (attempt %s/%s). Restarting in %.1fs...",
                    self.name,
                    self._restart_count,
                    self._max_restarts,
                    delay,
                )
                await self._publish(
                    f"agents/{self.actor_id}/errors",
                    {
                        "phase": "supervisor",
                        "severity": "warning",
                        "error": f"Agent crashed, restarting in {delay:.1f}s (attempt "
                        f"{self._restart_count}/{self._max_restarts}): {e}",
                        "restart_count": self._restart_count,
                        "agent": self.name,
                        "timestamp": time.time(),
                    },
                )
                await self._publish_heartbeat("restarting")
                # Cancel any leftover tasks from the crashed run
                for t in self._tasks:
                    t.cancel()
                self._tasks.clear()
                await asyncio.sleep(delay)
                # Re-compile fresh (code doesn't change, but namespace must be clean)
                self._ns = {}

    async def _run_lifecycle(self) -> None:
        """One full agent lifecycle: compile → setup → process loop + heartbeat loop.
        Raises on unhandled exceptions so _supervisor_loop can catch and restart.
        Compile errors and setup fatals publish an error event then return cleanly
        (no restart — broken code won't fix itself on retry).
        """
        # Reset per-run namespace and function pointers
        self._ns = {}
        self._fn_setup = None
        self._fn_process = None
        self._fn_handle_task = None

        err = self._compile()
        if err:
            logger.error("[%s] %s", self.name, err)
            await self._publish(
                f"agents/{self.actor_id}/errors",
                {
                    "phase": "compile",
                    "severity": "fatal",
                    "error": err,
                    "agent": self.name,
                    "timestamp": time.time(),
                },
            )
            self._running = False  # compile error → stop supervising
            return

        await self._publish_heartbeat("running")

        if self._fn_setup:
            try:
                await self._fn_setup(self._api)  # pyright: ignore[reportGeneralTypeIssues]  # _compile() populates this
                logger.info("[%s] setup() completed.", self.name)
            except Exception as e:
                err_str = traceback.format_exc()
                logger.exception("[%s] setup() failed", self.name)
                await self._publish(
                    f"agents/{self.actor_id}/errors",
                    {
                        "phase": "setup",
                        "severity": "fatal",
                        "error": str(e),
                        "traceback": err_str,
                        "agent": self.name,
                        "timestamp": time.time(),
                    },
                )
                self._running = False  # setup fatal → stop supervising
                return

        inner_tasks = []
        if self._fn_process:
            inner_tasks.append(asyncio.create_task(self._process_loop()))
        inner_tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._tasks = inner_tasks

        # Publish manifest immediately so main knows this remote agent exists
        # even before it calls publish() on any data topic
        await self._api._publish_manifest()

        # Wait for any task to finish (process escalation OR deliberate stop/cancel).
        # We use first-exception semantics: as soon as one task raises, cancel the rest.
        done, pending = await asyncio.wait(inner_tasks, return_when=asyncio.FIRST_EXCEPTION)
        # Cancel any still-running tasks (e.g. _heartbeat_loop after process escalation)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Re-raise any non-cancellation exception so the supervisor can restart
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        # Stop any stream windows the agent started — their background MQTT
        # listeners would otherwise outlive the agent and keep the broker
        # connection open.
        try:
            for w in list(self._api._windows.values()):
                w.stop()
            self._api._windows.clear()
        except Exception:
            logger.debug("[%s] Stream windows did not close cleanly", self.name, exc_info=True)
        self._save_state()
        await self._publish_heartbeat("stopped")
        logger.info("[%s] Stopped.", self.name)

    # ── Loops ─────────────────────────────────────────────────────────────────

    # After this many consecutive process() errors, raise to trigger a supervisor restart
    _PROCESS_ESCALATE_AFTER: int = 5

    async def _process_loop(self) -> None:
        """Run process() in a loop with per-error backoff.
        After _PROCESS_ESCALATE_AFTER consecutive errors, raises RuntimeError
        so the supervisor loop gets a clean restart (fresh namespace, reset state).
        A single successful call resets the consecutive counter.
        """
        consecutive_errors = 0
        successful_runs = 0
        while self._running:
            if self._fn_process is None:
                continue
            try:
                await self._fn_process(self._api)
                consecutive_errors = 0
                successful_runs += 1
                # After sustained healthy operation, credit back one restart token
                if successful_runs >= 10:
                    successful_runs = 0
                    if self._restart_count > 0:
                        self._restart_count -= 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                successful_runs = 0
                err_str = traceback.format_exc()
                severity = "critical" if consecutive_errors >= 3 else "warning"
                logger.exception("[%s] process() error #%s", self.name, consecutive_errors)
                await self._publish(
                    f"agents/{self.actor_id}/errors",
                    {
                        "phase": "process",
                        "severity": severity,
                        "error": str(e),
                        "consecutive": consecutive_errors,
                        "traceback": err_str[:800],
                        "agent": self.name,
                        "timestamp": time.time(),
                    },
                )
                if consecutive_errors >= self._PROCESS_ESCALATE_AFTER:
                    # Too many consecutive failures — let supervisor restart with clean namespace
                    raise ProcessEscalated(consecutive_errors, e) from e
                # Exponential backoff before next attempt
                await asyncio.sleep(min(2**consecutive_errors, 30))
                continue
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break

    async def _heartbeat_loop(self, interval: float = 10.0) -> None:
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self._publish_heartbeat("running")
            except asyncio.CancelledError:
                break
            except Exception:
                # The loop must outlive a failed publish, but a heartbeat that
                # stops arriving with nothing said is what makes an agent look
                # dead when it is not.
                logger.debug("[%s] Heartbeat publish failed", self.name, exc_info=True)

    async def _publish_heartbeat(self, state: str) -> None:
        await self._publish(
            f"agents/{self.actor_id}/heartbeat",
            {
                "actor_id": self.actor_id,
                "name": self.name,
                "timestamp": time.time(),
                "state": state,
                "node": self.node_name,  # extra field — shows in dashboard
                "cpu": 0.0,
                "memory_mb": 0.0,
                "task": "running" if state == "running" else state,
                "protected": False,
            },
        )

    # ── Task handling ─────────────────────────────────────────────────────────

    async def handle_task(self, payload: dict[str, Any]) -> Any:
        if not self._fn_handle_task:
            return {"error": f"Agent '{self.name}' has no handle_task function."}
        try:
            result = await self._fn_handle_task(self._api, payload)
        except Exception as e:
            err_str = traceback.format_exc()
            logger.exception("[%s] handle_task() error", self.name)
            await self._publish(
                f"agents/{self.actor_id}/errors",
                {
                    "phase": "handle_task",
                    "severity": "warning",
                    "error": str(e),
                    "traceback": err_str,
                    "agent": self.name,
                    "timestamp": time.time(),
                },
            )
            return {"error": str(e), "error_phase": "handle_task", "agent": self.name}
        else:
            return result or {}

    def deliver_reply(self, reply_topic: str, data: Any) -> bool:
        """Called by runner when an inbound reply arrives for this agent.
        Returns True if this agent had a pending future for the topic.
        """
        fut = self._pending_replies.get(reply_topic)
        if fut and not fut.done():
            fut.set_result(data)
            return True
        return False


# ── Remote runner (the process that lives on the Pi) ─────────────────────────


class _RemoteRunner:
    """The long-running process on the edge node.
    Connects to the MQTT broker, listens for spawn commands, manages agents.
    """

    def __init__(self, broker: str, port: int, node_name: str) -> None:
        self.broker = broker
        self.port = port
        self.node_name = node_name
        self._agents: dict[str, _RemoteAgent] = {}  # name → agent
        #: (topic, payload, retain, critical). The last field is what lets the
        #: cap below evict telemetry rather than whatever arrived first.
        self._pub_queue: asyncio.Queue[tuple[str, bytes, bool, bool]] | None = (
            None  # created in run() inside the event loop
        )
        self._running = False
        self._deps_ready: asyncio.Event | None = None  # set once aiomqtt/paho are importable
        self._start_time: float = time.time()  # for uptime reporting in heartbeat
        #: Messages the cap discarded, for the log.
        self._dropped = 0
        # Persistent state directory — survives reboots unlike /tmp
        _state_path = Path.home() / "wactorz" / "state"
        _state_path.mkdir(parents=True, exist_ok=True)
        self._state_dir = str(_state_path)

    # ── MQTT publish (queue-based, reconnect-safe) ────────────────────────────

    async def publish(self, topic: str, data: Any, retain: bool = False) -> None:
        """Queue a message for the publisher loop. Never waits for room.

        Waiting would push a stalled broker back into the agent code that called
        this -- the same reasoning as `MQTTPublisher._enqueue` on the server.
        """
        if self._pub_queue is None:
            return
        payload = json.dumps(data) if not isinstance(data, (str, bytes)) else data
        if isinstance(payload, str):
            payload = payload.encode()
        self._enqueue((topic, payload, retain, _is_critical(topic)))

    def _enqueue(self, item: tuple[str, bytes, bool, bool]) -> None:
        """Add a message, making room by dropping telemetry when the cap is hit.

        Room is made from the front: the oldest telemetry goes, because the next
        heartbeat replaces it anyway and the freshest sample is the useful one.
        If nothing droppable is queued, the incoming message gives way.

        ⚠ **Unlike the server, a message dropped here is gone.** `MQTTPublisher`
        can discard a queued QoS 1 because the SQLite outbox still holds it; a
        node has no outbox. The cap is therefore sized so that only a long
        outage on a busy node reaches it, and every drop is counted and logged.
        """
        queue = self._pub_queue
        if queue is None:
            return
        while True:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                if not self._discard_one_telemetry():
                    self._note_drop(item)
                    return
            else:
                return

    def _discard_one_telemetry(self) -> bool:
        """Drop the oldest non-critical message, if there is one. True if it did.

        Rebuilds the queue rather than reaching into it: `asyncio.Queue` offers
        no supported way to remove from the middle.
        """
        queue = self._pub_queue
        if queue is None:
            return False
        held: list[tuple[str, bytes, bool, bool]] = []
        dropped = False
        while not queue.empty():
            entry = queue.get_nowait()
            queue.task_done()
            if not dropped and not entry[3]:
                dropped = True
                self._note_drop(entry)
                continue
            held.append(entry)
        for entry in held:
            queue.put_nowait(entry)
        return dropped

    def _note_drop(self, item: tuple[str, bytes, bool, bool]) -> None:
        """Count a discarded message, and say so at a rate a log can carry."""
        self._dropped += 1
        if self._dropped == 1 or self._dropped % 1000 == 0:
            logger.warning(
                "[runner] publish queue full at %d — discarded %s (%d total). The broker "
                "is not keeping up, or is not there.",
                MAX_QUEUED,
                item[0],
                self._dropped,
            )

    # ── Spawn / stop agents ───────────────────────────────────────────────────

    async def spawn_agent(self, config: Any) -> None:
        if not isinstance(config, dict):
            logger.warning("[runner] spawn_agent: invalid config type %s, ignoring.", type(config))
            return
        name = config.get("name", f"agent-{uuid.uuid4().hex[:6]}")
        logger.info("[runner] Spawning agent '%s'...", name)
        if name in self._agents:
            if config.get("replace", False):
                logger.info("[runner] Replacing agent '%s'", name)
                await self.stop_agent(name)
            else:
                logger.info("[runner] Agent '%s' already running (use replace=true)", name)
                return

        packages = config.get("install", [])
        if packages:
            refused = await self._install_packages(packages)
            if refused:
                # Abort, unlike the pip-failure path below it, which warns and
                # carries on. A pip failure can be transient and may still leave
                # a usable environment; a refusal means we read the request and
                # rejected it, so starting the agent only moves the failure to an
                # import somewhere else — with the reason left in this node's log
                # and nothing on the dashboard saying why.
                logger.error("[runner] Not spawning '%s' — install list refused.", name)
                await self.publish(
                    f"agents/{self.node_name}/logs",
                    {
                        "type": "error",
                        "message": (
                            f"Refused to spawn '{name}': these are not package names: {refused}"
                        ),
                        "node": self.node_name,
                        "timestamp": time.time(),
                    },
                )
                return

        try:
            agent = _RemoteAgent(config, self, state_dir=self._state_dir)
            self._agents[name] = agent
            await agent.start()
            logger.info("[runner] Agent '%s' started.", name)
        except Exception as e:
            logger.exception("[runner] Failed to start agent '%s'", name)
            self._agents.pop(name, None)
            await self.publish(
                f"agents/{self.node_name}/logs",
                {
                    "type": "error",
                    "message": f"Failed to start '{name}': {e}",
                    "node": self.node_name,
                    "timestamp": time.time(),
                },
            )
            return

        await self.publish(
            f"agents/{self.node_name}/logs",
            {
                "type": "spawned",
                "message": f"Remote agent '{name}' started on {self.node_name}",
                "child_name": name,
                "node": self.node_name,
                "timestamp": time.time(),
            },
        )

    async def stop_agent(self, name: str, delete: bool = False) -> None:
        """Stop an agent. When `delete=True`, also wipe everything that would
        otherwise survive the stop:
          - the JSON state file on disk (~/wactorz/state/<name>_state.json)
          - the retained MQTT topics for this agent (status / heartbeat /
            metrics / manifest / logs / errors / detections / results /
            completed / spawned) so the broker stops re-delivering them on
            reconnect.

        Without delete=True, this is a plain stop: state is flushed to disk
        by agent.stop() and the next spawn or runner restart picks it back up.
        """
        agent = self._agents.pop(name, None)
        if not agent:
            return
        # Remember actor_id before stop() in case the agent clears attributes.
        actor_id = agent.actor_id

        await agent.stop()

        if delete:
            # agent.stop() already wrote the state file as part of its normal
            # shutdown. Undo that here, AFTER the agent is fully stopped, so
            # no later publish or supervisor restart can recreate it.
            agent._delete_state()
            await self._purge_agent_retained(actor_id)
            logger.info("[runner] Agent '%s' permanently deleted from this node.", name)

    async def _purge_agent_retained(self, actor_id: str) -> None:
        """Clear retained MQTT messages for an agent that has just been deleted.

        Empty-payload-with-retain is the MQTT idiom for "remove this retained
        topic" — without it, the broker keeps re-delivering the agent's last
        status / heartbeat / metrics / manifest etc. to every subscriber that
        connects later (including a freshly restarted monitor or main), which
        is what makes deleted agents reappear on restart.

        The runner is the right place to do this because it has the broker
        connection already open and knows the actor_id, so the purge is robust
        even when main is offline or unreachable at delete time.
        """
        topics = (
            "status",
            "heartbeat",
            "metrics",
            "logs",
            "spawned",
            "manifest",
            "errors",
            "detections",
            "results",
            "completed",
        )
        for metric in topics:
            try:
                await self.publish(f"agents/{actor_id}/{metric}", b"", retain=True)
            except Exception as e:
                logger.debug(
                    "[runner] Failed to clear retained agents/%s/%s: %s", actor_id, metric, e
                )

    async def stop_all(self) -> None:
        for name in list(self._agents):
            await self.stop_agent(name)

    async def _install_packages(self, packages: list[str]) -> list[str]:
        """Install pip packages on the edge node. Returns the names it refused.

        The names arrive in a spawn payload off the broker, so they are treated
        as input: refused unless they look like package names, and passed as
        argv rather than through a shell. A non-empty return means nothing was
        installed and the caller should not start the agent.
        """
        refused = [p for p in packages if not _is_installable_name(p)]
        if refused:
            # Refused as a whole, not filtered down to the acceptable ones:
            # installing a subset would report success for a request that was
            # not carried out, and the agent would then fail on a missing import
            # somewhere far from here.
            logger.error(
                "[runner] Refusing install — not package names: %s. Names may contain letters, digits, '.', '-', '_', extras and version specifiers; anything else (options, URLs, paths) is rejected.",
                refused,
            )
            return refused

        cmd, extra_env = _pip_install_command(packages)
        logger.info("[runner] Installing %s into %s.", " ".join(packages), _install_destination())
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env={**os.environ, **extra_env} if extra_env else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("[runner] pip install warning: %s", stderr.decode()[:200])
        return []

    # ── Status heartbeat for the node itself ──────────────────────────────────

    async def _node_heartbeat_loop(self, interval: float = 10.0) -> None:
        """Publish a heartbeat for the runner process itself so it appears in dashboard."""
        node_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.node.{self.node_name}"))
        while self._running:
            try:
                import psutil as _psutil

                agent_names = list(self._agents.keys())
                try:
                    cpu_pct = _psutil.cpu_percent(interval=None)
                    vm = _psutil.virtual_memory()
                    mem_used = vm.used // (1024 * 1024)
                    mem_free = vm.available // (1024 * 1024)
                except Exception:
                    cpu_pct = mem_used = mem_free = 0
                await self.publish(
                    f"nodes/{self.node_name}/heartbeat",
                    {
                        "node": self.node_name,
                        "node_id": node_id,
                        "timestamp": time.time(),
                        "agents": agent_names,
                        "agent_count": len(agent_names),
                        "broker": self.broker,
                        "pid": os.getpid(),
                        "uptime_s": round(time.time() - self._start_time, 1),
                        "cpu_pct": cpu_pct,
                        "mem_used_mb": mem_used,
                        "mem_free_mb": mem_free,
                    },
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(interval)

    # ── MQTT publisher task (paho-mqtt direct — aiomqtt v2.x doesn't flush reliably) ──

    def _connect_publisher(self) -> Any:
        """Open a paho client for publishing, with credentials where configured.

        The import is deferred rather than module-level: the runner installs its
        own dependencies at start-up, so paho need not exist when this file is
        first read.
        """
        import paho.mqtt.client as paho_mqtt
        from paho.mqtt.enums import CallbackAPIVersion
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties

        # Mirrors core/mqtt.py `client_id`. Spelled out rather than imported:
        # this file is deployed to a node on its own, with no wactorz package
        # beside it, so the shape has to be kept in step by hand.
        client = paho_mqtt.Client(
            # Explicit, not defaulted: omitting it selects paho's callback API
            # version 1, which is deprecated and warns on every construction.
            # Nothing here registers a paho callback, so version 2 costs no
            # migration -- and paho is not pinned anywhere (it arrives through
            # aiomqtt, which allows <3.0.0), while a node runs `pip install
            # aiomqtt` fresh on every deploy. A release that drops the old API
            # would otherwise break deploys with nothing in the tree to catch it.
            CallbackAPIVersion.VERSION2,
            client_id=f"wactorz-nodepub-{self.node_name}",
            # v5, so the kept session below can name a lifetime. v3.1.1 has no
            # expiry, and a decommissioned node would leave broker state for ever.
            protocol=paho_mqtt.MQTTv5,
        )
        user = os.environ.get("MQTT_USERNAME") or None
        if user:
            client.username_pw_set(user, os.environ.get("MQTT_PASSWORD") or None)
        properties = Properties(PacketTypes.CONNECT)
        properties.SessionExpiryInterval = NODE_SESSION_EXPIRY_SECONDS
        # Durable, so QoS 1 messages in flight when the link drops are
        # redelivered rather than discarded with the session.
        client.connect(
            self.broker, self.port, keepalive=60, clean_start=False, properties=properties
        )
        client.loop_start()
        return client

    async def _publish_one_queued(self, client: Any) -> None:
        """Hand the next queued message to the client, waiting for one to arrive.

        Entries are built by `publish` alone, so the shape is fixed:
        (topic, payload, retain, critical).
        """
        queue = self._pub_queue
        if queue is None:
            return
        item = await queue.get()
        topic, payload, retain = item[0], item[1], item[2]
        client.publish(topic, payload, qos=1, retain=retain)
        queue.task_done()

    async def _publisher_loop(self) -> None:
        """Uses paho-mqtt directly for reliable fire-and-forget publishing.
        aiomqtt v2.x wraps paho but its internal network loop doesn't get CPU
        time when we block on queue.get(), causing silent message loss.
        paho.loop_start() runs a background thread that handles ACKs/keepalives.
        """
        if self._deps_ready is None:
            return
        await self._deps_ready.wait()
        loop = asyncio.get_event_loop()
        client = None
        while self._running:
            if self._pub_queue is None:
                return
            try:
                if client is None:
                    client = await loop.run_in_executor(None, self._connect_publisher)
                    logger.info("[runner] Publisher connected to %s:%s", self.broker, self.port)

                await self._publish_one_queued(client)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[runner] Publisher error: %s. Reconnecting in 3s...", e)
                if client:
                    _close_mqtt_client(client, "Discarded publisher")
                    client = None
                await asyncio.sleep(3)

        if client:
            _close_mqtt_client(client, "Publisher")

    # ── MQTT subscriber task ──────────────────────────────────────────────────

    # ── Control-plane commands ────────────────────────────────────────────────

    async def _dispatch_control(self, topic_str: str, data: Any, msg: Any) -> None:
        """Route one control message to the command it names.

        Exact topics are looked up; the two families that carry a variable
        segment — a reply's correlation key and an agent's name — are matched
        after, because a table cannot express them.
        """
        exact = {
            f"nodes/{self.node_name}/desired_state": self._on_desired_state,
            f"nodes/{self.node_name}/spawn": self._on_spawn,
            f"nodes/{self.node_name}/stop": self._on_stop,
            f"nodes/{self.node_name}/migrate": self._on_migrate,
            f"nodes/{self.node_name}/stop_all": self._on_stop_all,
            f"nodes/{self.node_name}/restart": self._on_restart,
            f"nodes/{self.node_name}/restart_agent": self._on_restart_agent,
            f"nodes/{self.node_name}/list": self._on_list,
        }
        handler = exact.get(topic_str)
        if handler is not None:
            await handler(topic_str, data, msg)
        elif topic_str.startswith(f"nodes/{self.node_name}/reply/"):
            await self._on_reply(topic_str, data, msg)
        elif "/task" in topic_str:
            await self._on_task(topic_str, data, msg)

    async def _on_desired_state(self, topic_str: str, data: Any, msg: Any) -> None:
        """Start any agent named in the desired state that is not running."""
        if not msg.payload or not isinstance(data, dict):
            return
        desired = data.get("agents", [])
        if not desired:
            return
        logger.info(
            "[runner] Reconciling desired state: %s",
            [a.get("name") for a in desired],
        )

        def _log_exc(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception():
                logger.error("[runner] reconcile task failed: %s", t.exception())

        for agent_config in desired:
            aname = agent_config.get("name")
            if not aname:
                continue
            if aname in self._agents:
                logger.info("[runner] '%s' already running, skipping.", aname)
            else:
                logger.info("[runner] Reconcile: starting missing agent '%s'", aname)
                task = asyncio.create_task(self.spawn_agent(agent_config))
                task.add_done_callback(_log_exc)

    async def _on_spawn(self, topic_str: str, data: Any, msg: Any) -> None:
        if not msg.payload:  # empty = retain-clear message, ignore
            return

        def _log_task_exc(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception():
                logger.error("[runner] spawn_agent task failed: %s", t.exception())

        task = asyncio.create_task(self.spawn_agent(data))
        task.add_done_callback(_log_task_exc)
        # Clear the retained message so this spawn doesn't re-fire every time
        # the subscriber reconnects or restarts.
        asyncio.create_task(self.publish(topic_str, b"", retain=True))

    async def _on_stop(self, topic_str: str, data: Any, msg: Any) -> None:
        """Stop a named agent.

        Payload formats accepted:
          {"name": "foo"}                 plain stop, state preserved
          {"name": "foo", "delete": true} permanent delete: wipes the state file
                                          and the retained MQTT topics
          "foo"                           legacy bare name, plain stop
        """
        if isinstance(data, dict):
            name = data.get("name")
            do_delete = bool(data.get("delete", False))
        else:
            name = str(data)
            do_delete = False
        if name:
            asyncio.create_task(self.stop_agent(name, delete=do_delete))

    async def _on_migrate(self, topic_str: str, data: Any, msg: Any) -> None:
        """Move a running agent to another node.

        Payload: {"name": "agent-name", "target_node": "rpi-bedroom"}
        """
        if isinstance(data, dict):
            asyncio.create_task(self._migrate_agent(data))

    async def _on_stop_all(self, topic_str: str, data: Any, msg: Any) -> None:
        logger.info("[runner] stop_all received — shutting down.")
        asyncio.create_task(self._shutdown())

    async def _on_restart(self, topic_str: str, data: Any, msg: Any) -> None:
        """Restart the runner in place: stop the agents, re-exec, same PID."""
        logger.info("[runner] Restart command received.")
        asyncio.create_task(self._restart())

    async def _on_restart_agent(self, topic_str: str, data: Any, msg: Any) -> None:
        """Restart one agent without losing its config — stop plus spawn."""
        name = data.get("name") if isinstance(data, dict) else str(data)
        if not isinstance(name, str):
            name = str(name)
        asyncio.create_task(self._restart_agent(name))

    async def _on_list(self, topic_str: str, data: Any, msg: Any) -> None:
        await self.publish(
            f"nodes/{self.node_name}/agents",
            {
                "node": self.node_name,
                "agents": [{"name": a.name, "actor_id": a.actor_id} for a in self._agents.values()],
                "timestamp": time.time(),
            },
        )

    async def _on_reply(self, topic_str: str, data: Any, msg: Any) -> None:
        """Hand a reply to whichever agent is waiting on its topic."""
        for agent in self._agents.values():
            if agent.deliver_reply(topic_str, data):
                return
        # Every key actually waiting, so the mismatch is visible now rather
        # than as a timeout a minute later.
        waiting: list[str] = []
        for agent in self._agents.values():
            waiting.extend(list(agent._pending_replies.keys()))
        logger.warning(
            "[runner] Reply arrived on %s but no agent had a matching pending future. "
            "Waiting keys: %r",
            topic_str,
            waiting,
        )

    async def _on_task(self, topic_str: str, data: Any, msg: Any) -> None:
        """Run a task addressed to a named agent, off the consuming loop.

        The subscriber is a sequential consumer, so awaiting handle_task here
        would stop every other message being dispatched. An agent whose task
        makes a round trip — publishing to main and awaiting the reply on this
        same client — could then never receive it: the loop holding the only
        consumer is the loop waiting for the call to finish. It deadlocks and
        times out a minute later although main answered in milliseconds.
        """
        parts = topic_str.split("/")  # agents/by-name/{agent_name}/task
        if len(parts) < 4:
            return
        agent_name = parts[2]
        agent = self._agents.get(agent_name)
        if not agent or not isinstance(data, dict):
            return

        # handle_task receives the full envelope, as the local DynamicAgent
        # does. Unwrapping data['payload'] here instead would hand agent code a
        # bare string where it expects a dict, so the same agent would work
        # locally and break remotely. Transport metadata is stripped so it does
        # not leak into what the agent sees.
        reply_topic = data.get("_reply_topic")
        payload: Any = {k: v for k, v in data.items() if k not in ("_reply_topic", "_remote_task")}
        # A scalar wrapped in 'payload' and nothing else passes through as the
        # scalar, which is what callers sending {'payload': 42} expect.
        if set(payload.keys()) == {"payload"} and not isinstance(payload["payload"], dict):
            payload = payload["payload"]

        async def _run_task(a: _RemoteAgent, p: Any, rt: str | None, an: str) -> None:
            try:
                result = await a.handle_task(p)
            except Exception as e:
                logger.exception("[runner] handle_task error for '%s'", an)
                result = {"error": str(e), "agent": an}
            if rt:
                if not isinstance(result, dict):
                    result = {"result": str(result) if result is not None else ""}
                try:
                    await self.publish(rt, result)
                except Exception as e:
                    logger.warning("[runner] Reply publish failed for '%s' → %s: %s", an, rt, e)

        task = asyncio.create_task(_run_task(agent, payload, reply_topic, agent_name))
        # Hold a reference so the task is not collected mid-flight, and so a
        # shutdown can cancel it.
        agent._tasks.append(task)
        task.add_done_callback(lambda t, _ts=agent._tasks: _ts.remove(t) if t in _ts else None)

    async def _subscriber_loop(self) -> None:
        """Subscribes to:
        nodes/{node_name}/spawn          — spawn a new agent
        nodes/{node_name}/stop           — stop a named agent
        nodes/{node_name}/stop_all       — stop all agents and shut down
        nodes/{node_name}/list           — publish list of running agents
        nodes/{node_name}/reply/#        — route replies back to waiting agents
        agents/by-name/+/task           — task addressed to a named agent
        """
        if self._deps_ready is not None:
            await self._deps_ready.wait()
        import aiomqtt

        topics = [
            f"nodes/{self.node_name}/spawn",
            f"nodes/{self.node_name}/desired_state",  # reconciliation on reboot
            f"nodes/{self.node_name}/stop",
            f"nodes/{self.node_name}/stop_all",
            f"nodes/{self.node_name}/restart",  # restart the runner process in-place
            f"nodes/{self.node_name}/restart_agent",  # restart a single named agent
            f"nodes/{self.node_name}/migrate",
            f"nodes/{self.node_name}/list",
            f"nodes/{self.node_name}/reply/#",
            "agents/by-name/+/task",
        ]

        while self._running:
            try:
                async with aiomqtt.Client(
                    self.broker,
                    self.port,
                    username=os.environ.get("MQTT_USERNAME") or None,
                    password=os.environ.get("MQTT_PASSWORD") or None,
                    identifier=f"wactorz-node-{self.node_name}",  # mirrors core/mqtt.py client_id
                    # Durable: the broker holds control messages sent while this
                    # node was away, instead of dropping them on the floor. v5
                    # with an expiry rather than v3.1.1, which has none -- a
                    # decommissioned node would otherwise cost broker state for
                    # ever. Mirrors core/mqtt.py session_kwargs.
                    **_session_kwargs(aiomqtt, NODE_SESSION_EXPIRY_SECONDS),
                ) as client:
                    for topic in topics:
                        await client.subscribe(topic, qos=1)
                    logger.info(
                        "[runner] Subscribed to control topics on node '%s'", self.node_name
                    )

                    async for msg in client.messages:
                        topic_str = str(msg.topic)
                        try:
                            data = json.loads(msg.payload.decode())
                        except Exception:
                            data = msg.payload.decode()
                        await self._dispatch_control(topic_str, data, msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning("[runner] Subscriber disconnected: %s. Reconnecting in 3s...", e)
                    await asyncio.sleep(3)

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        # Created inside the running event loop.
        self._pub_queue = _new_pub_queue()
        self._deps_ready = asyncio.Event()
        logger.info(
            "[runner] Starting node '%s' → broker %s:%s", self.node_name, self.broker, self.port
        )

        # Bootstrap missing deps in thread pool; publisher/subscriber wait on this event.
        asyncio.create_task(_bootstrap_deps_async(self._deps_ready))

        tasks = [
            asyncio.create_task(self._publisher_loop()),
            asyncio.create_task(self._subscriber_loop()),
            asyncio.create_task(self._node_heartbeat_loop()),
        ]

        await asyncio.sleep(1)  # let publisher connect before anything else fires
        logger.info("[runner] Node '%s' online.", self.node_name)

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # Deliberately swallowed, unlike the other places that cancel a task
            # they own. This is the top of the node — nothing awaits run(), it is
            # driven by run_until_complete — and consuming the cancellation is
            # what lets the cleanup below finish. Left pending, the first await
            # in stop_all() would re-raise and the node would exit without
            # stopping its agents.
            pass
        finally:
            await self.stop_all()
            for t in tasks:
                t.cancel()

    async def _restart_agent(self, name: str) -> None:
        """Restart a single agent without losing its config or persisted state.
        The agent's state file is left on disk — the fresh instance picks it up
        via _load_state() on startup, so no state is lost.
        """
        agent = self._agents.get(name)
        if not agent:
            logger.warning("[runner] restart_agent: '%s' not running here", name)
            await self.publish(
                f"nodes/{self.node_name}/logs",
                {
                    "type": "error",
                    "message": f"restart_agent: '{name}' not found",
                    "node": self.node_name,
                    "timestamp": time.time(),
                },
            )
            return
        config = dict(agent._config)
        config["replace"] = True
        logger.info("[runner] Restarting agent '%s'", name)
        await self.spawn_agent(config)

    async def _restart(self) -> None:
        """Gracefully restart the runner process in-place using os.execv.
        - Stops all agents (their state files are flushed to disk by stop())
        - Publishes a "restarting" heartbeat so main sees the transition
        - Re-execs itself: same PID, same venv, clean asyncio state
        If systemd/supervisord is managing the process this is equivalent
        to a graceful reload; without a process manager, the process simply
        restarts itself.
        """
        logger.info("[runner] Restarting runner process via os.execv …")
        await self.stop_all()
        await self.publish(
            f"nodes/{self.node_name}/heartbeat",
            {"node": self.node_name, "status": "restarting", "timestamp": time.time()},
        )
        # Drain the publish queue before we replace the process image
        await asyncio.sleep(0.5)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    async def _shutdown(self) -> None:
        self._running = False
        await self.stop_all()
        await self.publish(
            f"nodes/{self.node_name}/heartbeat",
            {"node": self.node_name, "status": "offline", "timestamp": time.time()},
        )
        # Drain before exit so the heartbeat reaches the broker
        await asyncio.sleep(0.3)
        sys.exit(0)

    async def _migrate_agent(self, payload: dict[str, Any]) -> None:
        """Move a running agent to a different node.

        P0 fix: agent._persistent_state is serialised into the spawn config
        under "_initial_state" so the target node starts with the full state
        rather than an empty dict.  Only JSON-serialisable values survive the
        trip (counters, calibration values, thresholds, timestamps — everything
        a typical IoT agent stores).  Non-serialisable objects (numpy arrays,
        cv2 captures) are silently dropped with a warning; they would not
        survive a process restart anyway.

        payload: {"name": "agent-name", "target_node": "rpi-bedroom"}
        """
        name = payload.get("name")
        target_node = payload.get("target_node")
        if not name or not target_node:
            logger.warning("[runner] migrate: missing 'name' or 'target_node' in payload")
            return

        agent = self._agents.get(name)
        if not agent:
            logger.warning("[runner] migrate: agent '%s' not running here", name)
            await self.publish(
                f"nodes/{self.node_name}/migrate_result",
                {
                    "success": False,
                    "error": f"Agent '{name}' not found on {self.node_name}",
                    "agent": name,
                    "timestamp": time.time(),
                },
            )
            return

        # ── Capture config + state before stopping ────────────────────────────
        config = dict(agent._config)
        config["node"] = target_node
        config.pop("replace", None)  # clean slate on new node

        # Snapshot persistent state — serialize only JSON-safe values
        raw_state = dict(agent._persistent_state)
        safe_state: dict = {}
        dropped: list = []
        for k, v in raw_state.items():
            try:
                json.dumps(v)  # probe — raises if not serialisable
                safe_state[k] = v
            except (TypeError, ValueError):
                dropped.append(k)
        if dropped:
            logger.warning(
                "[runner] migrate '%s': dropping non-JSON state keys %s — they cannot travel over MQTT",
                name,
                dropped,
            )

        # ── Remote → Local migration ──────────────────────────────────────────
        # target_node == "@main" is the sentinel from MainActor meaning:
        # "don't spawn anywhere — stop the agent and return its state to me".
        # Main re-spawns the agent on its own host using this snapshot.
        if target_node == "@main":
            return_token = payload.get("return_token", "")
            logger.info(
                "[runner] Migrating '%s' from %s → local (main); returning %s state key(s)",
                name,
                self.node_name,
                len(safe_state),
            )
            # Snapshot the full config BEFORE we stop the agent (some
            # implementations clear _config on stop). 'config' here already
            # has node=@main from the assignment above — restore the original
            # node so main can see where it came from, and let main strip it.
            return_config = dict(agent._config)
            return_config["node"] = self.node_name
            return_config.pop("_initial_state", None)
            return_config.pop("replace", None)
            # Stop locally AND delete the state file. The agent has just been
            # migrated away — keeping its state.json behind would resurrect
            # stale memory if the agent is ever migrated back to this node.
            # The snapshot we're about to publish is the authoritative copy.
            await self.stop_agent(name, delete=True)
            await asyncio.sleep(0.3)
            await self.publish(
                f"nodes/{self.node_name}/state_return",
                {
                    "agent": name,
                    "return_token": return_token,
                    "config": return_config,
                    "state": safe_state,
                    "state_keys_dropped": dropped,
                    "from_node": self.node_name,
                    "timestamp": time.time(),
                },
            )
            await self.publish(
                f"nodes/{self.node_name}/migrate_result",
                {
                    "success": True,
                    "agent": name,
                    "from_node": self.node_name,
                    "to_node": "local",
                    "state_keys_transferred": list(safe_state.keys()),
                    "state_keys_dropped": dropped,
                    "timestamp": time.time(),
                },
            )
            logger.info("[runner] Migration of '%s' to local (main) dispatched.", name)
            return

        if safe_state:
            config["_initial_state"] = safe_state
            logger.info(
                "[runner] migrate '%s': carrying %s state key(s) to '%s': %s",
                name,
                len(safe_state),
                target_node,
                list(safe_state.keys()),
            )

        logger.info("[runner] Migrating '%s' from %s → %s", name, self.node_name, target_node)

        # Stop locally AND delete the state file. The agent's state is now in
        # `config["_initial_state"]` and about to be published to the target
        # node — that snapshot is authoritative. A stale leftover JSON on this
        # node would otherwise survive and conflict on any future migrate-back.
        await self.stop_agent(name, delete=True)
        await asyncio.sleep(0.3)  # let heartbeat "stopped" reach broker

        # Publish spawn to target node via MQTT
        await self.publish(f"nodes/{target_node}/spawn", config)

        await self.publish(
            f"nodes/{self.node_name}/migrate_result",
            {
                "success": True,
                "agent": name,
                "from_node": self.node_name,
                "to_node": target_node,
                "state_keys_transferred": list(safe_state.keys()),
                "state_keys_dropped": dropped,
                "timestamp": time.time(),
            },
        )
        logger.info("[runner] Migration of '%s' to '%s' dispatched.", name, target_node)


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wactorz edge node runner — deploy on Raspberry Pi or any remote machine"
    )
    parser.add_argument(
        "--broker",
        default=os.getenv("WACTORZ_BROKER", "localhost"),
        help="MQTT broker host (default: localhost or $WACTORZ_BROKER)",
    )
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port (default: 1883)")
    _default_node = os.getenv("WACTORZ_NODE", f"node-{uuid.uuid4().hex[:6]}")
    parser.add_argument(
        "--name", default=_default_node, help="Unique node name (default: $WACTORZ_NODE or random)"
    )
    parser.add_argument("--node", default=None, help="Alias for --name (either works)")
    parser.add_argument("--loglevel", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    # --node takes priority over --name if both supplied
    node_name = args.node or args.name

    logging.basicConfig(
        level=getattr(logging, args.loglevel),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # The node name becomes one level of every topic this runner uses
    # (nodes/<name>/heartbeat to publish, nodes/<name>/spawn to subscribe), so
    # an MQTT wildcard or separator in it makes the broker refuse every publish
    # and every subscribe. Without this check the runner connects, fails each
    # operation, and reconnects every 3s forever — filling its log and never
    # appearing on the dashboard. Duplicated from wactorz/config.py's
    # deploy_name_error rather than imported: this file is deployed to the edge
    # node on its own, with no wactorz package alongside it.
    bad = [c for c in ("#", "+", "/") if c in node_name]
    if bad or not node_name.strip():
        problem = f"contains {' and '.join(repr(c) for c in bad)}" if bad else "is empty"
        logger.error(
            "[runner] Refusing to start: node name %r %s, which cannot appear in an MQTT topic. Rename the node and redeploy.",
            node_name,
            problem,
        )
        raise SystemExit(2)

    runner = _RemoteRunner(broker=args.broker, port=args.port, node_name=node_name)

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _signal_handler() -> None:
        logger.info("[runner] Shutdown signal received.")
        loop.create_task(runner._shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, AttributeError):
            pass  # Windows doesn't support add_signal_handler for all signals

    try:
        loop.run_until_complete(runner.run())
    finally:
        loop.close()


# ── Self-test (python3 remote_runner.py --test) ───────────────────────────────


async def _settle(task: asyncio.Task) -> None:
    """Cancel a task and wait for it to unwind, whatever it raises on the way."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _run_supervisor_tests() -> bool:
    """Standalone tests. No MQTT broker required."""
    passed = 0
    failed = 0

    class _StubRunner:
        node_name = "test-node"

        def __init__(self) -> None:
            self._agents = {}
            self.events = []

        async def publish(self, topic: str, data: Any, retain: bool = False) -> None:
            # Mirrors _RemoteRunner.publish, `retain` included — without it
            # every lifecycle raised TypeError and the supervisor restarted
            # a healthy agent.
            self.events.append((topic, data))

    def make_agent(
        code: str,
        max_restarts: int = 3,
        restart_delay: float = 0.01,
        poll_interval: float = 0.01,
        escalate_after: int = 5,
    ) -> tuple[_RemoteAgent, _StubRunner]:
        runner = _StubRunner()
        config = {
            "name": "test-agent",
            "code": code,
            "max_restarts": max_restarts,
            "restart_delay": restart_delay,
            "poll_interval": poll_interval,
        }
        agent = _RemoteAgent(config, runner)  # pyright: ignore[reportArgumentType]
        agent._PROCESS_ESCALATE_AFTER = escalate_after
        agent._running = True  # start() sets this; we call _supervisor_loop directly in tests
        runner._agents["test-agent"] = agent
        return agent, runner

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if condition:
            print(f"  PASS  {name}")
            passed += 1
        else:
            print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))
            failed += 1

    print("\n-- remote_runner supervisor tests --")

    # Test 1: Stable agent never restarted
    agent, runner = make_agent("async def process(agent): pass")
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.15)
    agent._running = False
    await asyncio.sleep(0.05)
    await _settle(task)
    check("stable: restart_count=0", agent._restart_count == 0, f"got {agent._restart_count}")
    check("stable: not failed", not agent._failed)

    # Test 2: Crashing process escalates and triggers supervisor restart
    crash_code = "async def process(agent):\n    raise RuntimeError('boom')"
    agent, runner = make_agent(
        crash_code, max_restarts=3, restart_delay=0.01, poll_interval=0.001, escalate_after=2
    )
    task = asyncio.create_task(agent._supervisor_loop())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
    except asyncio.TimeoutError:
        pass
    check(
        "crash: error events published",
        any(
            isinstance(e, dict) and e.get("phase") in ("process", "supervisor")
            for _, e in runner.events
        ),
        f"{[(t, d.get('phase') if isinstance(d, dict) else '?') for t, d in runner.events[:5]]}",
    )
    check("crash: restart_count > 0", agent._restart_count > 0, f"got {agent._restart_count}")
    # Either failed completely, or has accumulated restarts (budget=3 may not exhaust in time)
    check(
        "crash: supervisor restarted at least once",
        agent._failed or agent._restart_count >= 1,
        f"count={agent._restart_count}",
    )

    # Test 3: Budget exhaustion
    agent, runner = make_agent(
        crash_code, max_restarts=1, restart_delay=0.01, poll_interval=0.001, escalate_after=1
    )
    task = asyncio.create_task(agent._supervisor_loop())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
    except asyncio.TimeoutError:
        pass
    check("budget: _failed=True", agent._failed, f"count={agent._restart_count}")
    check(
        "budget: fatal event",
        any(isinstance(e, dict) and e.get("severity") == "fatal" for _, e in runner.events),
    )
    check("budget: removed from runner", "test-agent" not in runner._agents)

    # Test 4: deliberate stop() no restart
    agent, runner = make_agent("async def process(agent): pass")
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.05)
    await agent.stop()
    await _settle(task)
    check("stop(): restart_count=0", agent._restart_count == 0)
    check("stop(): not failed", not agent._failed)

    # Test 5: health credit after 10 successful runs
    agent, runner = make_agent("async def process(agent): pass", poll_interval=0.001)
    agent._restart_count = 2
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.3)
    agent._running = False
    await asyncio.sleep(0.05)
    await _settle(task)
    check(
        "health credit: restart_count < 2", agent._restart_count < 2, f"got {agent._restart_count}"
    )

    # Test 6: compile error stops supervision
    agent, runner = make_agent("this is not valid python !!!")
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.15)
    await _settle(task)
    check("compile: _running=False", not agent._running)
    check("compile: restart_count=0", agent._restart_count == 0)
    check(
        "compile: fatal event",
        any(isinstance(e, dict) and e.get("phase") == "compile" for _, e in runner.events),
        f"{runner.events}",
    )

    # Test 7: setup() error stops supervision
    setup_fail = "async def setup(agent):\n    raise RuntimeError('bad')\nasync def process(agent):\n    pass"
    agent, runner = make_agent(setup_fail)
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.15)
    await _settle(task)
    check("setup: _running=False", not agent._running)
    check("setup: restart_count=0", agent._restart_count == 0)

    print(f"\n  {passed} passed, {failed} failed\n")
    return failed == 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        ok = asyncio.run(_run_supervisor_tests())
        sys.exit(0 if ok else 1)
    else:
        main()
