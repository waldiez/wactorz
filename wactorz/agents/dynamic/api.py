"""The surface generated agent code is handed as `agent`.

Two other places are written against this shape: the planner repairs generated
code from it, and every catalogue recipe calls it. Whether a method is sync or
async is part of that contract -- `agents/planner/validation.py` strips `await`
from the ones listed there as synchronous, so changing one here breaks code it
never sees.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ...core.actor import MessageType
from ...core.mqtt import mqtt_client
from ..lookup import find_main_actor

if TYPE_CHECKING:
    from .agent import DynamicAgent

logger = logging.getLogger(__name__)


class _AwaitableNone:
    """Sentinel that can be safely awaited (returns None) or used in bool context (False).

    LLMs writing async code inside DynamicAgent frequently add `await` to sync API
    calls like agent.subscribe(), agent.window(), agent.persist(), etc.  Returning
    this instead of bare None prevents 'TypeError: object NoneType can't be used
    in await expression' — the #1 runtime failure in LLM-generated agent code.
    """

    def __await__(self):
        return iter([])  # completes immediately, yields None

    def __bool__(self):
        return False

    def __repr__(self):
        return "None"


_AWAITABLE_NONE = _AwaitableNone()


class _LLMInterface:
    """Thin LLM wrapper exposed to generated code via agent.llm
    Tracks token usage and cost just like LLMAgent does.
    """

    def __init__(self, actor: DynamicAgent, agent_state: dict):
        self._actor = actor
        self._agent_state = agent_state  # reference to _AgentAPI.state

    async def chat(self, prompt: str, system: str = "") -> str:
        """Send a prompt to the LLM and return the response text."""
        provider = self._actor._llm_provider
        if provider is None:
            return "[No LLM configured for this agent]"
        try:
            # Build a minimal single-turn message
            messages = [{"role": "user", "content": prompt}]
            response, usage = await provider.complete(messages=messages, system=system)
            # Track cost on the actor metrics if it has those fields
            if hasattr(self._actor, "total_input_tokens"):
                self._actor._accrue_usage(usage)
                await self._actor._mqtt_publish(
                    f"agents/{self._actor.actor_id}/metrics",
                    self._actor._build_metrics(),
                )
            return response
        except Exception as e:
            logger.error(f"[{self._actor.name}] agent.llm.chat() failed: {e}")
            return f"[LLM error: {e}]"

    async def complete(self, messages: list, system: str = "") -> str:
        """Multi-turn version — pass a full messages list."""
        provider = self._actor._llm_provider
        if provider is None:
            return "[No LLM configured]"
        response, usage = await provider.complete(messages=messages, system=system)
        if hasattr(self._actor, "total_input_tokens"):
            self._actor._accrue_usage(usage)
            await self._actor._mqtt_publish(
                f"agents/{self._actor.actor_id}/metrics",
                self._actor._build_metrics(),
            )
        return response

    async def converse(self, user_message: str, system: str = "") -> str:
        """Stateful multi-turn chat — automatically maintains conversation history
        in agent.state['_chat_history']. Simplest way to build a chat agent.

        async def handle_task(agent, payload):
            reply = await agent.llm.converse(payload['text'], system="You are helpful.")
            return {"reply": reply}
        """
        history = self._agent_state.setdefault("_chat_history", [])
        history.append({"role": "user", "content": user_message})
        reply = await self.complete(messages=history, system=system)
        history.append({"role": "assistant", "content": reply})
        return reply


class _AgentAPI:
    """Clean API surface exposed to LLM-generated code via the `agent` parameter.
    Wraps the actual Actor internals so generated code can't break the framework.
    """

    def __init__(self, actor: DynamicAgent):
        self._actor = actor
        self.name = actor.name
        self.actor_id = actor.actor_id
        # Shared mutable namespace — generated code can store anything here
        self.state: dict = {}
        # LLM interface — available if llm_provider was passed at spawn time
        self.llm = _LLMInterface(actor, self.state) if actor._llm_provider else None
        # Auto-discovered topics this agent publishes to
        self._published_topics: set = set()
        # MQTT broker info — exposed so generated code can create aiomqtt clients
        self._mqtt_broker = actor._mqtt_broker
        self._mqtt_port = actor._mqtt_port

    # ── Identity properties (parity with _RemoteAgentAPI) ──────────────────
    # The remote API exposes `agent.node` as the node_name of the runner the
    # agent is running on. Generated code uses this for topic prefixing
    # patterns like f"{agent.node}/{agent.name}/detections" — common enough
    # that the LLM emits it routinely. Without the same property on local
    # _AgentAPI, agents that migrate from a remote node back to main crash
    # immediately with "'_AgentAPI' object has no attribute 'node'".
    #
    # The canonical "this agent is local" value across the rest of the
    # framework (spawn registry, desired_state, list_nodes filters) is the
    # empty string "" — see main_actor's `is_target_local` check which
    # treats ("", "local", "main") as equivalent. For *display* though, an
    # empty string concatenated into a topic produces a malformed leading
    # slash. We compromise by returning "local" so f-strings stay readable
    # and topics stay valid; user code that compares against "" should be
    # updated to also accept "local".
    @property
    def node(self) -> str:
        node = getattr(self._actor, "_node", None)
        if node:
            return str(node)
        return "local"

    # ── LLM convenience shims (parity with remote _RemoteAgentAPI) ─────────
    # The remote runner exposes agent.chat(messages, ...) directly on the
    # API object — generated code written on a remote node will use that
    # form. Without the same surface here, migrating an agent local→remote
    # and back (or copy-pasting code originally written for a remote node)
    # crashes with "'_AgentAPI' object has no attribute 'chat'".
    #
    # These delegate to agent.llm so generated code keeps working in both
    # environments. Both forms — agent.chat(...) and agent.llm.chat(...) —
    # are valid; pick whichever feels cleaner in your code.

    async def chat(self, messages, system: str = "", timeout: float = 60.0) -> str:
        """Multi-turn LLM call — mirrors _RemoteAgentAPI.chat() so the same
        generated code runs locally and remotely.

        ``messages`` is a list of {"role": "user"/"assistant", "content": "..."}.
        For a single-turn prompt, prefer ``agent.llm.chat("prompt")`` instead.
        """
        if self.llm is None:
            return "[No LLM configured for this agent]"
        # Allow callers passing a bare string by promoting it to a single
        # user-turn list — same forgiveness the remote side offers in practice.
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        return await self.llm.complete(messages, system=system)

    async def complete(self, messages, system: str = "", timeout: float = 60.0) -> str:
        """Alias for chat() — matches _LLMInterface.complete() naming."""
        return await self.chat(messages, system=system, timeout=timeout)

    # ── MQTT ───────────────────────────────────────────────────────────────

    async def publish(self, topic: str, data: Any):
        """Publish data to an MQTT topic. Auto-registers topic in capability manifest
        and TopicBus contract so the agent is discoverable without explicit declare_contract().
        On every publish, captures the actual payload schema (field names + types)
        so the planner and other agents know the real field names — not guesses.
        """
        await self._actor._mqtt_publish(topic, data)

        is_new_topic = topic not in self._published_topics

        # ── Auto-capture observed schema from real payloads ────────────────
        # This solves the "temp" vs "temperature" vocabulary mismatch:
        # the schema reflects what the code ACTUALLY publishes.
        # Uses TopicContract.update_observed() — a proper dataclass field,
        # not monkey-patched attributes.
        try:
            from ...core.topic_bus import TopicContract, get_topic_bus

            bus = get_topic_bus()
            if bus:
                existing = bus.registry.get(self.name)
                if existing:
                    if is_new_topic and topic not in existing.publishes:
                        existing.publishes.append(topic)
                    # Record actual field names on every publish (first call
                    # per topic populates; subsequent calls are no-ops if
                    # fields haven't changed, but cheap either way)
                    if isinstance(data, dict):
                        existing.update_observed(topic, data)
                        # Also keep produces_schema in sync
                        for k, v in (
                            existing.observed_samples.get(topic, {}).get("fields", {}).items()
                        ):
                            existing.produces_schema[k] = v
                    bus.registry.register(existing)
                elif is_new_topic:
                    # Create minimal contract from published topics
                    contract = TopicContract(
                        name=self.name,
                        publishes=list(self._published_topics | {topic}),
                        actor_id=self.actor_id,
                        node=getattr(self._actor, "_node", None),
                    )
                    if isinstance(data, dict):
                        contract.update_observed(topic, data)
                        # Bootstrap produces_schema from observed
                        contract.produces_schema = dict(
                            contract.observed_samples.get(topic, {}).get("fields", {})
                        )
                    bus.register_contract(contract)
        except Exception:
            pass  # TopicBus unavailable — not fatal

        if is_new_topic:
            self._published_topics.add(topic)
            await self._publish_manifest()

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
        import json

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
                            f"[{actor.name}] subscribe callback has "
                            f"'await None' error (suppressed): {e}"
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
                logger.error(f"[{actor.name}] aiomqtt not installed")
                return
            while True:
                try:
                    async with mqtt_client(actor._mqtt_broker, actor._mqtt_port) as client:
                        await client.subscribe(topic)
                        logger.info(f"[{actor.name}] Subscribed to {topic}")
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
                                    f"[{actor.name}] subscribe callback error "
                                    f"(escalation #{escalations + 1}/{_CB_MAX_ESCALATIONS},"
                                    f" topic={topic}): {e}"
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
                                            f"[{actor.name}] subscribe callback on '{topic}' "
                                            f"failed {escalations}x — marking FAILED for Supervisor."
                                        )
                                        from ...core.actor import ActorState

                                        actor.state = ActorState.FAILED
                                        return  # exits _listener task
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"[{actor.name}] MQTT subscribe error: {e} — retrying in 5s")
                    await asyncio.sleep(5)

        # Deduplication guard — prevent double-subscription if setup() is called
        # more than once (e.g. on reconnect). Same topic+callback combo gets one listener.
        # (topic, id(callback)) → callback. A dict rather than a set of keys:
        # id() is unique only among *live* objects, so holding the callback is
        # what stops its address being recycled by a later one and that
        # subscription silently skipped as a duplicate.
        sub_key = (topic, id(callback))
        if sub_key in actor._subscribed_topics:
            logger.debug(f"[{actor.name}] Already subscribed to {topic} — skipping duplicate")
            return _AWAITABLE_NONE
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
        return _AWAITABLE_NONE

    async def publish_detection(self, data: Any):
        """Convenience: publish to agents/{id}/detections"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/detections", data)

    async def publish_result(self, data: Any):
        """Convenience: publish to agents/{id}/result"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/result", data)

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

    # ── Logging / alerting ─────────────────────────────────────────────────

    async def log(self, message: str, level: str = "info"):
        """Add a message to the event log visible in the dashboard."""
        # Encode safely for Windows terminals that can't handle all unicode
        safe_msg = message.encode("ascii", errors="replace").decode("ascii")
        getattr(logger, level, logger.info)(f"[{self.name}] {safe_msg}")
        await self._actor._mqtt_publish(
            f"agents/{self._actor.actor_id}/logs",
            {"type": "log", "message": message, "timestamp": time.time()},
        )

    @property
    def logger(self):
        """Compatibility shim — allows agent.logger.info/warning/error in generated code."""
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

    async def alert(self, message: str, severity: str = "warning"):
        """Trigger an alert visible in the dashboard."""
        await self._actor._mqtt_publish(
            f"agents/{self._actor.actor_id}/alert",
            {
                "actor_id": self._actor.actor_id,
                "name": self.name,
                "message": message,
                "severity": severity,
                "timestamp": time.time(),
            },
        )

    async def notify_user(self, text: str):
        """Push a user-facing chat message to the chat panel (see Actor.notify_user).
        Use this — not log() or alert() — when the user should see the message in
        chat, e.g. when a long task finishes or an autonomous agent has news.
        """
        await self._actor.notify_user(text)

    def run_in_background(self, coro):
        """Schedule a coroutine on the actor's event loop and track it on the actor
        so it is cancelled cleanly on stop (same lifecycle as subscribe()).
        Returns the asyncio.Task.

        Use for slow work you don't want to block handle_task on: return a quick
        ack from handle_task, do the work in here, then call notify_user() with
        the result when it's ready.
        """
        task = asyncio.create_task(coro)
        try:
            self._actor._tasks.append(task)
        except Exception:
            pass
        return task

    # ── Persistence ────────────────────────────────────────────────────────

    def persist(self, key: str, value: Any):
        self._actor.persist(key, value)
        return _AWAITABLE_NONE  # safe to await

    def recall(self, key: str, default: Any = None) -> Any:
        """Load a persisted value. Returns `default` (None by default) if the
        key doesn't exist — same shape as dict.get(), and identical to the
        remote runner's _RemoteAgentAPI.recall() so the same agent code
        works on local and remote without modification.

        Note: recall() is synchronous — do NOT use await.
        The sanitizer strips `await agent.recall(...)` at compile time.
        If an accidental `await` slips through, the _safe_invoke callback
        wrapper (layer 4) will catch the TypeError.

        The return value is always the real persisted value (or the default).
        We do NOT substitute _AWAITABLE_NONE here because that would break
        the `if agent.recall('key') is None:` idiom that existing agent
        code relies on.
        """
        value = self._actor.recall(key)
        return value if value is not None else default

    # ── Inter-agent messaging ──────────────────────────────────────────────

    async def send_to(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Any | None:
        """Send a TASK to another agent by name and wait for its result.

        Routing priority:
          1. Local registry — fast in-process mailbox
          2. Remote node via MQTT — agents/by-name/{name}/task with reply topic
          3. Returns error dict if the agent is unknown in both

        Works with local DynamicAgent/LLMAgent AND remote _RemoteAgent on any node.
        """
        registry = self._actor._registry
        if not registry:
            logger.warning(f"[{self.name}] send_to: no registry")
            return None

        target = registry.find_by_name(agent_name)

        if target:
            # ── Local path ────────────────────────────────────────────────────
            import uuid as _uuid

            task_id = str(_uuid.uuid4())[:8]
            future = asyncio.get_event_loop().create_future()
            self._actor._result_futures[task_id] = future
            if not isinstance(payload, dict):
                payload = {"message": payload, "text": str(payload)}
            payload = dict(payload)
            payload["_task_id"] = task_id
            payload["_reply_to"] = self._actor.actor_id
            await self._actor.send(target.actor_id, MessageType.TASK, payload)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"[{self.name}] send_to '{agent_name}' timed out after {timeout}s")
                return {"error": f"Timeout waiting for '{agent_name}'"}
            finally:
                self._actor._result_futures.pop(task_id, None)

        # ── Remote path: find agent on a known node ───────────────────────────
        remote_node = None
        main = find_main_actor(registry)
        if main:
            for node_name, nd in main._known_nodes.items():
                if agent_name in nd.get("agents", []):
                    remote_node = node_name
                    break

        if not remote_node:
            logger.warning(
                f"[{self.name}] send_to: agent '{agent_name}' not found locally or remotely"
            )
            return {"error": f"Agent '{agent_name}' not found"}

        import uuid as _uuid

        reply_topic = f"agents/by-name/{self.name}/reply/{_uuid.uuid4().hex[:8]}"

        if not isinstance(payload, dict):
            payload = {"message": payload, "text": str(payload)}
        payload = dict(payload)
        payload["_reply_topic"] = reply_topic
        payload["_remote_task"] = True

        future = asyncio.get_event_loop().create_future()
        if not hasattr(self._actor, "_result_futures"):
            self._actor._result_futures = {}
        self._actor._result_futures[reply_topic] = future

        await self._actor._mqtt_publish(f"agents/by-name/{agent_name}/task", payload)

        async def _wait_reply():
            try:
                broker = getattr(self._actor, "_mqtt_broker", "localhost")
                port = getattr(self._actor, "_mqtt_port", 1883)
                async with mqtt_client(broker, port) as client:
                    await client.subscribe(reply_topic)
                    async for msg in client.messages:
                        try:
                            import json as _json

                            data = _json.loads(msg.payload.decode())
                            if not future.done():
                                future.set_result(data)
                        except Exception:
                            pass
                        return
            except Exception as e:
                if not future.done():
                    future.set_exception(e)

        reply_task = asyncio.create_task(_wait_reply())
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                f"[{self.name}] send_to '{agent_name}' on '{remote_node}' timed out after {timeout}s"
            )
            return {"error": f"Timeout waiting for remote '{agent_name}'"}
        finally:
            reply_task.cancel()
            self._actor._result_futures.pop(reply_topic, None)

    async def send_to_many(self, tasks: list[tuple[str, Any]], timeout: float = 60.0) -> list:
        """Send tasks to multiple agents IN PARALLEL and collect all results.

        tasks: list of (agent_name, payload) tuples
        Returns list of results in the same order.

        Example:
            results = await agent.send_to_many([
                ("weather-agent", {"city": "Athens"}),
                ("news-agent",    {"topic": "AI"}),
            ])
            weather, news = results[0], results[1]
        """
        coros = [self.send_to(name, payload, timeout) for name, payload in tasks]
        return list(await asyncio.gather(*coros, return_exceptions=True))

    def agents(self) -> list[dict]:
        """Return all running agents — both local and remote.

        Local agents come from the registry. Remote agents are sourced from
        main._known_nodes (populated by node heartbeats). Each entry includes
        a 'remote' flag and 'node' field so callers can route correctly.

        Example:
            available = agent.agents()
            remote_workers = [a for a in available if a.get("remote")]
        """
        registry = self._actor._registry
        result = []
        seen = set()

        # ── Local agents from registry ────────────────────────────────────────
        if registry:
            for actor in registry.all_actors():
                seen.add(actor.name)
                result.append(
                    {
                        "name": actor.name,
                        "type": type(actor).__name__,
                        "description": (
                            getattr(actor, "description", "")
                            or getattr(actor, "system_prompt", "")[:100]
                            or ""
                        ),
                        "state": actor.state.name
                        if hasattr(actor.state, "name")
                        else str(actor.state),
                        "remote": False,
                        "node": None,
                    }
                )

        # ── Remote agents from live node heartbeats ───────────────────────────
        main = find_main_actor(registry)
        if main:
            import time as _t

            for node_name, nd in main._known_nodes.items():
                if _t.time() - nd.get("last_seen", 0) > 30:
                    continue  # node is offline — skip
                for aname in nd.get("agents", []):
                    if aname in seen:
                        continue  # already in local registry (shouldn't happen but guard it)
                    seen.add(aname)
                    desc = main._agent_manifests.get(aname, {}).get("description", "")
                    result.append(
                        {
                            "name": aname,
                            "type": "RemoteAgent",
                            "description": desc,
                            "state": "running",
                            "remote": True,
                            "node": node_name,
                        }
                    )

        return result

    def nodes(self) -> list[dict]:
        """Return all known remote nodes with online status and running agents.
        Only available when the agent is running under a MainActor system.

        Example:
            for nd in agent.nodes():
                status = 'online' if nd['online'] else 'offline'
                await agent.log(f"{nd['node']}: {status}, agents: {nd['agents']}")
        """
        main = find_main_actor(self._actor._registry)
        if main:
            return main.list_nodes()
        return []

    def topics(self, keyword: str = "") -> list[dict]:
        """Return all known MQTT topics published by agents, optionally filtered by keyword.
        Each entry: {"topic": str, "agents": [{"name", "node", "description"}, ...]}

        Example:
            temp_topics = agent.topics("temp")   # find all temperature-related topics
            all_topics  = agent.topics()         # everything
            for t in temp_topics:
                data = await agent.mqtt_get(t["topic"])
        """
        main = find_main_actor(self._actor._registry)
        if main:
            return main.list_topics(keyword)
        return []

    def capabilities(self, keyword: str = "") -> list[dict]:
        """Return all known agents with their full capability profile.
        Each entry: {"name", "description", "capabilities", "input_schema", "output_schema"}

        Example:
            weather_agents = agent.capabilities("weather")
            for a in weather_agents:
                print(a["input_schema"])   # know exactly what to send
                print(a["output_schema"])  # know exactly what to expect back
        """
        main = find_main_actor(self._actor._registry)
        if main:
            return main.list_capabilities(keyword)
        return []

    async def delegate(self, agent_name: str, payload: Any, timeout: float = 60.0) -> Any | None:
        """Alias for send_to() — cleaner name for planner/coordinator agents."""
        return await self.send_to(agent_name, payload, timeout=timeout)

    async def mqtt_get(self, topic: str, timeout: float = 10.0) -> Any | None:
        """Wait for one MQTT message on topic and return its parsed payload.
        Useful for reading live data published by remote agents.

        Example:
            stats = await agent.mqtt_get('rpi-room/cpu')
            cpu = stats.get('cpu_percent') if stats else None
        """
        import asyncio
        import json

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

    # ── Topic Bus API ───────────────────────────────────────────────────────

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
            logger.error(f"[{self.name}] agent.window() failed: {e} — returning fallback window")
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
        return _AWAITABLE_NONE  # safe to await

    async def publish_world_state(self, key: str, data: Any, retain: bool = True):
        """Publish a piece of world state to the shared retained state hub.
        Other agents can read this without making a request — it's always there.

        Topic: agents/{agent_name}/data/{key}

        Usage:
            await agent.publish_world_state('person_present', {'present': True, 'zone': 'kitchen'})
            await agent.publish_world_state('energy', {'kwh': 2.3, 'cost': 0.45})
        """
        from ...core.topic_bus import get_topic_bus

        bus = get_topic_bus()
        if bus:
            await bus.state_hub.publish_agent_data(self.name, key, data)
        else:
            topic = f"agents/{self.name}/data/{key}"
            await self.publish(topic, data)

    async def read_world_state(self, topic: str, timeout: float = 2.0) -> Any | None:
        """Read a retained world state topic — returns immediately if cached,
        otherwise waits up to timeout seconds for the retained message.

        Usage:
            presence = await agent.read_world_state('home/presence/kitchen')
            energy   = await agent.read_world_state('home/energy/current')
            ha_state = await agent.read_world_state('home/state/light/light.living_room')
        """
        return await self.mqtt_get(topic, timeout=timeout)

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

    # ── Time-series queries (for ML agents) ────────────────────────────────

    def query_ts(
        self,
        hours: float = 24,
        topic: str | None = None,
        entity_id: str | None = None,
        field: str | None = None,
        limit: int = 100_000,
        as_dataframe: bool = False,
    ) -> Any:
        """Query historical sensor readings from the time-series store.

        Returns a list of dicts by default. Set as_dataframe=True to get
        a pandas DataFrame (requires pandas installed).

        SYNCHRONOUS — do NOT await.

        Usage:
            # Get last 24h of temperature data
            rows = agent.query_ts(hours=24, field='temp')

            # Get as pandas DataFrame for ML
            df = agent.query_ts(hours=168, entity_id='sensor.kitchen_temp', as_dataframe=True)

            # Train a model
            from sklearn.ensemble import IsolationForest
            model = IsolationForest().fit(df[['value']])
            agent.persist('anomaly_model', model)
        """
        from ...core.persistence import get_db

        db = get_db()
        if not db:
            logger.warning(f"[{self.name}] query_ts: persistence not initialised")
            return [] if not as_dataframe else None

        rows = db.query_sensor(
            hours=hours,
            topic=topic,
            entity_id=entity_id,
            field=field,
            limit=limit,
        )

        if as_dataframe:
            try:
                import pandas as pd

                return pd.DataFrame(rows)
            except ImportError:
                logger.warning(f"[{self.name}] pandas not installed — returning list of dicts")
                return rows
        return rows

    def query_detections(
        self,
        hours: float = 24,
        agent_name: str | None = None,
        class_name: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 50_000,
        as_dataframe: bool = False,
    ) -> Any:
        """Query historical object detections (YOLO, camera agents).

        Usage:
            # All person detections in last 12 hours
            rows = agent.query_detections(hours=12, class_name='person')

            # As DataFrame for analysis
            df = agent.query_detections(hours=48, min_confidence=0.8, as_dataframe=True)
        """
        from ...core.persistence import get_db

        db = get_db()
        if not db:
            return [] if not as_dataframe else None

        rows = db.query_detections(
            hours=hours,
            agent=agent_name,
            class_name=class_name,
            min_confidence=min_confidence,
            limit=limit,
        )

        if as_dataframe:
            try:
                import pandas as pd

                return pd.DataFrame(rows)
            except ImportError:
                return rows
        return rows

    def query_ha_states(
        self,
        hours: float = 24,
        entity_id: str | None = None,
        domain: str | None = None,
        limit: int = 50_000,
        as_dataframe: bool = False,
    ) -> Any:
        """Query historical Home Assistant state changes.

        Usage:
            # All light state changes in last week
            df = agent.query_ha_states(hours=168, domain='light', as_dataframe=True)

            # Specific entity history
            rows = agent.query_ha_states(hours=24, entity_id='sensor.kitchen_temp')
        """
        from ...core.persistence import get_db

        db = get_db()
        if not db:
            return [] if not as_dataframe else None

        rows = db.query_ha_states(
            hours=hours,
            entity_id=entity_id,
            domain=domain,
            limit=limit,
        )

        if as_dataframe:
            try:
                import pandas as pd

                return pd.DataFrame(rows)
            except ImportError:
                return rows
        return rows

    def ts_stats(self) -> dict:
        """Return row counts for all time-series tables.
        Useful for checking how much data is available before training.

        Usage:
            stats = agent.ts_stats()
            # {'sensor_readings': 145230, 'detections': 8920, ...}
        """
        from ...core.persistence import get_db

        db = get_db()
        if not db:
            return {}
        return db.stats()

    # ── Metrics ────────────────────────────────────────────────────────────

    def increment_processed(self):
        self._actor.metrics.messages_processed += 1

    def increment_errors(self):
        self._actor.metrics.errors += 1
