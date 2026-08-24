"""Subscriptions, rolling windows, and the contracts an agent declares."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from ...core.mqtt import mqtt_client

if TYPE_CHECKING:
    pass

from .awaitable import AWAITABLE_NONE
from .listener import run_subscription_listener

if TYPE_CHECKING:
    from .hosts import ApiHost

    # Typing-only base: it states what the host must provide and is gone
    # at runtime, so the real MRO is exactly what it was.
    _Host = ApiHost
else:
    _Host = object

logger = logging.getLogger(__name__)

#: Shown to generated code that calls subscribe() without a usable callback.
#: These messages are the contract: the model reads them and repairs its own
#: code, so they say what to write rather than only what went wrong.
CALLBACK_REQUIRED = (
    "agent.subscribe('{topic}', callback) requires a callable callback. "
    "Got: {got}. "
    "Define: async def on_msg(payload): ... then call agent.subscribe('{topic}', on_msg). "
    "For a one-shot read use: data = await agent.mqtt_get('{topic}')"
)

#: Shown when the callback exists but takes no payload argument.
CALLBACK_NEEDS_PAYLOAD = (
    "Subscribe callback must accept one argument (the payload dict). "
    "Got a function with no required parameters. "
    "Fix: async def {name}(payload): ..."
)

#: Shown when generated code awaits agent.window(), which is synchronous.
WINDOW_NOT_AWAITABLE = (
    "agent.window() is not a coroutine - do not use 'await'. "
    "Correct: agent.state['w'] = agent.window('topic', seconds=60)  # no await"
)


class StreamsMixin(_Host):
    """Mixed into AgentAPI; reads the actor through `self._actor`."""

    def subscribe(self, topic: str, callback: Any) -> Any:
        """Subscribe to an MQTT topic and call callback(payload_dict) for each message.
        Runs as a background task — setup() returns immediately.

        IMPORTANT: callback is REQUIRED and must be an async function.
        subscribe() is NOT awaitable and does NOT return data.
        For a one-shot read use: data = await agent.mqtt_get(topic)

        Correct usage in setup(agent):
            async def on_message(payload):
                agent.state['latest'] = payload.get('value')
            agent.subscribe('sensors/temperature', on_message)
        """
        if callback is None or not callable(callback):
            raise TypeError(CALLBACK_REQUIRED.format(topic=topic, got=type(callback).__name__))

        # Validate callback accepts exactly one argument (the payload)

        # Only signature() is guarded: a callback that cannot be inspected is
        # allowed through, but one we *can* inspect and that takes no payload is
        # refused. Raising inside the try would have been caught by it.
        try:
            sig = inspect.signature(callback)
        except (TypeError, ValueError) as exc:
            logger.debug("[%s] Cannot inspect callback: %s", self.name, exc)
        else:
            required = [p for p in sig.parameters.values() if p.default is inspect.Parameter.empty]
            if not required:
                raise TypeError(CALLBACK_NEEDS_PAYLOAD.format(name=callback.__name__))

        actor = self._actor

        # Wrap the callback so `await None` errors from LLM-generated code
        # (e.g. `await agent.persist(...)`) don't crash the listener.
        # We log the first occurrence, then silently suppress subsequent ones.
        _await_warned = False

        # ── Callback error tracking (actor-level, survives reconnects) ──────
        # Stored on the actor so:
        #   1. MQTT reconnects don't reset counts (closure vars would reset)
        #   2. process() success doesn't clear subscribe errors (_consecutive_errors
        #      is shared — a clean process() run was resetting callback error counts)
        #   3. Multiple subscriptions on the same actor share one error budget
        _cb_attr = f"_cb_err_{topic.replace('/', '_').replace('#', 'x').replace('+', 'y')}"
        # After this many escalations without recovery, stop the listener entirely
        # and mark the actor FAILED so the Supervisor can restart with fresh code.

        # Deduplication guard — prevent double-subscription if setup() is called
        # more than once (e.g. on reconnect). Same topic+callback combo gets one listener.
        # (topic, id(callback)) → callback. A dict rather than a set of keys:
        # id() is unique only among *live* objects, so holding the callback is
        # what stops its address being recycled by a later one and that
        # subscription silently skipped as a duplicate.
        sub_key = (topic, id(callback))
        if sub_key in actor._subscribed_topics:
            logger.debug("[%s] Already subscribed to %s — skipping duplicate", actor.name, topic)
            return AWAITABLE_NONE
        actor._subscribed_topics[sub_key] = callback

        task = asyncio.create_task(run_subscription_listener(actor, topic, callback))
        actor._tasks.append(task)

        # Auto-register subscription in TopicBus
        try:
            from ...core.topic_bus import TopicContract, get_topic_bus

            bus = get_topic_bus()
            if bus:
                existing = bus.registry.get(self.name)
                if existing:
                    if topic not in existing.subscribes:
                        existing.subscribes.append(topic)
                        bus.registry.register(existing)
                else:
                    contract = TopicContract(
                        name=self.name,
                        subscribes=[topic],
                        actor_id=self.actor_id,
                        node=getattr(actor, "_node", None),
                    )
                    bus.register_contract(contract)
        except Exception as exc:
            logger.debug(
                "[%s] Could not register the %s subscription with TopicBus: %s",
                self.name,
                topic,
                exc,
            )

        # Return an awaitable no-op so `await agent.subscribe(...)` doesn't crash.
        # LLMs frequently add `await` because setup() is async — this makes it safe.
        return AWAITABLE_NONE

    async def _publish_manifest(self) -> None:
        """Publish retained capability manifest so main/planner can discover this agent.
        Now includes full TopicContract (publishes, subscribes, triggers_when, schemas)
        so the planner can wire agents by data compatibility, not just by name.
        """
        actor = self._actor
        # Include TopicContract fields if declared
        contract = getattr(actor, "_topic_contract", None)
        manifest = {
            "name": self.name,
            "actor_id": self.actor_id,
            "node": getattr(actor, "_node", None),
            "description": getattr(actor, "description", ""),
            "capabilities": [],
            "input_schema": getattr(actor, "input_schema", {}),
            "output_schema": getattr(actor, "output_schema", {}),
            "publishes": sorted(self._published_topics),
            # TopicContract fields — populated via declare_contract()
            "subscribes": contract.subscribes if contract else [],
            "triggers_when": contract.triggers_when if contract else {},
            "produces_schema": contract.produces_schema if contract else {},
            "consumes_schema": contract.consumes_schema if contract else {},
            # Observed payload schemas — auto-captured from real publishes
            "observed_samples": contract.observed_samples if contract else {},
            "timestamp": time.time(),
        }
        await actor._mqtt_publish(f"agents/{self.actor_id}/manifest", manifest, retain=True)

    async def mqtt_get(self, topic: str, timeout: float = 10.0) -> Any | None:
        """Wait for one MQTT message on topic and return its parsed payload.
        Useful for reading live data published by remote agents.

        Example:
            stats = await agent.mqtt_get('rpi-room/cpu')
            cpu = stats.get('cpu_percent') if stats else None
        """
        try:
            import aiomqtt  # noqa: F401
        except ImportError:
            return None
        actor = self._actor
        result = []

        async def _fetch() -> None:
            try:
                async with mqtt_client(actor._mqtt_broker, actor._mqtt_port) as client:
                    await client.subscribe(topic)
                    async for msg in client.messages:
                        try:
                            result.append(json.loads(msg.payload.decode()))
                        except Exception:
                            result.append(msg.payload.decode())
                        return
            except Exception as exc:
                logger.debug("[%s] mqtt_get(%s) read failed: %s", self.name, topic, exc)

        try:
            await asyncio.wait_for(_fetch(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.debug("[%s] mqtt_get(%s) timed out after %ss", self.name, topic, timeout)
        return result[0] if result else None

    def window(self, topic: str, seconds: float = 300, max_size: int = 1000) -> Any:
        """Create a sliding time window over an MQTT topic stream.

        IMPORTANT: window() is synchronous — do NOT use await.
        CORRECT:  agent.state['w'] = agent.window('sensors/temp', seconds=60)
        WRONG:    agent.state['w'] = await agent.window(...)  # TypeError!

        Returns a StreamWindow with methods: mean, min, max, rising, falling,
        stable, absent_for, event_count, latest, count, values.

        Usage:
            async def setup(agent):
                agent.state['w'] = agent.window('sensors/temp', seconds=60)  # NO await

            async def process(agent):
                w = agent.state['w']
                avg = w.mean('value')
                mn  = w.min('value')
                mx  = w.max('value')
                if w.rising(threshold=3.0):
                    await agent.alert('Temperature rising fast!')
                if w.absent_for(60):
                    await agent.alert('Sensor stopped publishing!')
        """
        from ...core.topic_bus import StreamWindow, get_topic_bus

        w = None
        try:
            bus = get_topic_bus()
            if bus:
                w = bus.make_window(topic, seconds=seconds, max_size=max_size)
            else:
                w = StreamWindow(topic, seconds=seconds, max_size=max_size)
                w.start(self._actor._mqtt_broker, self._actor._mqtt_port)
        except Exception:
            logger.exception("[%s] agent.window() failed - using a local window", self.name)

        if w is None:
            # Last resort: a window fed directly from the broker, so generated
            # code gets something with the right shape rather than an exception.
            w = StreamWindow(topic, seconds=seconds, max_size=max_size)
            try:
                w.start(self._actor._mqtt_broker, self._actor._mqtt_port)
            except Exception as exc:
                logger.debug("[%s] Local window could not start: %s", self.name, exc)

        return UnAwaitableWindow(w)

    def declare_contract(
        self,
        publishes: Any = None,
        subscribes: Any = None,
        triggers_when: dict[str, Any] | None = None,
        produces_schema: dict[str, Any] | None = None,
        consumes_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Declare this agent's topic contract — what it produces and consumes.

        Call from setup() to make this agent discoverable by the planner
        and other agents via topic-based auto-wiring.

        Accepts common LLM kwarg variants:
          schema → produces_schema
          output_schema → produces_schema
          input_schema → consumes_schema
          topics → publishes

        Usage:
            async def setup(agent):
                agent.declare_contract(
                    publishes    = ['rpi-kitchen/camera/detections'],
                    subscribes   = ['homeassistant/state_changes/#'],
                    triggers_when= {'person_detected': True},
                    produces_schema = {'person_detected': 'bool', 'confidence': 'float'},
                )
        """
        # ── Accept common LLM kwarg aliases ────────────────────────────────
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

        # ── Coerce strings to single-element lists ─────────────────────────
        # LLMs often write publishes="topic" instead of publishes=["topic"]
        if isinstance(publishes, str):
            publishes = [publishes]
        if isinstance(subscribes, str):
            subscribes = [subscribes]

        from ...core.topic_bus import TopicContract, get_topic_bus

        contract = TopicContract(
            name=self.name,
            publishes=publishes or list(self._published_topics),
            subscribes=subscribes or [],
            triggers_when=triggers_when or {},
            produces_schema=produces_schema or {},
            consumes_schema=consumes_schema or {},
            actor_id=self.actor_id,
            node=getattr(self._actor, "_node", None),
        )
        bus = get_topic_bus()
        if bus:
            bus.register_contract(contract)
        # Also include in manifest so remote agents and planner can see it
        self._actor._topic_contract = contract
        asyncio.ensure_future(self._publish_manifest())
        return AWAITABLE_NONE  # safe to await

    def wiring_opportunities(self) -> list[dict[str, Any]]:
        """Return a list of other agents this agent can be auto-wired to,
        based on topic contract compatibility.

        Usage:
            opps = agent.wiring_opportunities()
            for o in opps:
                print(f"Can receive data from {o['producer']} via {o['topic']}")
        """
        from ...core.topic_bus import get_topic_bus

        bus = get_topic_bus()
        if not bus:
            return []
        pairs = bus.registry.find_wiring_opportunities()
        return [
            {"producer": p.name, "consumer": c.name, "topic": t}
            for p, c, t in pairs
            if p.name == self.name or c.name == self.name
        ]


class UnAwaitableWindow:
    """Wraps StreamWindow and raises a clear TypeError if accidentally awaited.

    We do NOT implement __await__ here. Yielding a StreamWindow from
    __await__ violates the awaitable protocol and causes
    `RuntimeError: Task got bad yield` in CPython's event loop.

    Instead, accidental `await agent.window(...)` is handled by:
      - Layer 2 (sanitizer): strips `await` from `agent.window()` at compile time
      - Layer 4 (_safe_invoke): catches TypeError in subscribe callbacks
    This wrapper exists solely for a clear error message if those layers miss it.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: Any) -> Any:
        return getattr(self._inner, name)

    def __repr__(self) -> str:
        return f"StreamWindow(topic={getattr(self._inner, 'topic', '?')}, seconds={getattr(self._inner, 'seconds', '?')})"

    def __await__(self) -> Any:
        raise TypeError(WINDOW_NOT_AWAITABLE)
        # Make this a generator so __await__ is syntactically valid
        return
        yield  # pragma: no cover
