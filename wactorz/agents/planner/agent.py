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
import hashlib
import json
import logging
import re
import time
from typing import Any

from ...core.actor import Actor, Message, MessageType
from ...core.mqtt import mqtt_client
from ..llm_agent import LLMProvider, accumulate_global_cost
from ..lookup import find_main_actor
from ..mixins.spawning import SpawnMixin, SpawnPlaceholder
from ..prompts.planner_prompts import (
    DECOMPOSE_PROMPT,
    HA_FEASIBILITY_PROMPT,
    PIPELINE_DESIGN_PROMPT,
    RULE_CONFLICT_PROMPT,
)
from .cache import PLAN_CACHE_KEY, select_cached_plan, with_plan_cached
from .detection import is_pipeline_request
from .parsing import extract_json_array, extract_json_object, task_hash
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


class PlannerAgent(Actor, SpawnMixin):
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
        approved_plan: dict | None = None,
        max_lifetime_s: float = DEFAULT_MAX_LIFETIME_S,
        **kwargs,
    ):
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
        self._spawn_results: dict[str, dict] = {}

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

    async def on_start(self):
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

    async def on_stop(self):
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
        except Exception:
            pass

    def _build_metrics(self) -> dict:
        m = super()._build_metrics()
        m["input_tokens"] = self.total_input_tokens
        m["output_tokens"] = self.total_output_tokens
        m["cost_usd"] = round(self.total_cost_usd, 6)
        return m

    def _accrue_usage(self, usage: dict) -> None:
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
                except Exception:
                    pass
        from ..llm_agent import current_time_context

        return current_time_context(user_tz)

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
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

    async def _report_plan(self, task: str):
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

    def _load_pipeline_rules(self) -> list[dict]:
        return self.recall("_pipeline_rules") or []

    def _save_pipeline_rule(self, rule: dict):
        rules = self._load_pipeline_rules()
        rules = [r for r in rules if r.get("rule_id") != rule["rule_id"]]
        rules.append(rule)
        self.persist("_pipeline_rules", rules)

    # ── Pipeline detection & dispatch ──────────────────────────────────────

    async def _run_plan(self, task: str) -> str:
        workers = self._discover_workers()
        await self._log(f"Workers available: {[w['name'] for w in workers]}")

        # ── Prune stale TopicBus contracts ────────────────────────────────
        # Remove contracts for agents that are no longer running so the
        # planner doesn't wire against dead topics.
        # IMPORTANT: include remote agents from live node heartbeats — they
        # are not in the local registry but their contracts are valid and
        # must NOT be pruned.
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
        except Exception:
            pass

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

    # ── Pipeline mode (persistent reactive agents) ─────────────────────────

    async def _run_pipeline(self, task: str, workers: list[dict]) -> str:
        """Builds and spawns persistent reactive agents for if/when/wherever rules.

        Two modes governed by constructor flags (set by main):
          - plan_only=True: build the plan, return it as a JSON string, do NOT spawn.
            Used for dry-run / approval flow. Main parses the JSON and shows a
            summary to the user.
          - approved_plan=<dict>: skip planning, execute the supplied plan directly.
            Used after the user approves a previously-generated plan.
          - default (neither set): plan AND execute in one go (original behavior,
            used by explicit-prefix paths like 'pipeline:' that bypass dry-run).
        """
        # ── Mode A: execute pre-approved plan, skip planning ───────────────
        if self._approved_plan:
            plan = self._approved_plan.get("plan") or self._approved_plan.get("agents") or []
            if not plan:
                return "Approved plan was empty — nothing to spawn."
            await self._log(f"Executing pre-approved plan ({len(plan)} agent(s))")
            # Mode A doesn't run topic resolution — pass through any note that
            # was carried in the approved envelope (set during plan_only build).
            note = self._approved_plan.get("resolution_note", "")
            return await self._execute_pipeline_plan(plan, task, resolution_note=note)

        # ── Build the plan (always — needed for both plan_only and full run) ──
        # Step 0: Topic resolution. Resolve vague references ("temperature",
        # "motion") to concrete MQTT topics or HA entities so the user can say
        # "react to temperature" without knowing the exact topic name.
        task, resolution_note = await self._resolve_data_references(task)
        if resolution_note:
            await self._log(f"Topic resolution: {resolution_note}")

        plan = await self._decompose_pipeline(task, workers)

        if not plan:
            await self._log("Pipeline decomposition failed — falling back to direct answer")
            return await self._llm_answer(task)

        if len(plan) == 1 and "_feasibility_error" in plan[0]:
            error = plan[0]["_feasibility_error"]
            await self._log(f"Pipeline not feasible: {error}")
            return f"Cannot set up this pipeline:\n\n{error}"

        # ── Advisory: check for duplicate / contradicting active rules ─────
        # Surfaced as extra info at approval time (or prepended to the summary
        # on the immediate-execute path). Never blocks — it's a heads-up.
        conflict_note = await self._check_rule_conflicts(task, plan)
        if conflict_note:
            await self._log(f"Rule-conflict advisory: {conflict_note}")

        # ── Mode B: plan_only — return the plan, don't spawn ───────────────
        if self._plan_only:
            await self._log(f"plan_only=True — returning plan ({len(plan)} agent(s)) for approval")
            # Serialize the plan back to JSON so main can store it intact.
            # Wrap in a dict with a marker so main can distinguish a plan
            # from a normal answer string.
            envelope = {
                "_plan_proposal": True,
                "task": task,
                "resolution_note": resolution_note or "",
                "plan": plan,
                "warnings": conflict_note,  # "" when nothing notable
            }
            return json.dumps(envelope)

        # ── Mode C: original behavior — plan AND execute ───────────────────
        await self._log(f"Pipeline plan: {len(plan)} agent(s)")
        summary = await self._execute_pipeline_plan(plan, task, resolution_note=resolution_note)
        if conflict_note:
            summary = f"⚠️ Heads up — {conflict_note}\n\n{summary}"
        return summary

    async def _execute_pipeline_plan(
        self, plan: list[dict], task: str, resolution_note: str = ""
    ) -> str:
        """Spawn each agent in the plan, register with main, build the final summary.
        The single execution path: a plan reaches it directly, and an approved
        dry-run reaches it too, so the two cannot diverge in what they run.

        resolution_note: optional preamble describing what topic/entity the planner
        resolved the user's vague reference to. Empty for pre-approved plans where
        the resolution step was skipped (note is carried in the envelope instead).
        """
        spawned: list[str] = []
        wired: list[str] = []
        rule_agents: list[str] = []

        for step in plan:
            name = step.get("name", "").strip()
            description = step.get("description", "")
            spawn_cfg = step.get("spawn_config")

            if not name:
                await self._log("Step missing name — skipping")
                continue

            if self._registry and self._registry.find_by_name(name):
                await self._log(f"'{name}' already running — skipping")
                wired.append(f"**{name}** (already active)")
                rule_agents.append(name)
                self._spawn_results[name] = {"ok": True, "status": "already_running"}
                continue

            if not spawn_cfg:
                await self._log(f"Step '{name}' has no spawn_config — skipping")
                self._spawn_results[name] = {
                    "ok": False,
                    "status": "no_config",
                    "error": "missing spawn_config",
                }
                continue

            spawn_cfg = dict(spawn_cfg)
            spawn_cfg["name"] = name

            spawn_type = spawn_cfg.get("type", "dynamic")
            await self._log(f"Spawning '{name}' (type={spawn_type})...")
            try:
                actor = await self._spawn_agent(spawn_cfg)
            except Exception as e:
                await self._log(f"Spawn failed for '{name}': {e}")
                wired.append(f"**{name}** — spawn failed: {e}")
                self._spawn_results[name] = {"ok": False, "status": "spawn_failed", "error": str(e)}
                continue

            if actor:
                self._spawned_by_planner.append(name)
                spawned.append(name)
                rule_agents.append(name)
                self._spawn_results[name] = {"ok": True, "status": "spawned"}

                # Register in main's spawn registry for auto-restore on restart
                if self._registry:
                    main = find_main_actor(self._registry)
                    if main:
                        registry_cfg = dict(spawn_cfg)
                        registry_cfg["name"] = name
                        registry_cfg["_rule"] = True
                        registry_cfg["_rule_task"] = task[:200]
                        main._save_to_spawn_registry(registry_cfg)

                topics = spawn_cfg.get("mqtt_topics", [])
                label = f"**{name}** — {description}"
                if topics:
                    label += "\n  listens: " + ", ".join(topics)
                wired.append(label)
                await asyncio.sleep(0.3)
            else:
                wired.append(f"**{name}** — failed to spawn")
                self._spawn_results[name] = {"ok": False, "status": "spawn_returned_none"}

        # ── Bootstrap current HA state for freshly-spawned agents ────────────
        # Agents wait for MQTT changes, but if the entity is already in the
        # target state before they spawned, they'd never receive a trigger.
        # Ask home-assistant-agent to re-publish the current state over MQTT
        # so agents can evaluate it immediately.
        if spawned:
            asyncio.create_task(self._bootstrap_ha_entity_states(task, plan))

        # Persist this rule into main's pipeline rules registry
        if rule_agents:
            rule_id = hashlib.md5(task.encode()).hexdigest()[:8]
            rule = {
                "rule_id": rule_id,
                "task": task,
                "agents": rule_agents,
                "created_at": time.time(),
            }
            # Save into main so it survives planner self-termination
            if self._registry:
                main = find_main_actor(self._registry)
                if main:
                    main.save_pipeline_rule(rule)
                    logger.info(f"[{self.name}] Pipeline rule {rule_id} saved to main")

        self._auto_terminate = False

        if not wired:
            return "Pipeline plan generated but no agents could be spawned. Check logs."

        out = ["Pipeline active! Here's what I set up:\n"]
        if resolution_note:
            out.insert(0, f"📡 **Data source resolved:** {resolution_note}\n")
        out += [f"{i + 1}. {w}" for i, w in enumerate(wired)]
        out.append("\nThese agents run continuously and react to events automatically.")
        out.append("Use `/rules` to see all active pipeline rules.")
        if spawned:
            out.append(f"\nSpawned: {', '.join(spawned)} — will auto-restore on restart.")
        return "\n".join(out)

    async def _check_rule_conflicts(self, task: str, plan: list[dict]) -> str:
        """Compare the proposed pipeline against already-active rules and flag
        duplicates or contradictions.

        Returns a short human-readable advisory (shown at approval time, or
        prepended to the immediate-execute summary), or "" when there's nothing
        notable, no LLM, or no existing rules. This is ADVISORY ONLY — it never
        blocks the plan.

        Two things are flagged:
          - DUPLICATE      — same trigger AND same action as an existing rule.
          - CONTRADICTION  — same/overlapping trigger, OPPOSING action
                             (e.g. "over 25° turn AC off" vs "over 25° turn AC on").
        """
        if not self.llm:
            return ""

        # Existing rules live on main (the authoritative store).
        existing: list[dict] = []
        if self._registry:
            main = find_main_actor(self._registry)
            if main:
                try:
                    existing = list(main.get_pipeline_rules().values())
                except Exception:
                    existing = []
        if not existing:
            return ""

        existing_lines = []
        by_id: dict[str, dict] = {}
        for r in existing[:30]:  # cap prompt size
            rid = r.get("rule_id", "?")
            rtask = (r.get("task") or "").strip().replace("\n", " ")[:160]
            if rtask:
                by_id[rid] = r
                existing_lines.append(f"- [{rid}] {rtask}")
        if not existing_lines:
            return ""

        prompt = (
            RULE_CONFLICT_PROMPT.format(task=task) + "\n".join(existing_lines) + "\n\n"
            "Respond with ONLY a JSON object:\n"
            '{"conflict": <true|false>, "items": [{"rule_id": "<id>", '
            '"kind": "duplicate|contradiction", "reason": "<one short sentence>"}]}\n'
            "Be conservative — if there is no CLEAR duplicate or contradiction, "
            'return {"conflict": false, "items": []}. Do not invent conflicts.'
        )
        try:
            response, _usage = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You are a precise rule-conflict checker. Output only JSON.",
                max_tokens=400,
            )
            self._accrue_usage(_usage)
            data = json.loads(extract_json_object(response))
        except Exception as e:
            logger.debug(f"[{self.name}] Rule-conflict check failed: {e}")
            return ""

        if not isinstance(data, dict) or not data.get("conflict"):
            return ""
        items = data.get("items") or []
        if not items:
            return ""

        lines = []
        for it in items[:5]:
            if not isinstance(it, dict):
                continue
            kind = (it.get("kind") or "overlap").lower()
            rid = it.get("rule_id", "?")
            reason = (it.get("reason") or "").strip()
            rtask = (by_id.get(rid, {}).get("task") or "").strip().replace("\n", " ")[:120]
            label = "Duplicate of" if kind.startswith("dup") else "May contradict"
            bit = f"{label} rule [{rid}]"
            if rtask:
                bit += f' ("{rtask}")'
            if reason:
                bit += f" — {reason}"
            lines.append(bit)
        if not lines:
            return ""
        return "This pipeline may overlap with existing rules:\n" + "\n".join(
            f"  • {ln}" for ln in lines
        )

    async def _resolve_data_references(self, task: str) -> tuple[str, str]:
        """Resolve vague data references in a task to concrete MQTT topics or HA entities.

        Examples:
          "log when temperature > 22"
            → finds sensors/test/temperature in TopicRegistry
            → enriches: "log when temperature > 22 [subscribe to: sensors/test/temperature]"

          "alert when motion detected"
            → finds rpi-kitchen/camera/detections in TopicRegistry
            → enriches: "alert when motion detected [subscribe to: rpi-kitchen/camera/detections]"

          "log when temperature > 22"  (no registered topics)
            → falls back to HA entity search
            → finds sensor.living_room_temperature
            → enriches: "log when temperature > 22 [HA entity: sensor.living_room_temperature]"

          "log when temperature > 22"  (ambiguous — multiple sources)
            → returns the task unchanged + a note listing candidates
            → planner LLM receives the candidates and picks the best one

        Returns: (enriched_task, resolution_note)
          enriched_task   — task with concrete topic/entity appended as context
          resolution_note — human-readable summary of what was found (shown to user)
        """
        # ── Data concept keywords → search terms ──────────────────────────
        # Maps natural language concepts to TopicRegistry search keywords
        CONCEPT_MAP = {
            r"\btemp(erature)?\b": ["temperature", "temp", "thermal"],
            r"\bhumid(ity)?\b": ["humidity", "humid"],
            r"\bmotion\b": ["motion", "pir", "presence", "detect"],
            r"\bpresence\b": ["presence", "motion", "occupancy"],
            r"\benergy\b": ["energy", "power", "kwh", "watt"],
            r"\bcpu\b": ["cpu", "processor"],
            r"\bmemory\b": ["memory", "ram"],
            r"\bco2\b": ["co2", "carbon"],
            r"\bair quality\b": ["air", "quality", "voc", "pm25"],
            r"\blight level\b": ["light", "lux", "illumin"],
            r"\bnoise\b": ["noise", "sound", "db"],
            r"\bdetect(ion)?\b": ["detect", "yolo", "camera", "vision"],
            r"\bdoor\b": ["door", "entry", "contact"],
            r"\bwindow\b": ["window", "contact"],
            r"\bwater\b": ["water", "flood", "leak"],
            r"\bgas\b": ["gas", "methane", "smoke"],
            r"\bvoltage\b": ["voltage", "power", "electric"],
        }

        task_lower = task.lower()

        # Find which concepts are mentioned in the task
        matched_concepts = []
        for pattern, keywords in CONCEPT_MAP.items():
            if re.search(pattern, task_lower):
                matched_concepts.extend(keywords)

        if not matched_concepts:
            return task, ""  # No vague data references found

        # ── Search TopicRegistry first ─────────────────────────────────────
        try:
            from ...core.topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus:
                # Deduplicate and search
                seen = set()
                candidates = []
                for kw in matched_concepts:
                    if kw in seen:
                        continue
                    seen.add(kw)
                    for contract in bus.registry.find_by_capability(kw):
                        for topic in contract.publishes:
                            if not any(c["topic"] == topic for c in candidates):
                                candidates.append(
                                    {
                                        "topic": topic,
                                        "agent": contract.name,
                                        "node": contract.node,
                                        "schema": contract.produces_schema,
                                        "source": "topic_registry",
                                    }
                                )

                if len(candidates) == 1:
                    # Unambiguous — auto-resolve
                    c = candidates[0]
                    node_str = f" on {c['node']}" if c.get("node") else ""
                    enriched = (
                        f"{task} "
                        f"[DATA SOURCE: subscribe to MQTT topic '{c['topic']}' "
                        f"published by {c['agent']}{node_str}. "
                        f"Use agent.subscribe('{c['topic']}', callback) in setup().]"
                    )
                    note = (
                        f"Found `{c['topic']}` from **{c['agent']}**{node_str} "
                        f"— using this as the data source."
                    )
                    return enriched, note

                if len(candidates) > 1:
                    # Multiple matches — give all to LLM, let it pick best
                    sources = ", ".join(f"'{c['topic']}' ({c['agent']})" for c in candidates[:5])
                    enriched = (
                        f"{task} "
                        f"[MULTIPLE DATA SOURCES FOUND: {sources}. "
                        f"Pick the most relevant topic based on the user's intent. "
                        f"Use agent.subscribe(chosen_topic, callback) in setup().]"
                    )
                    note = (
                        f"Found {len(candidates)} matching topics: "
                        + ", ".join(f"`{c['topic']}`" for c in candidates[:3])
                        + (" and more" if len(candidates) > 3 else "")
                        + " — planner will pick the most relevant."
                    )
                    return enriched, note

        except Exception as e:
            logger.debug(f"[{self.name}] TopicRegistry search failed: {e}")

        # ── Fallback: search HA entities ───────────────────────────────────
        # No registered agent topics found — check if HA has relevant sensors
        try:
            if self._registry:
                ha_agent = self._registry.find_by_name("home-assistant-agent")
                if ha_agent:
                    import uuid as _uuid

                    task_id = f"resolve_{_uuid.uuid4().hex[:6]}"
                    future = asyncio.get_running_loop().create_future()
                    self._result_futures[task_id] = future
                    await self.send(
                        ha_agent.actor_id,
                        MessageType.TASK,
                        {
                            "text": "list entities",
                            "_task_id": task_id,
                            "task": task_id,
                        },
                    )
                    try:
                        result = await asyncio.wait_for(future, timeout=8.0)
                        # home-assistant-agent returns {"entities": [...]} — a flat list
                        # of entity dicts with entity_id, name, state, etc.
                        # NOT a nested devices→entities structure.
                        entities_raw = (
                            result.get("entities", [])
                            or result.get("result", [])
                            or result.get("devices", [])  # legacy fallback
                        )
                        if isinstance(entities_raw, str):
                            entities_raw = []
                    except (asyncio.TimeoutError, Exception):
                        entities_raw = []
                    finally:
                        self._result_futures.pop(task_id, None)

                    # Search entity list for relevant matches
                    ha_candidates = []
                    for entity in entities_raw:
                        if not isinstance(entity, dict):
                            continue
                        # Handle both flat entity format and nested device format
                        if "entity_id" in entity:
                            # Flat format: {"entity_id": "sensor.temp", "name": "..."}
                            eid = entity.get("entity_id", "")
                            ename = entity.get("friendly_name", "") or entity.get("name", "")
                            state = entity.get("state", "")
                            combined = (eid + " " + ename).lower()
                            if any(kw in combined for kw in matched_concepts):
                                ha_candidates.append(
                                    {
                                        "entity_id": eid,
                                        "name": ename,
                                        "state": state,
                                        "source": "home_assistant",
                                    }
                                )
                        elif "entities" in entity:
                            # Nested device format (legacy): {"entities": [...]}
                            for sub in entity.get("entities", []):
                                eid = sub.get("entity_id", "")
                                ename = sub.get("friendly_name", "") or sub.get("name", "")
                                state = sub.get("state", "")
                                combined = (eid + " " + ename).lower()
                                if any(kw in combined for kw in matched_concepts):
                                    ha_candidates.append(
                                        {
                                            "entity_id": eid,
                                            "name": ename,
                                            "state": state,
                                            "source": "home_assistant",
                                        }
                                    )

                    if len(ha_candidates) == 1:
                        c = ha_candidates[0]
                        enriched = (
                            f"{task} "
                            f"[DATA SOURCE: Home Assistant entity '{c['entity_id']}' "
                            f"(name: {c['name']}, current state: {c['state']}). "
                            f"Subscribe to homeassistant/state_changes/# and filter "
                            f"by payload.get('entity_id') == '{c['entity_id']}'. "
                            f"The value is in payload.get('new_state', {{}}).get('state').]"
                        )
                        note = (
                            f"No MQTT topic found — using HA entity "
                            f"**{c['name']}** (`{c['entity_id']}`, currently: {c['state']})."
                        )
                        return enriched, note

                    if len(ha_candidates) > 1:
                        sources = ", ".join(
                            f"'{c['entity_id']}' ({c['name']})" for c in ha_candidates[:4]
                        )
                        enriched = (
                            f"{task} "
                            f"[MULTIPLE HA ENTITIES FOUND: {sources}. "
                            f"Pick the most relevant. Subscribe to homeassistant/state_changes/# "
                            f"and filter by entity_id in the payload.]"
                        )
                        note = (
                            f"No MQTT topic found — found {len(ha_candidates)} HA entities: "
                            + ", ".join(f"`{c['entity_id']}`" for c in ha_candidates[:3])
                            + (" and more" if len(ha_candidates) > 3 else "")
                            + " — planner will pick the most relevant."
                        )
                        return enriched, note

        except Exception as e:
            logger.debug(f"[{self.name}] HA entity search failed: {e}")

        # ── Nothing found — return task unchanged with a note ──────────────
        concepts_str = ", ".join(set(matched_concepts[:4]))
        enriched = (
            f"{task} "
            f"[NOTE: No registered MQTT topics or HA entities found matching: {concepts_str}. "
            f"If the user has a sensor agent running, it may not have published yet. "
            f"Ask the user to specify the exact MQTT topic or HA entity ID, "
            f"or check agent.topics() for available data streams.]"
        )
        note = (
            f"No data source found for: {concepts_str}. "
            f"You may need to specify the exact topic or entity."
        )
        return enriched, note

    async def _sample_live_topics(self, bus) -> list[str]:
        """Peek at one live MQTT message from each registered publish topic.
        Returns formatted lines with actual field names and an example value.

        This is the fallback when observed_samples haven't been captured yet
        (e.g. the producer started before the schema-capture code was deployed).

        Uses a single MQTT connection with a short per-topic timeout so it
        doesn't block planning. Topics that don't publish within the window
        are silently skipped.
        """
        try:
            import aiomqtt  # noqa: F401
        except ImportError:
            return []

        sample_lines = []
        topics_to_sample: list[tuple[str, str]] = []  # (topic, agent_name)

        for contract in bus.registry.all_contracts():
            for topic in (contract.publishes or [])[:5]:
                if not any(t == topic for t, _ in topics_to_sample):
                    topics_to_sample.append((topic, contract.name))
            if len(topics_to_sample) >= 10:
                break

        if not topics_to_sample:
            return []

        broker = getattr(self, "_mqtt_broker", "localhost")
        port = getattr(self, "_mqtt_port", 1883)

        # Subscribe to ALL topics on one connection, collect first message per topic
        # with a global timeout so we never hang.
        received: dict[str, dict] = {}  # topic → payload

        async def _collect():
            try:
                async with mqtt_client(broker, port) as client:
                    for topic, _ in topics_to_sample:
                        await client.subscribe(topic)
                    async for msg in client.messages:
                        t = str(msg.topic)
                        if t not in received:
                            try:
                                payload = json.loads(msg.payload.decode())
                            except Exception:
                                payload = msg.payload.decode()
                            if isinstance(payload, dict):
                                received[t] = payload
                        # Stop once we have a sample for every topic
                        if len(received) >= len(topics_to_sample):
                            return
            except Exception as e:
                logger.debug(f"[{self.name}] _sample_live_topics connection error: {e}")

        # Wait at most N seconds total (not per-topic) — covers the common case
        # where producers publish every few seconds.  Stale topics just get skipped.
        max_wait = min(15.0, 5.0 + 2.0 * len(topics_to_sample))
        try:
            await asyncio.wait_for(_collect(), timeout=max_wait)
        except asyncio.TimeoutError:
            pass  # we'll use whatever we collected so far

        # Build sample lines and store back into contracts
        topic_to_agent = dict(topics_to_sample)
        for topic, payload in received.items():
            agent_name = topic_to_agent.get(topic, "?")
            fields = {k: type(v).__name__ for k, v in payload.items() if not k.startswith("_")}
            # Persist into contract for future calls (no repeated sampling)
            for contract in bus.registry.all_contracts():
                if topic in (contract.publishes or []):
                    contract.update_observed(topic, payload)
                    break
            sample_lines.append(
                f"  Topic: {topic}  (published by {agent_name})\n"
                f"    Fields: {fields}\n"
                f"    Example payload: {payload}"
            )

        if sample_lines:
            logger.info(
                f"[{self.name}] Sampled {len(sample_lines)} live topic(s) for schema introspection"
            )
        return sample_lines

    async def _decompose_pipeline(self, task: str, workers: list[dict]) -> list[dict]:
        """Decomposes a reactive pipeline request into persistent agent spawn configs.

        Flow:
          1. Query HomeAssistantAgent for live entities (delegates — no duplication)
          2. Feasibility check — surface clear error if required HA entities are missing
          3. LLM produces spawn configs with real entity IDs and correct MQTT wiring
        """
        if not self.llm:
            return []

        # ── 1. Get HA entities via HomeAssistantAgent ──────────────────────
        ha_entities_text = ""
        ha_available = False

        try:
            if self._registry and self._registry.find_by_name("home-assistant-agent"):
                result = await self._delegate("home-assistant-agent", "list_entities")
                if result and not result.get("error"):
                    entities_list = result.get("entities", [])
                    if entities_list:
                        lines = []
                        # NB: do NOT truncate. Truncating silently dropped entities
                        # past index 200 (e.g. WiZ lights coming after dozens of
                        # sensor.* entries), causing feasibility checks to falsely
                        # claim "no light entity available" when the user's lamp
                        # was sitting in slots 200-305. If the entity list ever
                        # grows large enough to be a context-budget problem
                        # (4000+ entities), filter intelligently here — never
                        # blind-truncate.
                        for e in entities_list:
                            eid = e.get("entity_id", "")
                            ename = e.get("name", "")
                            plat = e.get("platform", "")
                            if eid:
                                parts = [eid]
                                if ename and ename != eid:
                                    parts.append(f"name={ename}")
                                if plat:
                                    parts.append(f"platform={plat}")
                                lines.append("  " + "  ".join(parts))
                        ha_entities_text = "\n".join(lines)
                        ha_available = True
                        logger.info(
                            f"[{self.name}] Got {len(entities_list)} HA entities via home-assistant-agent "
                            f"(formatted {len(lines)} lines for prompt)"
                        )
        except Exception as e:
            logger.warning(f"[{self.name}] Could not query home-assistant-agent: {e}")

        # Fallback: fetch directly if HA agent is unavailable
        if not ha_available:
            try:
                from ...config import CONFIG
                from ...core.integrations.home_assistant.ha_helper import (
                    fetch_devices_entities_with_location,
                )

                ha_url = (CONFIG.ha_url or "").rstrip("/")
                ha_token = (CONFIG.ha_token or "").strip()
                if ha_url and ha_token:
                    devices = await fetch_devices_entities_with_location(
                        ha_url, ha_token, include_states=True
                    )
                    lines = []
                    # Same rationale as above — don't truncate. Iterate ALL devices.
                    for device in devices:
                        area = device.get("area", "")
                        for entity in device.get("entities", []):
                            eid = entity.get("entity_id", "")
                            ename = entity.get("friendly_name") or entity.get("name", "")
                            state = entity.get("state", "")
                            if eid:
                                parts = [eid]
                                if ename:
                                    parts.append(f"name={ename}")
                                if area:
                                    parts.append(f"area={area}")
                                if state:
                                    parts.append(f"state={state}")
                                lines.append("  " + "  ".join(parts))
                    ha_entities_text = "\n".join(lines)
                    ha_available = bool(lines)
                    logger.info(f"[{self.name}] Direct HA fetch: {len(lines)} entities")
            except Exception as e:
                logger.warning(f"[{self.name}] Direct HA fetch failed: {e}")

        ha_section = (
            ha_entities_text or "  (HA not reachable — use entity IDs provided by the user)"
        )

        # ── Resolve real camera stream URLs via home-assistant-agent ───────
        # Mirrors the entity-list delegation above: ground PATTERN 3 (camera
        # detection pipelines) in real stream URLs instead of letting the LLM
        # invent /dev/video0 or guess proxy paths.
        camera_stream_urls: dict[str, str] = {}
        camera_snapshot_urls: dict[str, str] = {}
        try:
            camera_entity_ids = []
            camera_lines = []
            for line in ha_entities_text.splitlines():
                token = line.strip().split(" ", 1)[0]
                if token.startswith("camera."):
                    camera_lines.append(line.strip())
                    if token not in camera_entity_ids:
                        camera_entity_ids.append(token)

            if camera_entity_ids:
                task_words = {w for w in re.findall(r"[a-z0-9]+", task.lower()) if len(w) >= 3}
                candidates = [
                    eid for eid in camera_entity_ids if any(w in eid.lower() for w in task_words)
                ]
                if not candidates and any(
                    kw in task.lower() for kw in ("camera", "webcam", "stream")
                ):
                    candidates = camera_entity_ids[:5]

                logger.debug(f"[{self.name}] Camera candidates for '{task[:60]}': {candidates}")

                for eid in candidates:
                    result = await self._delegate_with_payload(
                        "home-assistant-agent",
                        {"operation": "get_camera_stream_url", "camera_entity_id": eid},
                        timeout=20.0,
                    )
                    logger.debug(f"[{self.name}] get_camera_stream_url({eid}) -> {result}")
                    if not result or result.get("error"):
                        continue
                    streams = (result.get("data") or {}).get("streams", {})
                    url = (
                        streams.get("camera_source")
                        or streams.get("mjpeg_proxy")
                        or streams.get("hls")
                    )
                    if url:
                        camera_stream_urls[eid] = url

                    snap_result = await self._delegate_with_payload(
                        "home-assistant-agent",
                        {"operation": "get_camera_snapshot_url", "camera_entity_id": eid},
                        timeout=20.0,
                    )
                    logger.debug(f"[{self.name}] get_camera_snapshot_url({eid}) -> {snap_result}")
                    if snap_result and not snap_result.get("error"):
                        snap_url = (snap_result.get("data") or {}).get("snapshot_url")
                        if snap_url:
                            camera_snapshot_urls[eid] = snap_url

                if camera_stream_urls:
                    logger.debug(
                        f"[{self.name}] Resolved {len(camera_stream_urls)} camera stream URL(s)"
                    )
                if camera_snapshot_urls:
                    logger.debug(
                        f"[{self.name}] Resolved {len(camera_snapshot_urls)} camera snapshot URL(s)"
                    )
        except Exception as e:
            logger.warning(f"[{self.name}] Could not resolve camera stream/snapshot URLs: {e}")

        if camera_stream_urls:
            cam_lines = [
                "CAMERA STREAM URLS (use these directly in code — do not invent /dev/video0 or proxy paths):"
            ]
            for eid, url in camera_stream_urls.items():
                cam_lines.append(f"  {eid}: {url}")
            camera_section = "\n".join(cam_lines)
        else:
            camera_section = (
                "CAMERA STREAM URLS: none resolved.\n"
                "If the user references a camera, use the matching camera.* entity_id from "
                "HOME ASSISTANT ENTITIES above and note in the description that the stream URL "
                "could not be resolved (Home Assistant may be unreachable or the camera unsupported)."
            )

        if camera_snapshot_urls:
            snap_lines = [
                "CAMERA SNAPSHOT URLS (for one-shot still-image capture — see PATTERN 7):",
            ]
            for eid, url in camera_snapshot_urls.items():
                snap_lines.append(f"  {eid}: {url}")
            snap_lines.append(
                "  Fetching requires header Authorization: Bearer {os.environ['HA_TOKEN']} — "
                "NEVER hardcode the token value, read it from the environment at runtime."
            )
            camera_snapshot_section = "\n".join(snap_lines)
        else:
            camera_snapshot_section = (
                "CAMERA SNAPSHOT URLS: none resolved.\n"
                "If the user wants a one-shot snapshot, use the matching camera.* entity_id from "
                "HOME ASSISTANT ENTITIES above and note in the description that the snapshot URL "
                "could not be resolved (Home Assistant may be unreachable or the camera unsupported)."
            )

        # ── Fetch TopicBus context (live data flows + wiring opportunities) ─
        topic_bus_section = ""
        topic_samples_section = ""
        try:
            from ...core.topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus and bus.registry.all_contracts():
                topic_bus_section = bus.to_planner_context()
                logger.info(
                    f"[{self.name}] TopicBus: {len(bus.registry.all_contracts())} contracts"
                )

                # ── Sample live payloads from registered topics ────────────
                # Captures ACTUAL field names so the LLM uses "temp" not "temperature"
                sample_lines = []
                for contract in bus.registry.all_contracts():
                    samples = contract.observed_samples or {}
                    if samples:
                        for topic, info in samples.items():
                            example = info.get("example", {})
                            fields = info.get("fields", {})
                            sample_lines.append(
                                f"  Topic: {topic}  (published by {contract.name})\n"
                                f"    Fields: {fields}\n"
                                f"    Example payload: {example}"
                            )

                # If no observed_samples yet, try to peek at one live message
                # from each published topic via MQTT (fast — 3s timeout each)
                if not sample_lines:
                    sample_lines = await self._sample_live_topics(bus)

                if sample_lines:
                    topic_samples_section = (
                        "LIVE TOPIC SAMPLES (actual payloads — use THESE field names in code):\n"
                        + "\n".join(sample_lines)
                    )

            else:
                topic_bus_section = (
                    "No topic contracts registered yet.\n"
                    "Agents can declare contracts via agent.declare_contract() in setup().\n"
                    "Once declared, the planner can wire agents automatically by topic compatibility."
                )
        except Exception as e:
            topic_bus_section = f"TopicBus unavailable: {e}"

        # ── Fetch stored notification URLs from main ──────────────────────
        notification_urls: dict = {}
        if self._registry:
            main = find_main_actor(self._registry)
            if main:
                notification_urls = main.get_notification_urls()

        # Also extract any URL directly mentioned in the task

        _url_match = re.search(
            r"https?://(?:discord\.com/api/webhooks|hooks\.slack\.com|api\.telegram\.org)/\S+", task
        )
        if _url_match:
            url = _url_match.group(0).rstrip(".,;!)'\"")
            if "discord" in url:
                notification_urls["discord"] = url
            elif "slack" in url:
                notification_urls["slack"] = url
            elif "telegram" in url:
                notification_urls["telegram"] = url

        notif_section = ""
        if notification_urls:
            lines = ["NOTIFICATION URLS (use these directly in code — do not use placeholders):"]
            for svc, url in notification_urls.items():
                lines.append(f"  {svc}: {url}")
            notif_section = "\n".join(lines)
        else:
            notif_section = (
                "NOTIFICATION URLS: none stored.\n"
                "If the user wants Discord/Slack/Telegram notifications and no URL is available,\n"
                "use a placeholder 'WEBHOOK_URL_REQUIRED' and set description to explain the user must run:\n"
                "  /webhook discord <url>"
            )
        # Skip feasibility ONLY when the requested action is clearly NOT an HA
        # service call. Original list included "message" and "notify" which
        # caused requests like "when X happens log a warning message" to bypass
        # the feasibility check entirely — sometimes useful, but it also masked
        # real HA-target bugs (the entity-truncation bug above wasn't visible
        # because most "log/notify" requests appeared to work despite a broken
        # entity list).
        #
        # Keep it tight: skip only for camera/vision pipelines (need cv2,
        # don't touch HA), explicit external-webhook integrations
        # (Discord/Slack/Telegram URLs — also don't touch HA), and pure-
        # observability tasks where no HA service call is implied.
        _non_ha_kw = (
            "camera",
            "webcam",
            "laptop camera",
            "yolo",
            "cv2",
            "opencv",
            "discord",
            "telegram",
            "slack",
            "webhook",
        )
        # HA service-call verbs — if the task contains one of these, feasibility
        # MUST run, because the LLM will try to emit an ha_actuator and we need
        # to confirm a real entity exists.
        _ha_action_verbs = (
            "turn on",
            "turn off",
            "open",
            "close",
            "lock",
            "unlock",
            "set temperature",
            "set brightness",
            "set color",
            "play",
            "pause",
            "start",
            "stop",
            "activate",
            "trigger ",
            "switch on",
            "switch off",
        )
        task_lower = task.lower()
        has_ha_verb = any(v in task_lower for v in _ha_action_verbs)
        has_skip_kw = any(kw in task_lower for kw in _non_ha_kw)
        # Skip feasibility if non-HA keyword present AND no HA verb is present.
        # If both present (e.g. "send Discord AND turn off the lamp"), feasibility
        # still runs to validate the lamp.
        _skip_feasibility = has_skip_kw and not has_ha_verb

        if ha_available and ha_entities_text and not _skip_feasibility:
            feas_prompt = HA_FEASIBILITY_PROMPT.format(task=task, ha_section=ha_section)
            try:
                feas_resp, _usage = await self.llm.complete(
                    messages=[{"role": "user", "content": feas_prompt}],
                    system=self._now_context() + "\nOutput only valid JSON. No markdown.",
                    max_tokens=400,
                )
                self._accrue_usage(_usage)
                clean = feas_resp.strip()
                for fence in ("```json", "```"):
                    clean = clean.removeprefix(fence)
                    clean = clean.removesuffix("```")
                clean = clean.strip()
                feas = json.loads(clean)
                if not feas.get("feasible", True):
                    reason = feas.get(
                        "reason", "Cannot fulfill request with available HA entities."
                    )
                    logger.warning(f"[{self.name}] Feasibility failed: {reason}")
                    return [{"_feasibility_error": reason}]
                logger.info(
                    f"[{self.name}] Feasibility OK — relevant: {feas.get('relevant_entities', [])}"
                )
            except Exception as e:
                logger.warning(f"[{self.name}] Feasibility check error (continuing): {e}")

        # ── 3. Decompose into spawn configs ────────────────────────────────

        # Build the prompt as a list of parts to avoid f-string escape issues
        prompt_parts = [
            PIPELINE_DESIGN_PROMPT,
            topic_bus_section,
            "",
            *(  # Include live topic samples if available
                [
                    "═══ LIVE TOPIC SAMPLES (use EXACTLY these field names in code!) ═══",
                    topic_samples_section,
                    "",
                    "CRITICAL: When subscribing to a topic listed above, use the EXACT field names",
                    "from the sample payload. For example if the sample shows {'temp': 30.5},",
                    "use payload['temp'] — NOT payload['temperature']. The field names in the",
                    "samples are authoritative.",
                    "",
                ]
                if topic_samples_section
                else []
            ),
            "═══ HOME ASSISTANT ENTITIES ═══",
            ha_section,
            "",
            "═══ NOTIFICATION URLS ═══",
            notif_section,
            "",
            "═══ CAMERA STREAM URLS ═══",
            camera_section,
            "",
            "═══ CAMERA SNAPSHOT URLS ═══",
            camera_snapshot_section,
            "",
            "═══ OUTPUT FORMAT ═══",
            "JSON array. Each element:",
            '{"name": "<unique-kebab-name>", "description": "<one sentence>", "spawn_config": {<full spawn_config>}}',
            "",
            "═══ USER REQUEST ═══",
            task,
        ]
        prompt = "\n".join(prompt_parts)

        try:
            response, _usage = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system=self._now_context()
                + "\nYou are a JSON-only pipeline architect. Output only a valid JSON array. No markdown, no explanation.",
                max_tokens=4000,
            )
            self._accrue_usage(_usage)
            plan = json.loads(extract_json_array(response))
            if isinstance(plan, list):
                # Validate generated code — catch common LLM mistakes
                plan = self._validate_pipeline_code(plan)
                logger.info(f"[{self.name}] Pipeline plan: {len(plan)} step(s)")
                for i, step in enumerate(plan):
                    sc = step.get("spawn_config", {})
                    logger.info(
                        f"[{self.name}]   step {i + 1}: name={step.get('name')}  "
                        f"type={sc.get('type')}  topics={sc.get('mqtt_topics', [])}"
                    )
                return plan
        except Exception as e:
            logger.error(f"[{self.name}] Pipeline decomposition error: {e}")
        return []

    # ── Pipeline code validator ────────────────────────────────────────────

    def _validate_pipeline_code(self, plan: list[dict]) -> list[dict]:
        return validate_pipeline_code(plan, self.name)

    # ── Plan cache ─────────────────────────────────────────────────────────

    def _load_cached_plan(self, cache_key: str, workers: list[dict]) -> list | None:
        alive = {w["name"] for w in workers} | {"main", self.name}
        return select_cached_plan(self.recall(PLAN_CACHE_KEY) or {}, cache_key, alive, self.name)

    def _save_plan_cache(self, cache_key: str, task: str, plan: list) -> None:
        raw = self.recall(PLAN_CACHE_KEY) or {}
        self.persist(PLAN_CACHE_KEY, with_plan_cached(raw, cache_key, task, plan))

    # ── Worker discovery ───────────────────────────────────────────────────

    def _discover_workers(self) -> list[dict]:
        if not self._registry:
            return []
        # Pull full manifests from main's capability registry (includes schemas)
        main = find_main_actor(self._registry)
        manifest_map: dict = {}
        if main:
            for cap in main.list_capabilities():
                manifest_map[cap["name"]] = cap

        workers = []
        seen = set()

        # ── Local actors ──────────────────────────────────────────────────────
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

        # ── Remote agents from live node heartbeats ───────────────────────────
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

    # ── Decomposition ──────────────────────────────────────────────────────

    async def _decompose(self, task: str, workers: list[dict]) -> list[dict]:
        """LLM breaks task into steps. Can declare missing agents with spawn configs."""
        if not self.llm:
            return []

        def _fmt_worker(w: dict) -> str:
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
                    lines.append(
                        f"    topic '{topic}' payload fields: {fields}  example: {example}"
                    )
            return "\n".join(lines)

        workers_desc = "\n".join(_fmt_worker(w) for w in workers)

        # ── Gather live topic samples for schema context ──────────────────
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
        except Exception:
            pass

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
        except Exception as e:
            logger.error(f"[{self.name}] Decomposition error: {e}")
        return []

    # ── Missing agent spawning ─────────────────────────────────────────────

    async def _ensure_agents(self, plan: list[dict]) -> list[dict]:
        """For any step with a spawn_config, spawn the agent if it's not running.
        Updates the plan with the actual agent name once spawned.

        Continuous agents (those with a process() loop or subscribe-based setup)
        are marked with _spawn_only=True so _execute_step skips delegation —
        spawning them WAS the action.
        """
        if not self._registry:
            return plan

        for step in plan:
            spawn_config = step.get("spawn_config")
            if not spawn_config:
                continue

            agent_name = spawn_config.get("name") or step.get("agent")
            if not agent_name:
                continue
            existing = self._registry.find_by_name(agent_name)

            if existing:
                await self._log(f"Agent '{agent_name}' already running — skipping spawn")
                step["agent"] = agent_name
                continue

            await self._log(f"Spawning missing agent: '{agent_name}'")
            try:
                actor = await self._spawn_agent(spawn_config)
                if actor:
                    step["agent"] = agent_name
                    self._spawned_by_planner.append(agent_name)

                    # Detect if this is a continuous/persistent agent.
                    # If the code has a process() loop or uses agent.subscribe(),
                    # delegation via TASK would just timeout — spawning IS the action.
                    #
                    # An explicit spawn_config["continuous"] (bool) always wins over
                    # the string heuristic, so a planner that knows the agent's mode
                    # can declare it instead of relying on substring matching (which
                    # misclassifies agents that both subscribe and answer tasks).
                    code = spawn_config.get("code", "")
                    explicit = spawn_config.get("continuous")
                    if explicit is not None:
                        is_continuous = bool(explicit)
                    else:
                        is_continuous = bool(
                            spawn_config.get("type") == "dynamic"
                            and code
                            and (
                                "def process(" in code
                                or "agent.subscribe(" in code
                                or "agent.window(" in code
                            )
                            # Only if there's no meaningful handle_task that does work
                            and "def handle_task(" not in code
                        )
                    if is_continuous:
                        step["_spawn_only"] = True
                        await self._log(
                            f"'{agent_name}' is continuous — spawn is the action, skipping delegation"
                        )

                    # Brief pause to let agent initialise
                    await asyncio.sleep(1.0)
                    await self._log(f"'{agent_name}' ready.")
                else:
                    await self._log(
                        f"Failed to spawn '{agent_name}' — step will use main as fallback"
                    )
                    step["agent"] = "main"
            except Exception as e:
                logger.error(f"[{self.name}] Spawn of '{agent_name}' failed: {e}")
                step["agent"] = "main"

        return plan

    async def _spawn_agent(self, config: dict) -> Actor | SpawnPlaceholder | None:
        """Spawn an agent for a plan step. Delegates to the shared SpawnMixin.

        Uses the BLOCKING install path: a pipeline's next step may depend on
        this agent being live, so we wait for any package install to finish
        rather than returning a placeholder.
        """
        return await self._spawn_local_from_config(config, register=True, blocking_install=True)

    # ── Execution ──────────────────────────────────────────────────────────

    async def _execute(self, plan: list[dict[str, Any]]) -> dict[str, Any]:
        results: dict = {}
        completed: set[int | str] = set()
        remaining: list[dict[str, Any]] = list(plan)

        # ── Validate dependency references up front ────────────────────────
        # A step whose depends_on points at a step number not in the plan can
        # never become ready and would stall the whole batch. Surface it as a
        # failed step (and mark it 'completed' so its dependents can resolve)
        # rather than silently deadlocking.
        valid_ids = {s.get("step") for s in plan}
        for s in list(remaining):
            bad = [d for d in (s.get("depends_on") or []) if d not in valid_ids]
            if bad:
                step = s.get("step")
                if not isinstance(step, (int, str)):
                    step = str(step)
                logger.error(
                    f"[{self.name}] Step {step} depends on missing step(s) {bad} — marking failed"
                )
                results[step] = {"error": f"unsatisfiable dependency on step(s) {bad}"}
                completed.add(step)
                remaining.remove(s)

        while remaining:
            ready = [
                s for s in remaining if all(d in completed for d in (s.get("depends_on") or []))
            ]
            if not ready:
                # Cyclic or otherwise unschedulable. Don't silently drop work —
                # record an error for every stuck step so synthesis (and the
                # user) sees that the plan was only partially executed.
                stuck = [s.get("step") for s in remaining]
                logger.error(
                    f"[{self.name}] Plan deadlock — unschedulable steps {stuck} "
                    f"(circular depends_on?)"
                )
                for s in remaining:
                    results[s.get("step")] = {
                        "error": f"skipped — circular/unschedulable dependency "
                        f"(step {s.get('step')})"
                    }
                break

            parallel = [s for s in ready if s.get("parallel", False)]
            sequential = [s for s in ready if not s.get("parallel", False)]

            if parallel:
                await self._log(f"Parallel: steps {[s['step'] for s in parallel]}")
                outputs = await asyncio.gather(
                    *[self._execute_step(s, results) for s in parallel],
                    return_exceptions=True,
                )
                for step, out in zip(parallel, outputs, strict=False):
                    results[step["step"]] = (
                        out if not isinstance(out, Exception) else {"error": str(out)}
                    )
                    completed.add(step["step"])
                    remaining.remove(step)

            for step in sequential:
                await self._log(f"Sequential: step {step['step']} → @{step['agent']}")
                results[step["step"]] = await self._execute_step(step, results)
                completed.add(step["step"])
                remaining.remove(step)

        return results

    async def _execute_step(self, step: dict, prior: dict) -> dict:
        agent_name = step.get("agent", "main")
        task_text = step.get("task", "")
        depends_on = step.get("depends_on") or []

        # Continuous agents (process loop / subscribe-based) were already started
        # by _ensure_agents — spawning them WAS the action. Don't send a TASK
        # that would just timeout because there's no handle_task to respond.
        if step.get("_spawn_only"):
            await self._log(f"  ✓ @{agent_name}: spawned and running (continuous agent)")
            return {
                "result": f"Agent '{agent_name}' spawned and running continuously.",
                "spawned": True,
            }

        # Inject context from prior steps
        if depends_on:
            ctx = []
            for dep in depends_on:
                r = prior.get(dep, {})
                t = (r.get("result") or r.get("text") or r.get("answer") or str(r))[:600]
                ctx.append(f"[Step {dep} result]: {t}")
            if ctx:
                task_text += "\n\nContext from previous steps:\n" + "\n".join(ctx)

        if agent_name in ("main", self.name):
            return {"result": await self._llm_answer(task_text)}

        await self._log(f"  → @{agent_name}: {task_text[:60]}")
        result = await self._delegate(agent_name, task_text)
        if not result:
            return {"error": f"No response from {agent_name}"}
        # If agent reported an error, check if we can replan around it
        if "error" in result and "error_phase" in result:
            await self._log(
                f"  ⚠ @{agent_name} failed ({result['error_phase']}): {result['error'][:80]}"
            )
            # Try main as fallback synthesizer
            await self._log("  → falling back to @main for this step")
            fallback = await self._llm_answer(
                f"The agent '{agent_name}' failed. Do your best to answer: {task_text}"
            )
            return {"result": fallback, "fallback": True, "original_error": result["error"]}
        return result

    # ── Delegation ─────────────────────────────────────────────────────────

    async def _delegate(self, agent_name: str, task: str, timeout: float = 60.0) -> dict | None:
        return await self._delegate_with_payload(agent_name, {"text": task}, timeout=timeout)

    async def _delegate_with_payload(
        self, agent_name: str, payload: dict, timeout: float = 60.0
    ) -> dict | None:
        if not self._registry:
            return None
        target = self._registry.find_by_name(agent_name)
        if not target:
            logger.warning(f"[{self.name}] Agent '{agent_name}' not found for delegation")
            return {"error": f"Agent '{agent_name}' not found"}

        import uuid

        task_id = str(uuid.uuid4())[:8]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._result_futures[task_id] = future

        await self.send(
            target.actor_id,
            MessageType.TASK,
            {**payload, "_task_id": task_id, "_reply_to": self.actor_id},
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Timeout from '{agent_name}'")
            return {"error": f"Timeout from {agent_name}"}
        finally:
            self._result_futures.pop(task_id, None)

    # ── Synthesis ──────────────────────────────────────────────────────────

    async def _synthesize(self, task: str, plan: list[dict], results: dict) -> str:
        # If every step was a spawn-only continuous agent, skip LLM synthesis
        # and return a clean confirmation — no need to "summarize" spawns.
        all_spawned = all(
            isinstance(results.get(s["step"]), dict) and results[s["step"]].get("spawned")
            for s in plan
        )
        if all_spawned:
            agents = [s["agent"] for s in plan]
            lines = [f"Done! Spawned {len(agents)} continuous agent(s):\n"]
            for s in plan:
                desc = ""
                sc = s.get("spawn_config") or {}
                desc = sc.get("description", s.get("task", ""))
                lines.append(f"• **{s['agent']}** — {desc}")
            lines.append("\nThey're running now and will auto-restore on restart.")
            return "\n".join(lines)

        if not self.llm:
            parts = []
            for s in plan:
                r = results.get(s["step"], {})
                t = r.get("result") or r.get("text") or r.get("answer") or str(r)
                parts.append(f"[@{s['agent']}]: {t}")
            return "\n\n".join(parts)

        results_text = []
        for s in plan:
            r = results.get(s["step"], {})
            t = (r.get("result") or r.get("text") or r.get("answer") or str(r))[:800]
            results_text.append(f"Step {s['step']} (@{s['agent']}): {t}")

        prompt = (
            f"You collected results from multiple agents for this task:\n\n"
            f"ORIGINAL TASK: {task}\n\n"
            f"RESULTS:\n"
            + "\n\n".join(results_text)
            + "\n\nSynthesize into a single, clear, well-structured answer for the user. "
            "Do not mention agent names, step numbers, or internal system details."
        )
        try:
            response, _usage = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You synthesize multi-agent results into clean, user-facing answers.",
                max_tokens=2048,
            )
            self._accrue_usage(_usage)
            return response
        except Exception as e:
            logger.error(f"[{self.name}] Synthesis failed: {e}")
            return "\n\n".join(results_text)

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
            return response
        except Exception as e:
            return f"[LLM error: {e}]"

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _bootstrap_ha_entity_states(self, task: str, plan: list[dict] | None = None) -> None:
        """After pipeline agents are spawned they sit idle until the next MQTT
        change arrives.  If the relevant HA entity is *already* in the desired
        state (e.g. lights are already on) that change never comes, so the
        agents never fire.

        Fix: directly fetch current HA states and publish them to
        homeassistant/state_changes/# so freshly-spawned agents get an
        immediate bootstrap event without waiting for a real state change.

        Entity IDs are extracted from (in order of reliability):
          1. spawn_config["code"]        — generated agent code always contains
                                           the literal entity_id string
          2. spawn_config["actions"]     — ha_actuator explicit entity_id fields
          3. spawn_config["mqtt_topics"] — per-entity topic path segments
          4. The enriched task string    — [HA entity: sensor.xxx] annotations
        """
        import uuid

        HA_DOMAINS = {
            "sensor",
            "binary_sensor",
            "light",
            "switch",
            "climate",
            "cover",
            "media_player",
            "input_boolean",
            "input_number",
            "input_select",
            "automation",
            "script",
            "scene",
            "group",
            "person",
            "zone",
            "device_tracker",
            "alarm_control_panel",
            "camera",
            "fan",
            "vacuum",
            "lock",
            "humidifier",
            "water_heater",
            "number",
            "select",
            "button",
            "update",
            "event",
        }
        HA_ENTITY_RE = re.compile(r"\b([a-z_][a-z0-9_]*\.[a-z0-9_]+)\b")

        seen: set[str] = set()
        entity_ids: list[str] = []

        def _add(eid: str) -> None:
            eid = eid.strip().lower()
            if eid and eid not in seen and eid.split(".")[0] in HA_DOMAINS:
                seen.add(eid)
                entity_ids.append(eid)

        for step in plan or []:
            cfg = step.get("spawn_config") or {}

            # Source 1: generated agent code — the LLM embeds the real entity_id
            # as a string literal, e.g.: payload.get('entity_id') == 'sensor.ewelink_...'
            for m in HA_ENTITY_RE.finditer(cfg.get("code", "")):
                _add(m.group(1))

            # Source 2: ha_actuator actions
            for action in cfg.get("actions") or []:
                _add(action.get("entity_id", ""))

            # Source 3: per-entity mqtt_topics
            for topic in cfg.get("mqtt_topics") or []:
                m = re.search(r"homeassistant/state_changes/[^/]+/([^/#]+)", topic)
                if m:
                    _add(m.group(1))

        # Source 4: enriched task string
        for m in HA_ENTITY_RE.finditer(task.lower()):
            _add(m.group(1))

        await self._log(f"Bootstrap — entity IDs found: {entity_ids}")

        if not entity_ids:
            await self._log("Bootstrap: no HA entity IDs found — skipping")
            return

        if not self._registry:
            return

        ha_actor = self._registry.find_by_name("home-assistant-agent")
        if not ha_actor:
            await self._log("Bootstrap skipped — home-assistant-agent not running")
            return

        # Wait for spawned agents to complete setup() and subscribe before
        # the bootstrap MQTT messages land.
        await asyncio.sleep(1.5)

        entity_list = " ".join(entity_ids)
        await self._log(f"Bootstrap — sending get_entities_state to HA agent for: {entity_ids}")

        task_id = str(uuid.uuid4())[:8]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._result_futures[task_id] = future
        try:
            await self.send(
                ha_actor.actor_id,
                MessageType.TASK,
                {
                    "text": f"get_entities_state {entity_list}",
                    "_task_id": task_id,
                    "_reply_to": self.actor_id,
                },
            )
            result = await asyncio.wait_for(future, timeout=15.0)
            await self._log(f"Bootstrap — HA agent responded: {result.get('result', '')[:120]}")
        except asyncio.TimeoutError:
            await self._log("Bootstrap — HA agent timed out")
        except Exception as exc:
            await self._log(f"Bootstrap — error: {exc}")
        finally:
            self._result_futures.pop(task_id, None)

    async def _lifetime_watchdog(self):
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

    async def _terminate(self):
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
        if self._registry:
            sup = getattr(self._registry, "_supervisor_ref", None)
            if sup is not None:
                try:
                    sup.release(self.name)
                except Exception:
                    pass

        if self._registry:
            try:
                await self._registry.unregister(self.actor_id)
            except Exception:
                pass
        try:
            await self.stop()
        except Exception:
            pass

    async def _deferred_stop(self, delay: float = 2.0):
        await asyncio.sleep(delay)
        await self._terminate()

    async def _log(self, msg: str):
        logger.info(f"[{self.name}] {msg}")
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": msg, "timestamp": time.time()},
        )
