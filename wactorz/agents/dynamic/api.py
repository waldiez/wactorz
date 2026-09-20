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

    def __init__(self, actor: DynamicAgent, agent_state: dict[str, Any]) -> None:
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
        except Exception as e:
            logger.exception("[%s] agent.llm.chat() failed", self._actor.name)
            return f"[LLM error: {e}]"
        else:
            return response

    async def complete(self, messages: list[Any], system: str = "") -> str:
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

    def __init__(self, actor: DynamicAgent) -> None:
        self._actor = actor
        self.name = actor.name
        self.actor_id = actor.actor_id
        # Shared mutable namespace — generated code can store anything here
        self.state: dict = {}
        # LLM interface — available if llm_provider was passed at spawn time
        self.llm = LLMInterface(actor, self.state) if actor._llm_provider else None
        # Auto-discovered topics this agent publishes to
        self._published_topics: set = set()
        #: One window per topic, so repeated `agent.window(...)` calls -- which
        #: generated code makes from a process loop -- return the one that has
        #: been filling rather than a fresh empty one on a new connection.
        self._windows: dict[str, Any] = {}

    async def stop(self) -> None:
        """End this agent. Its work is done and it should not come back.

        Call it and then return — this is not `exit`, so anything written after
        it still runs, against an agent that is already stopping.

            async def process(agent):
                if agent.state.get('done'):
                    await agent.stop()
                    return  # nothing below this line should run

        The agent leaves the dashboard, is not restored on the next restart,
        and its `cleanup()` runs on the way out. Nothing restarts it; ending is
        final, so raise an error instead if you want to be repaired or retried.
        """
        await self._actor.end_self()

    # ── The broker this agent's host is on ─────────────────────────────────
    # Read through to the actor rather than copied at construction: an actor's
    # broker is set after it is built -- by the supervisor's inject step, or by
    # a node handing its agent the address it dials -- so a copy taken here was
    # `localhost:1883` for every agent that had one. Generated code that opens
    # a connection of its own reads these.

    @property
    def _mqtt_broker(self) -> str:
        return self._actor._mqtt_broker

    @property
    def _mqtt_port(self) -> int:
        return self._actor._mqtt_port

    @property
    def node(self) -> str:
        """Which node this agent runs on, or ``"local"`` when it runs on main.

        Generated code puts this straight into topics —
        ``f"{agent.node}/{agent.name}/detections"`` — which is why main answers
        ``"local"`` rather than the empty string the rest of the framework uses
        for the same thing (see main's ``is_target_local``): an empty segment
        makes a topic with a doubled separator in it. Code comparing against
        ``""`` should accept ``"local"`` too.
        """
        return str(self._actor._node) if self._actor._node else "local"

    # ── LLM convenience shims ──────────────────────────────────────────────
    # Both spellings work: agent.chat(...) and agent.llm.chat(...). Generated
    # code uses each about as often, and an agent migrating between main and a
    # node must not have to be rewritten for the one its new host offers.

    async def chat(self, messages: Any, system: str = "", timeout: float = 60.0) -> str:
        """Multi-turn LLM call.

        ``messages`` is a list of {"role": "user"/"assistant", "content": "..."}.
        For a single-turn prompt, prefer ``agent.llm.chat("prompt")`` instead.
        """
        if self.llm is None:
            return "[No LLM configured for this agent]"
        # Allow callers passing a bare string by promoting it to a single
        # user-turn list — models write both.
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        try:
            return await asyncio.wait_for(
                self.llm.complete(messages, system=system), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning("[%s] chat timed out after %ss", self.name, timeout)
            return ""

    async def complete(self, messages: Any, system: str = "", timeout: float = 60.0) -> str:
        """Alias for chat() — matches LLMInterface.complete() naming."""
        return await self.chat(messages, system=system, timeout=timeout)

    async def ask_llm(self, prompt: str, system: str = "", timeout: float = 60.0) -> str:
        """Single-turn LLM call, giving up after ``timeout`` seconds.

        The spelling node-written programs use, because on a node this is the
        request that travels to main. It is the same call as
        ``agent.llm.chat(prompt)``, and both work in both places.

        The bound is applied here rather than passed down, because a provider
        need not accept one — and an agent that asked for a 5-second answer must
        not be left waiting a minute by one that does not. `wait_for`, not
        `asyncio.timeout`: a node may be running Python 3.10.
        """
        if self.llm is None:
            return "[No LLM configured for this agent]"
        try:
            return await asyncio.wait_for(self.llm.chat(prompt, system=system), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[%s] ask_llm timed out after %ss", self.name, timeout)
            return ""

    # ── Status ─────────────────────────────────────────────────────────────

    async def set_status(self, status: str) -> None:
        """Say what this agent is doing, for the line on its dashboard card.

        Free text, replaced each time. An agent that never calls this shows its
        description instead, which is what it did before.
        """
        self._actor._status_text = str(status)

    # ── MQTT ───────────────────────────────────────────────────────────────

    # ── Logging / alerting ─────────────────────────────────────────────────

    @property
    def logger(self) -> Any:
        """Compatibility shim — allows agent.logger.info/warning/error in generated code."""
        api = self

        class _LoggerShim:
            def info(self, msg: Any) -> None:
                asyncio.ensure_future(api.log(msg, "info"))

            def warning(self, msg: Any) -> None:
                asyncio.ensure_future(api.log(msg, "warning"))

            def error(self, msg: Any) -> None:
                asyncio.ensure_future(api.log(msg, "error"))

            def debug(self, msg: Any) -> None:
                asyncio.ensure_future(api.log(msg, "debug"))

        return _LoggerShim()

    def run_in_background(self, coro: Any) -> Any:
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
        except Exception as exc:
            logger.debug("[%s] Could not track a background task: %s", self.name, exc)
        return task

    # ── Persistence ────────────────────────────────────────────────────────

    def persist(self, key: str, value: Any) -> Any:
        self._actor.persist(key, value)
        return AWAITABLE_NONE  # safe to await

    def recall(self, key: str, default: Any = None) -> Any:
        """Load a persisted value. Returns `default` (None by default) if the
        key doesn't exist — same shape as dict.get(). A node stores what was
        persisted as JSON rather than as a pickle, and this is the same call
        either way, so the same agent code works in both places.

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

    def agents(self) -> list[dict[str, Any]]:
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
            for node_name, nd in main._known_nodes.items():
                if time.time() - nd.get("last_seen", 0) > 30:
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

    def nodes(self) -> list[dict[str, Any]]:
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
        # No main in this process, which on a node is the ordinary case rather
        # than a fault: the cluster-wide view lives there, and what is
        # answerable here is this node itself. Code that needs the whole
        # picture sends a task to main for it.
        if self._actor._node:
            return [{"node": self.node, "online": True, "agents": self._agent_names()}]
        return []

    def _agent_names(self) -> list[str]:
        """The agents in this process, by name."""
        registry = self._actor._registry
        return [actor.name for actor in registry.all_actors()] if registry else []

    def _local_contracts(self) -> list[tuple[str, Any]]:
        """Every agent in this process and the contract it declared, if any."""
        registry = self._actor._registry
        if not registry:
            return []
        return [
            (
                actor.name,
                getattr(actor, "_topic_contract", None) or getattr(actor, "_spawn_contract", None),
            )
            for actor in registry.all_actors()
        ]

    def topics(self, keyword: str = "") -> list[dict[str, Any]]:
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
        if not self._actor._node:
            return []
        # The node's own view: what the agents here publish and subscribe to.
        wanted = keyword.lower().strip()
        found: dict[str, list[dict[str, Any]]] = {}
        for name, contract in self._local_contracts():
            topics = set(contract.publishes) | set(contract.subscribes) if contract else set()
            for topic in topics:
                if wanted and wanted not in topic.lower():
                    continue
                found.setdefault(topic, []).append({"name": name, "node": self.node})
        return [{"topic": topic, "agents": found[topic]} for topic in sorted(found)]

    def capabilities(self, keyword: str = "") -> list[dict[str, Any]]:
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
        registry = self._actor._registry
        if not self._actor._node or not registry:
            return []
        # The node's own view, in the same shape main answers in.
        wanted = keyword.lower().strip()
        profiles = []
        for actor in registry.all_actors():
            description = str(getattr(actor, "description", "") or "")
            if wanted and wanted not in description.lower() and wanted not in actor.name.lower():
                continue
            profiles.append(
                {
                    "name": actor.name,
                    "description": description,
                    "capabilities": list(getattr(actor, "capabilities", []) or []),
                    "input_schema": getattr(actor, "input_schema", {}),
                    "output_schema": getattr(actor, "output_schema", {}),
                }
            )
        return profiles

    # ── Topic Bus API ───────────────────────────────────────────────────────

    # ── Time-series queries (for ML agents) ────────────────────────────────

    # ── Metrics ────────────────────────────────────────────────────────────

    def increment_processed(self) -> None:
        self._actor.metrics.messages_processed += 1

    def increment_errors(self) -> None:
        self._actor.metrics.errors += 1
