#!/usr/bin/env python3
"""remote_runner.py — Wactorz edge node runner.

Deploy this single file to any machine (Raspberry Pi, VM, edge device).
It connects to the shared MQTT broker, listens for spawn commands from main,
and runs DynamicAgents locally. Those agents heartbeat back to the same broker
so they appear in the central dashboard exactly like local agents.

Usage on the remote machine:
    pip install aiomqtt paho-mqtt psutil aiohttp --break-system-packages
    python3 remote_runner.py --broker 192.168.1.10 --name rpi-livingroom

From the main Wactorz chat (automatic via devops-agent):
    "deploy node rpi-livingroom to pi@192.168.1.50 with broker 192.168.1.10"

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

import argparse
import asyncio
import importlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
import traceback
import uuid
from typing import Any


def _missing_deps() -> list:
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


async def _bootstrap_deps_async(ready: "asyncio.Event") -> None:
    """Install missing deps in a thread pool, then signal the event."""
    import importlib as _il

    needed = _missing_deps()
    if not needed:
        ready.set()
        return
    print(f"[remote_runner] auto-installing {needed} via {sys.executable}...", flush=True)

    def _pip() -> tuple:
        cmd = [sys.executable, "-m", "pip", "install", *needed, "-q"]
        if sys.platform != "win32":
            cmd.append("--break-system-packages")
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, r.stderr[:300]

    loop = asyncio.get_running_loop()
    rc, err = await loop.run_in_executor(None, _pip)
    if rc != 0:
        print(f"[remote_runner] pip warning: {err}", flush=True)
    else:
        print("[remote_runner] deps installed.", flush=True)
    _il.invalidate_caches()
    ready.set()


logger = logging.getLogger("remote_runner")


# ── Sentinel: awaitable None ──────────────────────────────────────────────────
# Mirror of dynamic_agent._AwaitableNone. Returned from sync methods like
# subscribe() and declare_contract() so LLM-generated code that mistakenly
# writes `await agent.subscribe(...)` doesn't blow up.


class _AwaitableNone:
    def __await__(self):
        return iter([])  # completes immediately, yields None

    def __bool__(self):
        return False

    def __repr__(self):
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
    ):
        from collections import deque

        self.topic = topic
        self.seconds = float(seconds)
        self.max_size = int(max_size)
        self._broker = broker
        self._port = port
        self._buffer: deque[dict] = deque(maxlen=self.max_size)
        self._task: asyncio.Task | None = None

    def start(self) -> "_RemoteStreamWindow":
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._listen())
        return self

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def _listen(self):
        try:
            import aiomqtt
        except ImportError:
            logger.error("[StreamWindow] aiomqtt not installed")
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
                        payload["_ts"] = time.time()
                        self._buffer.append(payload)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5)

    # ── Trimming + queries ────────────────────────────────────────────────────
    def _trim(self):
        cutoff = time.time() - self.seconds
        while self._buffer and self._buffer[0].get("_ts", 0) < cutoff:
            self._buffer.popleft()

    def values(self, key: str = "value") -> list:
        self._trim()
        return [e[key] for e in self._buffer if key in e]

    def count(self) -> int:
        self._trim()
        return len(self._buffer)

    def latest(self, key: str = "value"):
        self._trim()
        for e in reversed(self._buffer):
            if key in e:
                return e[key]
        return None

    def mean(self, key: str = "value"):
        vs = [v for v in self.values(key) if isinstance(v, (int, float))]
        return sum(vs) / len(vs) if vs else None

    def min(self, key: str = "value"):
        vs = [v for v in self.values(key) if isinstance(v, (int, float))]
        return min(vs) if vs else None

    def max(self, key: str = "value"):
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
# Mirror of dynamic_agent._LLMInterface, but the actual LLM call happens on
# main via the existing main/llm_request bridge. This means:
#   - The same agent code (`agent.llm.chat(...)` / `agent.llm.complete(...)`)
#     works on both local and remote nodes — no migration breakage.
#   - The API key stays on main; the edge device never needs it.
#
# Cost tracking caveat: locally the _LLMInterface increments the agent's
# token / cost counters from the LLM response's usage dict. The LLM bridge
# currently returns only {"text": ...} — usage is not propagated back, so
# remote LLM cost is currently attributed to main, not to the agent that
# spent it. Fixing that needs the bridge to ship usage in the reply; left
# as a follow-up so this fix stays minimal.


class _RemoteLLMInterface:
    """Drop-in equivalent of _AgentAPI.llm on the remote side."""

    def __init__(self, api: "_RemoteAgentAPI"):
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
        """Stateful multi-turn chat — mirrors local _LLMInterface.converse().
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

    def __init__(self, agent: "_RemoteAgent"):
        self._agent = agent
        self._published_topics: set = set()
        # Observed payload schemas captured from real publish() calls. Maps
        # topic → {"fields": {name: type_str}, "example": dict}. Mirrors the
        # local DynamicAgent behaviour so the planner sees real field names
        # for remote agents, not just LLM-declared guesses.
        self._observed_samples: dict[str, dict] = {}
        # Set of (topic, id(callback)) pairs — dedup guard against double
        # subscribe() when setup() runs more than once (e.g. on reconnect).
        self._subscribed_topics: set = set()
        # Background subscriber tasks, kept so they can be cancelled on stop()
        # and not garbage-collected while running.
        self._subscriber_tasks: list[asyncio.Task] = []
        # Declared contract surface (subscribes / triggers_when / schemas)
        # populated by declare_contract(). Folded into the manifest by
        # _publish_manifest() so main can register a complete TopicContract.
        self._declared_subscribes: list = []
        self._declared_triggers_when: dict = {}
        self._declared_produces_schema: dict = {}
        self._declared_consumes_schema: dict = {}
        # Active stream windows by topic, so window() is idempotent per topic
        # and tasks are reachable for shutdown.
        self._windows: dict[str, _RemoteStreamWindow] = {}
        # Shared mutable namespace exposed as agent.state to user code (mirrors
        # DynamicAgent._AgentAPI.state). The remote runner historically pointed
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
    async def publish(self, topic: str, data: Any):
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

    async def _publish_manifest(self):
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

    async def publish_result(self, data: Any):
        """Publish agent result to agents/{id}/results — mirrors DynamicAgent API."""
        await self._agent._publish(
            f"agents/{self.actor_id}/results",
            {"agent": self.name, "node": self.node, "result": data, "timestamp": time.time()},
        )

    async def publish_detection(self, data: Any):
        """Publish detection results to agents/{id}/detections — mirrors DynamicAgent API."""
        await self._agent._publish(
            f"agents/{self.actor_id}/detections",
            {"agent": self.name, "node": self.node, "detections": data, "timestamp": time.time()},
        )
        # Also publish to a human-friendly topic for easy MQTT subscription
        await self.publish(f"{self.node}/{self.name}/detections", data)

    # ── Subscriptions ─────────────────────────────────────────────────────────
    def subscribe(self, topic: str, callback):
        """Subscribe to an MQTT topic and call callback(payload_dict) for each
        message. Runs as a background task — setup() returns immediately.

        IMPORTANT: callback is REQUIRED and must be an async function.
        subscribe() is NOT awaitable and does NOT return data.
        For a one-shot read use: data = await agent.mqtt_get(topic)

        Mirrors DynamicAgent._AgentAPI.subscribe(). The remote node has no
        TopicBus, so the subscription is also recorded on the API so that the
        next _publish_manifest() includes it — main then registers it on the
        central TopicBus and the planner can wire it.
        """
        if callback is None or not callable(callback):
            raise TypeError(
                f"agent.subscribe('{topic}', callback) requires a callable callback. "
                f"Got: {type(callback).__name__}. "
                f"Define: async def on_msg(payload): ... then call agent.subscribe('{topic}', on_msg). "
                f"For a one-shot read use: data = await agent.mqtt_get('{topic}')"
            )

        # Validate callback accepts at least one argument (the payload)
        import inspect

        try:
            sig = inspect.signature(callback)
            params = [p for p in sig.parameters.values() if p.default is inspect.Parameter.empty]
            if len(params) == 0:
                raise TypeError(
                    f"Subscribe callback must accept one argument (the payload dict). "
                    f"Got a function with no required parameters. "
                    f"Fix: async def {callback.__name__}(payload): ..."
                )
        except (TypeError, ValueError):
            pass  # Can't inspect — proceed and let runtime catch it

        # Dedup — same topic+callback pair only registers one listener.
        sub_key = (topic, id(callback))
        if sub_key in self._subscribed_topics:
            logger.debug(f"[{self.name}] Already subscribed to {topic} — skipping duplicate")
            return _AWAITABLE_NONE
        self._subscribed_topics.add(sub_key)

        broker = self._agent._runner.broker
        port = self._agent._runner.port
        agent_name = self.name

        # Tolerate LLM-generated `await None` errors inside callbacks — same
        # protection DynamicAgent.subscribe() applies. Warn once per topic
        # then suppress so we don't spam logs.
        _await_warned = False

        async def _safe_invoke(cb, payload):
            nonlocal _await_warned
            try:
                await cb(payload)
            except TypeError as e:
                if "NoneType" in str(e) and "await" in str(e):
                    if not _await_warned:
                        logger.warning(
                            f"[{agent_name}] subscribe callback has "
                            f"'await None' error (suppressed): {e}"
                        )
                        _await_warned = True
                else:
                    raise

        async def _listener():
            try:
                import aiomqtt
            except ImportError:
                logger.error(f"[{agent_name}] aiomqtt not installed")
                return
            while True:
                try:
                    async with aiomqtt.Client(
                        broker,
                        port,
                        username=os.environ.get("MQTT_USERNAME") or None,
                        password=os.environ.get("MQTT_PASSWORD") or None,
                    ) as client:
                        await client.subscribe(topic)
                        logger.info(f"[{agent_name}] Subscribed to {topic}")
                        async for msg in client.messages:
                            try:
                                payload = json.loads(msg.payload.decode())
                            except Exception:
                                payload = {"raw": msg.payload.decode()}
                            try:
                                await _safe_invoke(callback, payload)
                            except Exception as e:
                                logger.error(
                                    f"[{agent_name}] subscribe callback error (topic={topic}): {e}"
                                )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(
                        f"[{agent_name}] MQTT subscribe error on {topic}: {e} — retrying in 5s"
                    )
                    await asyncio.sleep(5)

        task = asyncio.create_task(_listener())
        self._subscriber_tasks.append(task)
        # Also let the agent's task list see it so stop() cancels cleanly.
        try:
            self._agent._tasks.append(task)
        except Exception:
            pass

        # Record the subscription on the contract surface and re-publish the
        # manifest so main learns about it and updates the central TopicBus.
        if topic not in self._declared_subscribes:
            self._declared_subscribes.append(topic)
            asyncio.create_task(self._publish_manifest())

        # Return an awaitable no-op so `await agent.subscribe(...)` doesn't crash.
        return _AWAITABLE_NONE

    # ── One-shot reads / time windows / world state ──────────────────────────
    async def mqtt_get(self, topic: str, timeout: float = 10.0) -> Any | None:
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

        async def _fetch():
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
                pass

        try:
            await asyncio.wait_for(_fetch(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return result[0] if result else None

    def window(
        self, topic: str, seconds: float = 300, max_size: int = 1000
    ) -> "_RemoteStreamWindow":
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

    async def publish_world_state(self, key: str, data: Any, retain: bool = True):
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
        publishes=None,
        subscribes=None,
        triggers_when: dict = None,
        produces_schema: dict = None,
        consumes_schema: dict = None,
        **kwargs,
    ):
        """Declare this agent's topic contract — what it produces and consumes.

        Call from setup() to make this agent discoverable by the planner and
        other agents via topic-based auto-wiring. Same signature and aliases
        as DynamicAgent._AgentAPI.declare_contract().

        On a remote node there's no local TopicBus, so the declared values are
        stored on the API and folded into the next _publish_manifest() — main
        then registers a complete TopicContract on the central bus.
        """
        # ── Accept common LLM kwarg aliases ───────────────────────────────────
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

        # ── Coerce single strings to lists ─────────────────────────────────────
        if isinstance(publishes, str):
            publishes = [publishes]
        if isinstance(subscribes, str):
            subscribes = [subscribes]

        # Fold declared values into our tracking — _publish_manifest() picks
        # them up next time it fires.
        for t in publishes or []:
            self._published_topics.add(t)
        for t in subscribes or []:
            if t not in self._declared_subscribes:
                self._declared_subscribes.append(t)
        if triggers_when:
            self._declared_triggers_when.update(triggers_when)
        if produces_schema:
            self._declared_produces_schema.update(produces_schema)
        if consumes_schema:
            self._declared_consumes_schema.update(consumes_schema)

        asyncio.create_task(self._publish_manifest())
        # Safe to await — return an awaitable sentinel because LLM code often
        # writes `await agent.declare_contract(...)`.
        return _AWAITABLE_NONE

    def wiring_opportunities(self) -> list:
        """Remote agents can't query the central TopicBus directly — that runs in
        the main process. Returns an empty list. Use `/agents` from main or
        ask the planner if you need wiring info.
        """
        return []

    # ── Introspection ─────────────────────────────────────────────────────────
    # These mirror the LOCAL helpers' shape but only see what's reachable from
    # this remote node. Cross-cluster introspection lives on main; remote code
    # that needs the global view should send a task there.

    def nodes(self) -> list:
        """List of nodes visible to this remote runner — only itself."""
        return [
            {
                "node": self.node,
                "online": True,
                "agents": [a.name for a in self._agent._runner._agents.values()],
            }
        ]

    def topics(self, keyword: str = "") -> list:
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

    def capabilities(self, keyword: str = "") -> list:
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
    def logger(self):
        """Compatibility shim — allows agent.logger.info/warning/error in
        generated code, mirroring DynamicAgent._AgentAPI.logger.
        """
        api = self

        class _LoggerShim:
            def info(self, msg):
                asyncio.ensure_future(api.log(msg, "info"))

            def warning(self, msg):
                asyncio.ensure_future(api.log(msg, "warning"))

            def error(self, msg):
                asyncio.ensure_future(api.log(msg, "error"))

            def debug(self, msg):
                asyncio.ensure_future(api.log(msg, "debug"))

        return _LoggerShim()

    async def set_status(self, status: str):
        """Update agent task status string visible in dashboard."""
        self._agent._status = status

    # ── Logging ───────────────────────────────────────────────────────────────
    async def log(self, message: str, level: str = "info"):
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

    async def alert(self, message: str, severity: str = "warning"):
        logger.warning(f"[{self.name}] ALERT({severity}): {message}")
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
    def persist(self, key: str, value: Any):
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
            logger.warning(f"[{self.name}] ask_llm timed out after {timeout}s")
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
            logger.warning(f"[{self.name}] chat() timed out after {timeout}s")
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
            logger.warning(f"[{self.name}] send_to '{agent_name}' timed out")
            return None
        finally:
            self._agent._pending_replies.pop(reply_topic, None)

    # Alias used in DynamicAgent code
    async def delegate(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Any:
        return await self.send_to(agent_name, payload, timeout)

    def agents(self) -> list:
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

    def __init__(self, config: dict, runner: "_RemoteRunner", state_dir: str = "/tmp"):
        self.name = config.get("name", f"remote-agent-{uuid.uuid4().hex[:6]}")
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
        self._state_path = os.path.join(state_dir, f"{safe_name}_state.json")
        self._pending_replies: dict[str, asyncio.Future] = {}
        self._api = _RemoteAgentAPI(self)
        self._tasks: list[asyncio.Task] = []
        self._running = False

        self._fn_setup = None
        self._fn_process = None
        self._fn_handle_task = None

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
            if os.path.exists(self._state_path):
                logger.info(
                    f"[{self.name}] Migration: overwriting stale local state file "
                    f"at {self._state_path} with {len(initial)} key(s) shipped from "
                    f"the source node"
                )
                try:
                    os.unlink(self._state_path)
                except Exception as e:
                    logger.warning(
                        f"[{self.name}] Could not remove stale state file before "
                        f"applying migration snapshot: {e}"
                    )
            self._persistent_state = dict(initial)
            self._save_state()
            logger.info(
                f"[{self.name}] Restored {len(initial)} state key(s) from migration: "
                f"{list(initial.keys())}"
            )
        else:
            # Normal start/restart — pick up whatever is already on disk.
            self._load_state()

    # ── State persistence (JSON, not pickle — portable across Python versions) ─

    def _save_state(self):
        try:
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump(self._persistent_state, f)
        except Exception as e:
            logger.warning(f"[{self.name}] State save failed: {e}")

    def _load_state(self):
        if os.path.exists(self._state_path):
            try:
                with open(self._state_path, encoding="utf-8") as f:
                    self._persistent_state = json.load(f)
                logger.info(f"[{self.name}] Loaded persistent state.")
            except Exception:
                pass

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
            if os.path.exists(self._state_path):
                os.unlink(self._state_path)
                removed = True
                logger.info(f"[{self.name}] Deleted persistent state file: {self._state_path}")
        except Exception as e:
            logger.warning(f"[{self.name}] Failed to delete state file {self._state_path}: {e}")
        return removed

    # ── MQTT publish helper ───────────────────────────────────────────────────

    async def _publish(self, topic: str, data: Any):
        await self._runner.publish(topic, data)

    # ── Code compilation ──────────────────────────────────────────────────────

    def _compile(self) -> str | None:
        """Compile user code into self._ns. Returns error string or None."""
        try:
            exec(compile(self._code, f"<{self.name}>", "exec"), self._ns)
            self._fn_setup = self._ns.get("setup")
            self._fn_process = self._ns.get("process")
            self._fn_handle_task = self._ns.get("handle_task")
            return None
        except Exception as e:
            return f"Compile error: {e}\n{traceback.format_exc()}"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        """Start the agent under a supervision loop.
        The supervisor restarts the agent on unexpected crashes up to
        max_restarts times, with exponential back-off between attempts.
        Compile errors and deliberate stop() calls are never retried.
        """
        self._running = True
        asyncio.create_task(self._supervisor_loop())

    async def _supervisor_loop(self):
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
                    logger.error(
                        f"[{self.name}] Crashed {self._restart_count} times — "
                        f"giving up (max_restarts={self._max_restarts})."
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
                    f"[{self.name}] Crashed (attempt {self._restart_count}/{self._max_restarts}). "
                    f"Restarting in {delay:.1f}s..."
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

    async def _run_lifecycle(self):
        """One full agent lifecycle: compile → setup → process loop + heartbeat loop.
        Raises on unhandled exceptions so _supervisor_loop can catch and restart.
        Compile errors and setup fatals publish an error event then return cleanly
        (no restart — broken code won't fix itself on retry).
        """
        # Reset per-run namespace and function pointers
        self._ns = {}
        self._fn_setup = self._fn_process = self._fn_handle_task = None

        err = self._compile()
        if err:
            logger.error(f"[{self.name}] {err}")
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
                await self._fn_setup(self._api)
                logger.info(f"[{self.name}] setup() completed.")
            except Exception as e:
                err_str = traceback.format_exc()
                logger.error(f"[{self.name}] setup() failed: {e}")
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

    async def stop(self):
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
            pass
        self._save_state()
        await self._publish_heartbeat("stopped")
        logger.info(f"[{self.name}] Stopped.")

    # ── Loops ─────────────────────────────────────────────────────────────────

    # After this many consecutive process() errors, raise to trigger a supervisor restart
    _PROCESS_ESCALATE_AFTER = 5

    async def _process_loop(self):
        """Run process() in a loop with per-error backoff.
        After _PROCESS_ESCALATE_AFTER consecutive errors, raises RuntimeError
        so the supervisor loop gets a clean restart (fresh namespace, reset state).
        A single successful call resets the consecutive counter.
        """
        consecutive_errors = 0
        successful_runs = 0
        while self._running:
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
                logger.error(f"[{self.name}] process() error #{consecutive_errors}: {e}")
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
                    raise RuntimeError(
                        f"process() failed {consecutive_errors} times in a row, last error: {e}"
                    ) from e
                # Exponential backoff before next attempt
                await asyncio.sleep(min(2**consecutive_errors, 30))
                continue
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break

    async def _heartbeat_loop(self, interval: float = 10.0):
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self._publish_heartbeat("running")
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _publish_heartbeat(self, state: str):
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

    async def handle_task(self, payload: dict) -> Any:
        if not self._fn_handle_task:
            return {"error": f"Agent '{self.name}' has no handle_task function."}
        try:
            result = await self._fn_handle_task(self._api, payload)
            return result or {}
        except Exception as e:
            err_str = traceback.format_exc()
            logger.error(f"[{self.name}] handle_task() error: {e}")
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

    def __init__(self, broker: str, port: int, node_name: str):
        self.broker = broker
        self.port = port
        self.node_name = node_name
        self._agents: dict[str, _RemoteAgent] = {}  # name → agent
        self._pub_queue: asyncio.Queue = None  # created in run() inside the event loop
        self._running = False
        self._deps_ready: asyncio.Event = None  # set once aiomqtt/paho are importable
        self._start_time: float = time.time()  # for uptime reporting in heartbeat
        # Persistent state directory — survives reboots unlike /tmp
        self._state_dir = os.path.join(os.path.expanduser("~"), "wactorz", "state")
        os.makedirs(self._state_dir, exist_ok=True)

    # ── MQTT publish (queue-based, reconnect-safe) ────────────────────────────

    async def publish(self, topic: str, data: Any, retain: bool = False):
        payload = json.dumps(data) if not isinstance(data, (str, bytes)) else data
        if isinstance(payload, str):
            payload = payload.encode()
        await self._pub_queue.put((topic, payload, retain))

    # ── Spawn / stop agents ───────────────────────────────────────────────────

    async def spawn_agent(self, config: dict):
        if not isinstance(config, dict):
            logger.warning(f"[runner] spawn_agent: invalid config type {type(config)}, ignoring.")
            return
        name = config.get("name", f"agent-{uuid.uuid4().hex[:6]}")
        logger.info(f"[runner] Spawning agent '{name}'...")
        if name in self._agents:
            if config.get("replace", False):
                logger.info(f"[runner] Replacing agent '{name}'")
                await self.stop_agent(name)
            else:
                logger.info(f"[runner] Agent '{name}' already running (use replace=true)")
                return

        packages = config.get("install", [])
        if packages:
            await self._install_packages(packages)

        try:
            agent = _RemoteAgent(config, self, state_dir=self._state_dir)
            self._agents[name] = agent
            await agent.start()
            logger.info(f"[runner] Agent '{name}' started.")
        except Exception as e:
            logger.error(f"[runner] Failed to start agent '{name}': {e}")
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

    async def stop_agent(self, name: str, delete: bool = False):
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
            logger.info(f"[runner] Agent '{name}' permanently deleted from this node.")

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
                logger.debug(f"[runner] Failed to clear retained agents/{actor_id}/{metric}: {e}")

    async def stop_all(self):
        for name in list(self._agents):
            await self.stop_agent(name)

    async def _install_packages(self, packages: list):
        """Install pip packages on the edge node."""
        pkgs = " ".join(packages)
        logger.info(f"[runner] Installing: {pkgs}")
        proc = await asyncio.create_subprocess_shell(
            f"pip install {pkgs} --break-system-packages -q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(f"[runner] pip install warning: {stderr.decode()[:200]}")

    # ── Status heartbeat for the node itself ──────────────────────────────────

    async def _node_heartbeat_loop(self, interval: float = 10.0):
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

    async def _publisher_loop(self):
        """Uses paho-mqtt directly for reliable fire-and-forget publishing.
        aiomqtt v2.x wraps paho but its internal network loop doesn't get CPU
        time when we block on queue.get(), causing silent message loss.
        paho.loop_start() runs a background thread that handles ACKs/keepalives.
        """
        await self._deps_ready.wait()
        import paho.mqtt.client as paho_mqtt

        loop = asyncio.get_event_loop()

        def _connect():
            c = paho_mqtt.Client(client_id=f"runner-pub-{self.node_name}-{uuid.uuid4().hex[:6]}")
            _user = os.environ.get("MQTT_USERNAME") or None
            if _user:
                c.username_pw_set(_user, os.environ.get("MQTT_PASSWORD") or None)
            c.connect(self.broker, self.port, keepalive=60)
            c.loop_start()
            return c

        client = None
        while self._running:
            try:
                if client is None:
                    client = await loop.run_in_executor(None, _connect)
                    logger.info(f"[runner] Publisher connected to {self.broker}:{self.port}")

                item = await self._pub_queue.get()
                topic, payload = item[0], item[1]
                retain = item[2] if len(item) > 2 else False
                client.publish(topic, payload, qos=1, retain=retain)
                self._pub_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[runner] Publisher error: {e}. Reconnecting in 3s...")
                if client:
                    try:
                        client.loop_stop()
                        client.disconnect()
                    except Exception:
                        pass
                    client = None
                await asyncio.sleep(3)

        if client:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

    # ── MQTT subscriber task ──────────────────────────────────────────────────

    async def _subscriber_loop(self):
        """Subscribes to:
        nodes/{node_name}/spawn          — spawn a new agent
        nodes/{node_name}/stop           — stop a named agent
        nodes/{node_name}/stop_all       — stop all agents and shut down
        nodes/{node_name}/list           — publish list of running agents
        nodes/{node_name}/reply/#        — route replies back to waiting agents
        agents/by-name/+/task           — task addressed to a named agent
        """
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
                ) as client:
                    for topic in topics:
                        await client.subscribe(topic)
                    logger.info(f"[runner] Subscribed to control topics on node '{self.node_name}'")

                    async for msg in client.messages:
                        topic_str = str(msg.topic)
                        try:
                            data = json.loads(msg.payload.decode())
                        except Exception:
                            data = msg.payload.decode()

                        if topic_str == f"nodes/{self.node_name}/desired_state":
                            # Reconcile: start any agents in desired state not already running
                            if not msg.payload or not isinstance(data, dict):
                                continue
                            desired = data.get("agents", [])
                            if not desired:
                                continue
                            logger.info(
                                f"[runner] Reconciling desired state: {[a.get('name') for a in desired]}"
                            )
                            for agent_config in desired:
                                aname = agent_config.get("name")
                                if not aname:
                                    continue
                                if aname in self._agents:
                                    logger.info(f"[runner] '{aname}' already running, skipping.")
                                else:
                                    logger.info(
                                        f"[runner] Reconcile: starting missing agent '{aname}'"
                                    )

                                    def _log_exc(t):
                                        if not t.cancelled() and t.exception():
                                            logger.error(
                                                f"[runner] reconcile task failed: {t.exception()}"
                                            )

                                    task = asyncio.create_task(self.spawn_agent(agent_config))
                                    task.add_done_callback(_log_exc)

                        elif topic_str == f"nodes/{self.node_name}/spawn":
                            if not msg.payload:  # empty = retain-clear message, ignore
                                continue

                            def _log_task_exc(t):
                                if not t.cancelled() and t.exception():
                                    logger.error(
                                        f"[runner] spawn_agent task failed: {t.exception()}"
                                    )

                            task = asyncio.create_task(self.spawn_agent(data))
                            task.add_done_callback(_log_task_exc)
                            # Clear the retained message so this spawn doesn't
                            # re-fire every time the subscriber reconnects/restarts
                            asyncio.create_task(self.publish(topic_str, b"", retain=True))

                        elif topic_str == f"nodes/{self.node_name}/stop":
                            # Payload formats accepted:
                            #   {"name": "foo"}                → plain stop, state preserved
                            #   {"name": "foo", "delete": true} → permanent delete:
                            #     wipes state file + retained MQTT topics
                            #   "foo"                          → legacy bare-name plain stop
                            if isinstance(data, dict):
                                name = data.get("name")
                                do_delete = bool(data.get("delete", False))
                            else:
                                name = str(data)
                                do_delete = False
                            if name:
                                asyncio.create_task(self.stop_agent(name, delete=do_delete))

                        elif topic_str == f"nodes/{self.node_name}/migrate":
                            # Migrate a running agent to another node
                            # payload: {"name": "agent-name", "target_node": "rpi-bedroom"}
                            asyncio.create_task(self._migrate_agent(data))

                        elif topic_str == f"nodes/{self.node_name}/stop_all":
                            logger.info("[runner] stop_all received — shutting down.")
                            asyncio.create_task(self._shutdown())

                        elif topic_str == f"nodes/{self.node_name}/restart":
                            # Gracefully restart the runner process in-place using os.execv.
                            # Stops all agents cleanly, publishes a "restarting" heartbeat,
                            # then re-execs itself — same PID, same venv, clean state.
                            logger.info("[runner] Restart command received.")
                            asyncio.create_task(self._restart())

                        elif topic_str == f"nodes/{self.node_name}/restart_agent":
                            # Restart a single named agent without losing its config.
                            # Equivalent to stop + spawn with replace=true.
                            name = data.get("name") if isinstance(data, dict) else str(data)
                            asyncio.create_task(self._restart_agent(name))

                        elif topic_str == f"nodes/{self.node_name}/list":
                            await self.publish(
                                f"nodes/{self.node_name}/agents",
                                {
                                    "node": self.node_name,
                                    "agents": [
                                        {"name": a.name, "actor_id": a.actor_id}
                                        for a in self._agents.values()
                                    ],
                                    "timestamp": time.time(),
                                },
                            )

                        elif topic_str.startswith(f"nodes/{self.node_name}/reply/"):
                            # Route reply back to the waiting agent
                            delivered = False
                            for agent in self._agents.values():
                                if agent.deliver_reply(topic_str, data):
                                    delivered = True
                                    break
                            if not delivered:
                                # Log every key actually waiting so we can see
                                # the mismatch in one shot rather than 60s later.
                                waiting = []
                                for agent in self._agents.values():
                                    waiting.extend(list(agent._pending_replies.keys()))
                                logger.warning(
                                    f"[runner] Reply arrived on {topic_str} but "
                                    f"no agent had a matching pending future. "
                                    f"Waiting keys: {waiting!r}"
                                )

                        elif "/task" in topic_str:
                            # agents/by-name/{agent_name}/task
                            parts = topic_str.split("/")
                            if len(parts) >= 4:
                                agent_name = parts[2]
                                agent = self._agents.get(agent_name)
                                if agent and isinstance(data, dict):
                                    # Match local-actor semantics: handle_task
                                    # receives the FULL task envelope (text,
                                    # payload, etc.). Previously we unwrapped
                                    # data['payload'], which could be a bare
                                    # string and crashed agent code that calls
                                    # payload.get('text'). The local
                                    # DynamicAgent does NOT unwrap — it passes
                                    # msg.payload (the full dict) straight to
                                    # _fn_handle_task. Remote must agree, or
                                    # the same agent works locally and breaks
                                    # remotely (and vice versa).
                                    # We strip transport-level metadata so the
                                    # agent doesn't see _reply_topic / _remote_task
                                    # leaking into its payload.
                                    reply_topic = data.get("_reply_topic")
                                    payload = {
                                        k: v
                                        for k, v in data.items()
                                        if k not in ("_reply_topic", "_remote_task")
                                    }
                                    # If the sender wrapped a scalar in a
                                    # 'payload' field and that's literally all
                                    # there is, pass the scalar through —
                                    # preserves the older convention for
                                    # callers that send {'payload': 42} and
                                    # expect 42 in handle_task.
                                    if set(payload.keys()) == {"payload"} and not isinstance(
                                        payload["payload"], dict
                                    ):
                                        payload = payload["payload"]

                                    # CRITICAL: run handle_task in a background
                                    # task. The subscriber loop is a SEQUENTIAL
                                    # consumer (`async for msg in client.messages`)
                                    # — while we await handle_task here, no other
                                    # MQTT message gets dispatched. If the agent's
                                    # handle_task makes a round-trip RPC such as
                                    # agent.llm.chat() (publishes to main, awaits
                                    # the reply on this same MQTT client), the
                                    # reply CANNOT be delivered because we hold
                                    # the only consumer — so the call deadlocks
                                    # and times out 60s later, even though main
                                    # responded in ms. By offloading to a task,
                                    # the subscriber returns to the loop and is
                                    # free to deliver subsequent replies.
                                    async def _run_task(a, p, rt, an):
                                        try:
                                            result = await a.handle_task(p)
                                        except Exception as e:
                                            logger.error(
                                                f"[runner] handle_task error for '{an}': {e}"
                                            )
                                            result = {"error": str(e), "agent": an}
                                        if rt:
                                            if not isinstance(result, dict):
                                                result = {
                                                    "result": str(result)
                                                    if result is not None
                                                    else ""
                                                }
                                            try:
                                                await self.publish(rt, result)
                                            except Exception as e:
                                                logger.warning(
                                                    f"[runner] Reply publish failed "
                                                    f"for '{an}' → {rt}: {e}"
                                                )

                                    task = asyncio.create_task(
                                        _run_task(agent, payload, reply_topic, agent_name)
                                    )
                                    # Keep a reference so the task doesn't get
                                    # garbage-collected mid-flight, and so a
                                    # shutdown can cancel it cleanly.
                                    agent._tasks.append(task)
                                    task.add_done_callback(
                                        lambda t, _ts=agent._tasks: (
                                            _ts.remove(t) if t in _ts else None
                                        )
                                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning(f"[runner] Subscriber disconnected: {e}. Reconnecting in 3s...")
                    await asyncio.sleep(3)

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        self._pub_queue = asyncio.Queue()  # must be created inside the running event loop
        self._deps_ready = asyncio.Event()
        logger.info(f"[runner] Starting node '{self.node_name}' → broker {self.broker}:{self.port}")

        # Bootstrap missing deps in thread pool; publisher/subscriber wait on this event.
        asyncio.create_task(_bootstrap_deps_async(self._deps_ready))

        tasks = [
            asyncio.create_task(self._publisher_loop()),
            asyncio.create_task(self._subscriber_loop()),
            asyncio.create_task(self._node_heartbeat_loop()),
        ]

        await asyncio.sleep(1)  # let publisher connect before anything else fires
        logger.info(f"[runner] Node '{self.node_name}' online.")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop_all()
            for t in tasks:
                t.cancel()

    async def _restart_agent(self, name: str):
        """Restart a single agent without losing its config or persisted state.
        The agent's state file is left on disk — the fresh instance picks it up
        via _load_state() on startup, so no state is lost.
        """
        agent = self._agents.get(name)
        if not agent:
            logger.warning(f"[runner] restart_agent: '{name}' not running here")
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
        logger.info(f"[runner] Restarting agent '{name}'")
        await self.spawn_agent(config)

    async def _restart(self):
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

    async def _shutdown(self):
        self._running = False
        await self.stop_all()
        await self.publish(
            f"nodes/{self.node_name}/heartbeat",
            {"node": self.node_name, "status": "offline", "timestamp": time.time()},
        )
        # Drain before exit so the heartbeat reaches the broker
        await asyncio.sleep(0.3)
        sys.exit(0)

    async def _migrate_agent(self, payload: dict):
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
            logger.warning(f"[runner] migrate: agent '{name}' not running here")
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
                f"[runner] migrate '{name}': dropping non-JSON state keys "
                f"{dropped} — they cannot travel over MQTT"
            )

        # ── Remote → Local migration ──────────────────────────────────────────
        # target_node == "@main" is the sentinel from MainActor meaning:
        # "don't spawn anywhere — stop the agent and return its state to me".
        # Main re-spawns the agent on its own host using this snapshot.
        if target_node == "@main":
            return_token = payload.get("return_token", "")
            logger.info(
                f"[runner] Migrating '{name}' from {self.node_name} → local (main); "
                f"returning {len(safe_state)} state key(s)"
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
            logger.info(f"[runner] Migration of '{name}' to local (main) dispatched.")
            return

        if safe_state:
            config["_initial_state"] = safe_state
            logger.info(
                f"[runner] migrate '{name}': carrying {len(safe_state)} state key(s) "
                f"to '{target_node}': {list(safe_state.keys())}"
            )

        logger.info(f"[runner] Migrating '{name}' from {self.node_name} → {target_node}")

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
        logger.info(f"[runner] Migration of '{name}' to '{target_node}' dispatched.")


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
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

    runner = _RemoteRunner(broker=args.broker, port=args.port, node_name=node_name)

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _signal_handler():
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


async def _run_supervisor_tests():
    """Standalone tests. No MQTT broker required."""
    passed = 0
    failed = 0

    class _StubRunner:
        node_name = "test-node"

        def __init__(self):
            self._agents = {}
            self.events = []

        async def publish(self, topic, data):
            self.events.append((topic, data))

    def make_agent(code, max_restarts=3, restart_delay=0.01, poll_interval=0.01, escalate_after=5):
        runner = _StubRunner()
        config = {
            "name": "test-agent",
            "code": code,
            "max_restarts": max_restarts,
            "restart_delay": restart_delay,
            "poll_interval": poll_interval,
        }
        agent = _RemoteAgent(config, runner)
        agent._PROCESS_ESCALATE_AFTER = escalate_after
        agent._running = True  # start() sets this; we call _supervisor_loop directly in tests
        runner._agents["test-agent"] = agent
        return agent, runner

    def check(name, condition, detail=""):
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
    task.cancel()
    try:
        await task
    except Exception:
        pass
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
    task.cancel()
    try:
        await task
    except Exception:
        pass
    check("stop(): restart_count=0", agent._restart_count == 0)
    check("stop(): not failed", not agent._failed)

    # Test 5: health credit after 10 successful runs
    agent, runner = make_agent("async def process(agent): pass", poll_interval=0.001)
    agent._restart_count = 2
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.3)
    agent._running = False
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except Exception:
        pass
    check(
        "health credit: restart_count < 2", agent._restart_count < 2, f"got {agent._restart_count}"
    )

    # Test 6: compile error stops supervision
    agent, runner = make_agent("this is not valid python !!!")
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except Exception:
        pass
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
    task.cancel()
    try:
        await task
    except Exception:
        pass
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
