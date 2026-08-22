"""PlannerAgent — On-demand task orchestrator with plan caching and auto-spawning.

Spawned by MainActor when a task is too complex for a single agent.
Pipeline:
  1. Check plan cache — reuse structure if task is similar and agents still alive
  2. Discover available workers
  3. LLM decomposes task into steps (with agent assignments + spawn configs for missing agents)
  4. Spawn any missing agents before execution
  5. Fan out steps (parallel where possible), inject context into dependent steps
  6. Synthesize all results into a coherent answer
  7. Cache the plan, report back to main, self-terminate

Trigger explicitly:   "coordinate: get weather and news then summarize"
Trigger explicitly:   "plan: ..."
Auto-triggered by MainActor when complexity heuristic fires.
"""

import asyncio
import json
import logging
import time
from typing import Any

from ...core.actor import Actor, Message, MessageType
from ..llm_agent import LLMProvider, accumulate_global_cost
from ..lookup import find_main_actor
from ..mixins.spawning import SpawnMixin
from ..prompts.planner_prompts import (
    DECOMPOSE_PROMPT,
)
from .cache import PLAN_CACHE_KEY, select_cached_plan, with_plan_cached
from .context import ContextMixin
from .detection import is_pipeline_request
from .execution import ExecutionMixin
from .parsing import extract_json_array, task_hash
from .pipeline import PipelineMixin
from .validation import validate_pipeline_code

logger = logging.getLogger(__name__)

_SKIP_AGENTS = {
    "main",
    "monitor",
    "installer",
    "home-assistant-agent",
    "home-assistant-hardware",
    "home-assistant-automation",
    "anomaly-detector",
    "code-agent",
}


