"""Subscriptions, rolling windows, and the contracts an agent declares."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from ...core.mqtt import mqtt_client

if TYPE_CHECKING:
    pass

from .awaitable import AWAITABLE_NONE

if TYPE_CHECKING:
    from .hosts import ApiHost

    # Typing-only base: it states what the host must provide and is gone
    # at runtime, so the real MRO is exactly what it was.
    _Host = ApiHost
else:
    _Host = object

logger = logging.getLogger(__name__)


class StreamsMixin(_Host):
    """Mixed into AgentAPI; reads the actor through `self._actor`."""

    def subscribe(self, topic: str, callback):
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
            raise TypeError(
                f"agent.subscribe('{topic}', callback) requires a callable callback. "
                f"Got: {type(callback).__name__}. "
                f"Define: async def on_msg(payload): ... then call agent.subscribe('{topic}', on_msg). "
                f"For a one-shot read use: data = await agent.mqtt_get('{topic}')"
            )

        # Validate callback accepts exactly one argument (the payload)
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
        import asyncio

        actor = self._actor

        # Wrap the callback so `await None` errors from LLM-generated code
        # (e.g. `await agent.persist(...)`) don't crash the listener.
        # We log the first occurrence, then silently suppress subsequent ones.
        _await_warned = False

        async def _safe_invoke(cb, payload):
            nonlocal _await_warned
            try:
                await cb(payload)
            except TypeError as e:
                if "NoneType" in str(e) and "await" in str(e):
                    if not _await_warned:
                        logger.warning(
                            "[%s] subscribe callback has 'await None' error (suppressed): %s",
                            actor.name,
                            e,
                        )
                        _await_warned = True
                    # Swallow: a sync API method was awaited, harmless
                else:
                    raise

        # ── Callback error tracking (actor-level, survives reconnects) ──────
        # Stored on the actor so:
        #   1. MQTT reconnects don't reset counts (closure vars would reset)
        #   2. process() success doesn't clear subscribe errors (_consecutive_errors
        #      is shared — a clean process() run was resetting callback error counts)
        #   3. Multiple subscriptions on the same actor share one error budget
        _cb_attr = f"_cb_err_{topic.replace('/', '_').replace('#', 'x').replace('+', 'y')}"
        # After this many escalations without recovery, stop the listener entirely
        # and mark the actor FAILED so the Supervisor can restart with fresh code.
        _CB_MAX_ESCALATIONS = 5
        _CB_ERROR_REPORT_INTERVAL = 30.0  # seconds between escalations per error key

        async def _listener():
            try:
                import aiomqtt  # noqa: F401
            except ImportError:
                logger.error("[%s] aiomqtt not installed", actor.name)
                return
            while True:
                try:
                    async with mqtt_client(actor._mqtt_broker, actor._mqtt_port) as client:
                        await client.subscribe(topic)
                        logger.info("[%s] Subscribed to %s", actor.name, topic)
                        async for msg in client.messages:
                            try:
                                payload = json.loads(msg.payload.decode())
                            except Exception:
                                payload = {"raw": msg.payload.decode()}
                            try:
                                await _safe_invoke(callback, payload)
                                # Successful invocation — reset this topic's error budget
                                actor._cb_error_count.pop(topic, None)
                                actor._cb_error_last.pop(topic, None)
                            except Exception as e:
                                import time as _t
                                import traceback as _tb

                                now = _t.time()
                                last = actor._cb_error_last.get(topic, 0)
                                escalations = actor._cb_error_count.get(topic, 0)

                                logger.error(
                                    "[%s] subscribe callback error (escalation #%s/%s, topic=%s): %s",
                                    actor.name,
                                    escalations + 1,
                                    _CB_MAX_ESCALATIONS,
                                    topic,
                                    e,
                                )

                                # Rate-limit escalation to supervision
                                if (now - last) >= _CB_ERROR_REPORT_INTERVAL:
                                    escalations += 1
                                    actor._cb_error_count[topic] = escalations
                                    actor._cb_error_last[topic] = now

                                    fatal = escalations >= _CB_MAX_ESCALATIONS
                                    await actor._publish_error(
                                        phase="subscribe_callback",
                                        error=e,
                                        traceback_str=_tb.format_exc(),
                                        fatal=fatal,
                                    )

                                    if fatal:
                                        # Budget exhausted — stop looping, let Supervisor restart
                                        logger.critical(
                                            "[%s] subscribe callback on '%s' failed %sx — marking FAILED for Supervisor.",
                                            actor.name,
                                            topic,
                                            escalations,
                                        )
                                        from ...core.actor import ActorState

                                        actor.state = ActorState.FAILED
                                        return  # exits _listener task
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning("[%s] MQTT subscribe error: %s — retrying in 5s", actor.name, e)
                    await asyncio.sleep(5)

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

        task = asyncio.create_task(_listener())
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
        except Exception:
            pass  # TopicBus unavailable — not fatal

        # Return an awaitable no-op so `await agent.subscribe(...)` doesn't crash.
        # LLMs frequently add `await` because setup() is async — this makes it safe.
        return AWAITABLE_NONE

    async def _publish_manifest(self):
        """Publish retained capability manifest so main/planner can discover this agent.
        Now includes full TopicContract (publishes, subscribes, triggers_when, schemas)
        so the planner can wire agents by data compatibility, not just by name.
        """
        import time as _t

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
            "timestamp": _t.time(),
        }
        await actor._mqtt_publish(f"agents/{self.actor_id}/manifest", manifest, retain=True)

    async def mqtt_get(self, topic: str, timeout: float = 10.0) -> Any | None:
        """Wait for one MQTT message on topic and return its parsed payload.
        Useful for reading live data published by remote agents.

        Example:
            stats = await agent.mqtt_get('rpi-room/cpu')
            cpu = stats.get('cpu_percent') if stats else None
        """
        import asyncio

        try:
            import aiomqtt  # noqa: F401
        except ImportError:
            return None
        actor = self._actor
        result = []

        async def _fetch():
            try:
                async with mqtt_client(actor._mqtt_broker, actor._mqtt_port) as client:
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

    def window(self, topic: str, seconds: float = 300, max_size: int = 1000):
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

        class _UnAwaitableWindow:
            """Wraps StreamWindow and raises a clear TypeError if accidentally awaited.

            We do NOT implement __await__ here. Yielding a StreamWindow from
            __await__ violates the awaitable protocol and causes
            `RuntimeError: Task got bad yield` in CPython's event loop.

            Instead, accidental `await agent.window(...)` is handled by:
              - Layer 2 (sanitizer): strips `await` from `agent.window()` at compile time
              - Layer 4 (_safe_invoke): catches TypeError in subscribe callbacks
            This wrapper exists solely for a clear error message if those layers miss it.
            """

            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __repr__(self):
                return f"StreamWindow(topic={getattr(self._inner, 'topic', '?')}, seconds={getattr(self._inner, 'seconds', '?')})"

            def __await__(self):
                raise TypeError(
                    "agent.window() is not a coroutine — do not use 'await'. "
                    "Correct: agent.state['w'] = agent.window('topic', seconds=60)  # no await"
                )
                # Make this a generator so __await__ is syntactically valid
                return
                yield  # pragma: no cover

        try:
            bus = get_topic_bus()
            if bus:
                w = bus.make_window(topic, seconds=seconds, max_size=max_size)
            else:
                w = StreamWindow(topic, seconds=seconds, max_size=max_size)
                w.start(self._actor._mqtt_broker, self._actor._mqtt_port)
            if w is None:
                raise ValueError("StreamWindow construction returned None")
            return _UnAwaitableWindow(w)
        except Exception as e:
            # Last resort fallback — return a minimal no-op window that won't crash
            logger.error("[%s] agent.window() failed: %s — returning fallback window", self.name, e)
            w = StreamWindow(topic, seconds=seconds, max_size=max_size)
            try:
                w.start(self._actor._mqtt_broker, self._actor._mqtt_port)
            except Exception:
                pass
            return _UnAwaitableWindow(w)

    def declare_contract(
        self,
        publishes=None,
        subscribes=None,
        triggers_when: dict | None = None,
        produces_schema: dict | None = None,
        consumes_schema: dict | None = None,
        **kwargs,
    ):
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

    def wiring_opportunities(self) -> list[dict]:
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
