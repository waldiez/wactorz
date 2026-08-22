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
from typing import TYPE_CHECKING, Any

from ..lookup import find_main_actor

if TYPE_CHECKING:
    from .agent import DynamicAgent

from .awaitable import AWAITABLE_NONE
from .messaging import MessagingMixin
from .queries import QueriesMixin
from .streams import StreamsMixin

logger = logging.getLogger(__name__)


class LLMInterface:
    """Thin LLM wrapper exposed to generated code via agent.llm
    Tracks token usage and cost just like LLMAgent does.
    """

    def __init__(self, actor: DynamicAgent, agent_state: dict):
        self._actor = actor
        self._agent_state = agent_state  # reference to AgentAPI.state

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
            logger.error("[%s] agent.llm.chat() failed: %s", self._actor.name, e)
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


class AgentAPI(StreamsMixin, QueriesMixin, MessagingMixin):
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
        self.llm = LLMInterface(actor, self.state) if actor._llm_provider else None
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
    # AgentAPI, agents that migrate from a remote node back to main crash
    # immediately with "'AgentAPI' object has no attribute 'node'".
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
    # crashes with "'AgentAPI' object has no attribute 'chat'".
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
        """Alias for chat() — matches LLMInterface.complete() naming."""
        return await self.chat(messages, system=system, timeout=timeout)

    # ── MQTT ───────────────────────────────────────────────────────────────

    # ── Logging / alerting ─────────────────────────────────────────────────

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
        return AWAITABLE_NONE  # safe to await

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
        We do NOT substitute AWAITABLE_NONE here because that would break
        the `if agent.recall('key') is None:` idiom that existing agent
        code relies on.
        """
        value = self._actor.recall(key)
        return value if value is not None else default

    # ── Inter-agent messaging ──────────────────────────────────────────────

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

    # ── Topic Bus API ───────────────────────────────────────────────────────

    # ── Time-series queries (for ML agents) ────────────────────────────────

    # ── Metrics ────────────────────────────────────────────────────────────

    def increment_processed(self):
        self._actor.metrics.messages_processed += 1

    def increment_errors(self):
        self._actor.metrics.errors += 1