class PlannerAgent(Actor, SpawnMixin, ContextMixin, ExecutionMixin, PipelineMixin):
    """On-demand orchestrator. Spawned per complex task, self-terminates when done."""

    #: Hard cap on a planner's life. A caller awaiting its reply must not wait
    #: longer than this plus delivery: once the cap fires the planner is gone
    #: and no reply can follow, so any remaining wait is spent on nothing.
    DEFAULT_MAX_LIFETIME_S = 90.0

    def __init__(
        self,
        llm_provider: LLMProvider | None = None,
        task: str = "",
        reply_to_id: str = "",
        reply_task_id: str = "",
        auto_terminate: bool = True,
        plan_only: bool = False,
        approved_plan: dict[str, Any] | None = None,
        max_lifetime_s: float = DEFAULT_MAX_LIFETIME_S,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("name", "planner")
        super().__init__(**kwargs)
        self.llm = llm_provider
        self._task = task
        self._reply_to_id = reply_to_id
        self._reply_task_id = reply_task_id
        self._auto_terminate = auto_terminate
        # Dry-run support:
        #   plan_only=True       → produce a plan, return it as JSON, do NOT spawn.
        #   approved_plan=<dict> → skip planning, execute the supplied plan directly.
        # These are mutually exclusive in practice. If both are somehow set,
        # approved_plan takes precedence (it is checked first in _run_plan) and
        # the plan is executed; plan_only is ignored. on_start() enforces this
        # and logs it so the behaviour is never silent.
        self._plan_only = plan_only
        self._approved_plan = approved_plan
        self._result_futures: dict[str, asyncio.Future] = {}
        self._spawned_by_planner: list[str] = []  # agents we created this run
        # Per-agent spawn outcome: name → {"ok": bool, "error": str|None}
        # Sent back to main in the RESULT payload so it knows what actually happened.
        self._spawn_results: dict[str, dict[str, Any]] = {}

        # Lifecycle: a planner must never outlive its task. _max_lifetime_s is a
        # hard cap after which the planner self-removes regardless of state, so
        # proposal/pipeline/approved planners don't accumulate until an app
        # restart. _terminated guards against double-teardown; _lifetime_task
        # holds the watchdog so normal completion can cancel it.
        self._max_lifetime_s: float = max_lifetime_s
        self._terminated: bool = False
        self._lifetime_task: asyncio.Task | None = None

        # Cost tracking — accumulated across all llm.complete() calls this session
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cost_usd: float = 0.0
        self._last_period_cost_usd: float = 0.0

    def _current_task_description(self) -> str:
        return self._task[:60] if self._task else "waiting for task"

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self) -> None:
        await self._log(f"Planner ready. Task: {self._task[:80]}")

        # Enforce documented precedence: approved_plan wins over plan_only.
        if self._approved_plan and self._plan_only:
            await self._log(
                "Both approved_plan and plan_only set — approved_plan takes "
                "precedence; ignoring plan_only."
            )
            self._plan_only = False

        # Hard lifetime cap so planners never pile up until a restart.
        self._lifetime_task = asyncio.create_task(self._lifetime_watchdog())

        if self._task:
            asyncio.create_task(self._report_plan(self._task))

    async def on_stop(self) -> None:
        """Persist final cost metrics so lifetime spend survives agent termination."""
        if self.total_cost_usd > 0:
            self.persist(
                "_final_cost",
                {
                    "input_tokens": self.total_input_tokens,
                    "output_tokens": self.total_output_tokens,
                    "cost_usd": round(self.total_cost_usd, 6),
                    "name": self.name,
                    "stopped_at": time.time(),
                },
            )
        try:
            await self._mqtt_publish(
                f"agents/{self.actor_id}/metrics",
                self._build_metrics(),
            )
        except Exception as exc:
            logger.debug("[%s] Final metrics publish failed: %s", self.name, exc)

    def _build_metrics(self) -> dict[str, Any]:
        m = super()._build_metrics()
        m["input_tokens"] = self.total_input_tokens
        m["output_tokens"] = self.total_output_tokens
        m["cost_usd"] = round(self.total_cost_usd, 6)
        return m

    def _accrue_usage(self, usage: dict[str, Any]) -> None:
        """Accumulate token/cost usage returned by any llm.complete() call."""
        self.total_input_tokens += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd += usage.get("cost_usd", 0.0)
        delta = self.total_cost_usd - self._last_period_cost_usd
        if delta > 0:
            accumulate_global_cost(delta)
            self._last_period_cost_usd = self.total_cost_usd

    def _now_context(self) -> str:
        """Live date/time block for planning prompts. Resolves the user's timezone
        from main's facts (same source the scheduler uses) so a "tomorrow at 3pm"
        request is decomposed against the correct calendar date and zone.
        """
        user_tz = None
        if self._registry:
            main = find_main_actor(self._registry)
            if main:
                try:
                    user_tz = main.get_user_facts().get("pref_timezone")
                except Exception as exc:
                    logger.debug("[%s] No user timezone available: %s", self.name, exc)
        from ..llm_agent import current_time_context

        return current_time_context(user_tz)

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message) -> None:
        if msg.type == MessageType.TASK:
            payload = msg.payload if isinstance(msg.payload, dict) else {"text": str(msg.payload)}
            task_text = payload.get("text") or payload.get("task") or str(msg.payload)
            self._reply_to_id = (
                payload.get("_reply_to") or msg.reply_to or msg.sender_id or self._reply_to_id
            )
            task_id = payload.get("_task_id")
            await self._log(f"Received task: {task_text[:80]}")
            result = await self._run_plan(task_text)
            if self._reply_to_id:
                # Use the initiating task_id (from main) so the future resolves,
                # falling back to the message-level task_id if present
                resolve_id = self._reply_task_id or task_id
                reply: dict[str, Any] = {"result": result, "text": result}
                if resolve_id:
                    reply["_task_id"] = resolve_id
                if self._spawned_by_planner:
                    reply["spawned"] = self._spawned_by_planner
                await self.send(self._reply_to_id, MessageType.RESULT, reply)

    # ── Report wrapper (on_start path) ────────────────────────────────────

    async def _report_plan(self, task: str) -> None:
        """Run the plan and report the result back to main (used when task set at spawn time)."""
        result = await self._run_plan(task)
        if self._reply_to_id:
            reply = {
                "result": result,
                "text": result,
                "spawn_results": self._spawn_results,  # per-agent outcome dict
            }
            if self._reply_task_id:
                reply["_task_id"] = self._reply_task_id
            if self._spawned_by_planner:
                reply["spawned"] = self._spawned_by_planner
            await self.send(self._reply_to_id, MessageType.RESULT, reply)

    # ── Pipeline registry ──────────────────────────────────────────────────
    # Each pipeline rule is stored here so users can list / delete them later.
    # Stored in persistent state under key "_pipeline_rules".
    #
    # Schema per rule:
    # {
    #   "rule_id":    str,       # unique slug
    #   "task":       str,       # original user request
    #   "agents":     [str],     # names of spawned agents for this rule
    #   "created_at": float,
    # }

    # ── Pipeline detection & dispatch ──────────────────────────────────────

    async def _prune_stale_contracts(self) -> None:
        """Drop TopicBus contracts for agents that are no longer running.

        Planning reads the registry to decide what to reuse, so a contract
        left behind by a dead agent would offer a topic nobody publishes.
        """
        try:
            from ...core.topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus and self._registry:
                live = {a.name for a in self._registry.all_actors()}
                # Add remotely-running agents from main's known_nodes
                main = find_main_actor(self._registry)
                if main:
                    for nd in main._known_nodes.values():
                        if time.time() - nd.get("last_seen", 0) < 30:
                            live.update(nd.get("agents", []))
                pruned = bus.registry.prune_stale(live)
                if pruned:
                    await self._log(f"Pruned {len(pruned)} stale TopicBus contract(s): {pruned}")
        except Exception as exc:
            logger.debug("[%s] Contract prune skipped: %s", self.name, exc)

    async def _run_plan(self, task: str) -> str:
        workers = self._discover_workers()
        await self._log(f"Workers available: {[w['name'] for w in workers]}")

        # ── Prune stale TopicBus contracts ────────────────────────────────
        # Remove contracts for agents that are no longer running so the
        # planner doesn't wire against dead topics.
        # IMPORTANT: include remote agents from live node heartbeats — they
        # are not in the local registry but their contracts are valid and
        # must NOT be pruned.
        await self._prune_stale_contracts()

        # ── Approved-plan execution: skip planning entirely ────────────────
        # Set when main is calling us back to execute a previously-approved
        # proposal. Route directly to the pipeline executor since approved
        # plans are by definition pipeline plans (they were generated in
        # plan_only mode through _run_pipeline).
        if self._approved_plan:
            return await self._run_pipeline(task, workers)

        # Detect pipeline vs one-shot
        is_pipeline = is_pipeline_request(task)
        if is_pipeline:
            await self._log("Pipeline request detected — spawning persistent agents...")
            return await self._run_pipeline(task, workers)

        # ── Dry-run guard for one-shot path ────────────────────────────────
        # The one-shot path can still produce persistent agents via
        # _ensure_agents (it spawns missing ones declared by _decompose).
        # If main asked for plan_only, we MUST honor it here too — otherwise
        # any task that doesn't trip the pipeline heuristic silently spawns,
        # bypassing approval. Force the pipeline path so we get the same
        # approval flow regardless of which heuristic branch was taken.
        if self._plan_only:
            await self._log(
                "plan_only=True on one-shot path — routing through pipeline planner for approval flow"
            )
            return await self._run_pipeline(task, workers)

        # ── 1. Check cache ─────────────────────────────────────────────────
        cache_key = task_hash(task)
        cached = self._load_cached_plan(cache_key, workers)
        if cached:
            await self._log(f"Cache hit — reusing plan ({len(cached)} steps)")
            plan = cached
        else:
            await self._log("No cache hit — generating plan with LLM...")
            plan = await self._decompose(task, workers)
            if not plan:
                await self._log("Decomposition failed — answering directly")
                return await self._llm_answer(task)

        # ── 2. Spawn any missing agents declared in the plan ───────────────
        plan = await self._ensure_agents(plan)

        # ── 3. Execute ─────────────────────────────────────────────────────
        await self._log(f"Executing {len(plan)} step(s)...")
        results = await self._execute(plan)

        # ── 4. Synthesize ──────────────────────────────────────────────────
        answer = await self._synthesize(task, plan, results)

        # ── 5. Cache successful plan ───────────────────────────────────────
        if not cached:
            self._save_plan_cache(cache_key, task, plan)
            await self._log("Plan cached for future reuse.")

        await self._log("Task complete.")
        if self._auto_terminate:
            asyncio.create_task(self._deferred_stop())

        return answer

    # ── Pipeline code validator ────────────────────────────────────────────

    def _validate_pipeline_code(self, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return validate_pipeline_code(plan, self.name)

    # ── Plan cache ─────────────────────────────────────────────────────────

    def _load_cached_plan(self, cache_key: str, workers: list[dict[str, Any]]) -> list[Any] | None:
        alive = {w["name"] for w in workers} | {"main", self.name}
        return select_cached_plan(self.recall(PLAN_CACHE_KEY) or {}, cache_key, alive, self.name)

    def _save_plan_cache(self, cache_key: str, task: str, plan: list[dict[str, Any]]) -> None:
        raw = self.recall(PLAN_CACHE_KEY) or {}
        self.persist(PLAN_CACHE_KEY, with_plan_cached(raw, cache_key, task, plan))

    # ── Worker discovery ───────────────────────────────────────────────────

    def _local_workers(self, manifest_map: dict[str, Any], seen: set[str]) -> list[dict[str, Any]]:
        """Workers running in this process, described for the planning prompt."""
        workers: list[dict[str, Any]] = []
        if not self._registry:
            return workers
        for actor in self._registry.all_actors():
            if actor.name in _SKIP_AGENTS or actor.name == self.name:
                continue
            if actor.name.startswith("planner-"):
                continue
            seen.add(actor.name)
            manifest = manifest_map.get(actor.name, {})
            workers.append(
                {
                    "name": actor.name,
                    "type": type(actor).__name__,
                    "node": None,
                    "remote": False,
                    "description": (
                        manifest.get("description")
                        or getattr(actor, "description", "")
                        or getattr(actor, "system_prompt", "")[:100]
                        or type(actor).__name__
                    ),
                    "capabilities": manifest.get("capabilities", []),
                    "input_schema": manifest.get("input_schema", {}),
                    "output_schema": manifest.get("output_schema", {}),
                    "publishes": manifest.get("publishes", []),
                    "observed_samples": manifest.get("observed_samples", {}),
                }
            )
        return workers

    def _remote_workers(
        self, main: Any, manifest_map: dict[str, Any], seen: set[str]
    ) -> list[dict[str, Any]]:
        """Workers on other nodes, taken from the heartbeats main has seen."""
        workers: list[dict[str, Any]] = []
        if main:
            for node_name, nd in main._known_nodes.items():
                if time.time() - nd.get("last_seen", 0) > 30:
                    continue  # node offline — skip
                for aname in nd.get("agents", []):
                    if aname in seen or aname in _SKIP_AGENTS:
                        continue
                    seen.add(aname)
                    manifest = manifest_map.get(aname, {})
                    workers.append(
                        {
                            "name": aname,
                            "type": "RemoteAgent",
                            "node": node_name,
                            "remote": True,
                            "description": manifest.get(
                                "description", f"Remote agent on {node_name}"
                            ),
                            "capabilities": manifest.get("capabilities", []),
                            "input_schema": manifest.get("input_schema", {}),
                            "output_schema": manifest.get("output_schema", {}),
                            "publishes": manifest.get("publishes", []),
                            "observed_samples": manifest.get("observed_samples", {}),
                        }
                    )
        return workers

    def _discover_workers(self) -> list[dict[str, Any]]:
        if not self._registry:
            return []
        # Pull full manifests from main's capability registry (includes schemas)
        main = find_main_actor(self._registry)
        manifest_map: dict[str, Any] = {}
        if main:
            for cap in main.list_capabilities():
                manifest_map[cap["name"]] = cap

        workers = []
        seen = set()

        # ── Local actors ──────────────────────────────────────────────────────
        workers += self._local_workers(manifest_map, seen)

        # ── Remote agents from live node heartbeats ───────────────────────────
        workers += self._remote_workers(main, manifest_map, seen)

        return workers

    # ── Decomposition ──────────────────────────────────────────────────────

    async def _topic_schema_context(self) -> str:
        """Live topic samples, so generated code uses real field names."""
        topic_schema_ctx = ""
        try:
            from ...core.topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus:
                sample_lines = []
                for contract in bus.registry.all_contracts():
                    samples = contract.observed_samples or {}
                    for topic, info in samples.items():
                        example = info.get("example", {})
                        fields = info.get("fields", {})
                        sample_lines.append(
                            f"  {topic} (by {contract.name}): fields={fields}  example={example}"
                        )
                if not sample_lines:
                    sample_lines = await self._sample_live_topics(bus)
                if sample_lines:
                    topic_schema_ctx = (
                        "\n\nLIVE TOPIC SCHEMAS (use EXACTLY these field names in generated code):\n"
                        + "\n".join(sample_lines)
                        + "\nCRITICAL: Use the exact field names from the samples above. "
                        "If a sample shows 'temp', use payload['temp'] — NOT payload['temperature'].\n"
                    )
        except Exception as exc:
            logger.debug("[%s] No live topic samples available: %s", self.name, exc)
        return topic_schema_ctx

    async def _decompose(self, task: str, workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """LLM breaks task into steps. Can declare missing agents with spawn configs."""
        if not self.llm:
            return []

        workers_desc = "\n".join(_fmt_worker(w) for w in workers)

        # ── Gather live topic samples for schema context ──────────────────
        topic_schema_ctx = await self._topic_schema_context()

        prompt = DECOMPOSE_PROMPT.format(
            workers_desc=workers_desc,
            topic_schema_ctx=topic_schema_ctx,
            task=task,
        )

        try:
            response, _usage = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system=self._now_context()
                + "\nYou are a JSON-only task planner. Output only valid JSON arrays, nothing else.",
                max_tokens=1500,
            )
            self._accrue_usage(_usage)
            plan = json.loads(extract_json_array(response))
            if isinstance(plan, list) and plan:
                return plan
        except Exception:
            logger.exception("[%s] Decomposition failed", self.name)
        return []

    # ── Direct answer (fallback when a plan is not the right shape) ────────

    async def _llm_answer(self, task: str) -> str:
        if not self.llm:
            return f"[No LLM available: {task}]"
        try:
            response, _usage = await self.llm.complete(
                messages=[{"role": "user", "content": task}],
                system="You are a helpful assistant.",
                max_tokens=2048,
            )
            self._accrue_usage(_usage)
        except Exception as e:
            return f"[LLM error: {e}]"
        else:
            return response

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _lifetime_watchdog(self) -> None:
        """Hard lifetime cap.

        A planner must never outlive its task. Whether it finishes normally,
        gets stuck waiting on an approval round-trip that never returns, or sets
        up a persistent pipeline, it self-removes after _max_lifetime_s so
        planners don't accumulate until an app restart.

        Spawned pipeline agents are independent actors (registered in main's
        spawn registry), so terminating the planner does NOT stop them.
        """
        try:
            await asyncio.sleep(self._max_lifetime_s)
        except asyncio.CancelledError:
            return
        if not self._terminated:
            mins = self._max_lifetime_s / 60
            await self._log(f"Max lifetime ({mins:.0f} min) reached — self-terminating.")
            await self._terminate()

    async def _release_from_registry(self) -> None:
        """Let go of everything holding this planner alive, Supervisor first.

        spawn() registers every child with the Supervisor, which keeps a strong
        reference and a name in its order. Unregistering alone would drop the
        planner from the message registry while the Supervisor still held the
        object, so _specs would grow by one planner per request until the app
        restarted. release() drops the reference and retires the spec, which
        also rules out a restart race. Mirrors the delete path main uses.
        """
        if self._registry:
            sup = getattr(self._registry, "_supervisor_ref", None)
            if sup is not None:
                try:
                    sup.release(self.name)
                except Exception as exc:
                    logger.debug("[%s] Supervisor release failed: %s", self.name, exc)

        if self._registry:
            try:
                await self._registry.unregister(self.actor_id)
            except Exception as exc:
                logger.debug("[%s] Unregister failed: %s", self.name, exc)

    async def _terminate(self) -> None:
        """Idempotent teardown: cancel watchdog + pending futures, unregister, stop."""
        if self._terminated:
            return
        self._terminated = True

        # Cancel the watchdog unless we are being invoked *from* it.
        if (
            self._lifetime_task
            and not self._lifetime_task.done()
            and self._lifetime_task is not asyncio.current_task()
        ):
            self._lifetime_task.cancel()

        # Fail any in-flight delegations so nothing awaits a dead planner.
        for fut in list(self._result_futures.values()):
            if not fut.done():
                fut.cancel()
        self._result_futures.clear()

        await self._log("Self-terminating.")

        # ── Release from the Supervisor FIRST ──────────────────────────────
        # spawn() auto-registers every child under the Supervisor, which pins a
        # strong reference (spec.actor) and keeps the name in _order. Without
        # releasing, unregister()+stop() only removes us from the message
        # registry — the Supervisor still holds the object, so it is never
        # garbage-collected and _specs grows one entry per planner until the app
        # restarts. release() drops the actor reference and marks the spec
        # retired (which also prevents any restart race). This mirrors main's
        # own delete path: release() → unregister() → stop().
        await self._release_from_registry()
        try:
            await self.stop()
        except Exception as exc:
            logger.debug("[%s] Stop failed: %s", self.name, exc)

    async def _deferred_stop(self, delay: float = 2.0) -> None:
        await asyncio.sleep(delay)
        await self._terminate()

    async def _log(self, msg: str) -> None:
        logger.info("[%s] %s", self.name, msg)
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": msg, "timestamp": time.time()},
        )


def _fmt_worker(w: dict[str, Any]) -> str:
    """One prompt line per worker, plus whatever schema it declares."""
    location = f" on {w['node']}" if w.get("remote") and w.get("node") else ""
    lines = [f"  - {w['name']} ({w['type']}{location}): {w['description']}"]
    if w.get("capabilities"):
        lines.append(f"    capabilities: {', '.join(w['capabilities'])}")
    if w.get("input_schema"):
        lines.append(f"    input_schema : {w['input_schema']}")
    if w.get("output_schema"):
        lines.append(f"    output_schema: {w['output_schema']}")
    if w.get("publishes"):
        lines.append(f"    publishes: {w['publishes']}")
    if w.get("observed_samples"):
        for topic, info in w["observed_samples"].items():
            fields = info.get("fields", {})
            example = info.get("example", {})
            lines.append(f"    topic '{topic}' payload fields: {fields}  example: {example}")
    return "\n".join(lines)
