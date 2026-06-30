"""
PlannerAgent — On-demand task orchestrator with plan caching and auto-spawning.

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
import time

from ..core.actor import Actor, Message, MessageType
from ..core.mqtt import mqtt_client
from .llm_agent import LLMProvider, _accumulate_global_cost
from .mixins import SpawnMixin

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
_PLAN_CACHE_KEY = "_plan_cache"
_CACHE_TTL_S = 86400  # 24 hours


class PlannerAgent(Actor, SpawnMixin):
    """
    On-demand orchestrator. Spawned per complex task, self-terminates when done.
    """

    def __init__(
        self,
        llm_provider: LLMProvider | None = None,
        task: str = "",
        reply_to_id: str = "",
        reply_task_id: str = "",
        auto_terminate: bool = True,
        plan_only: bool = False,
        approved_plan: dict | None = None,
        max_lifetime_s: float = 90.0,
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
            _accumulate_global_cost(delta)
            self._last_period_cost_usd = self.total_cost_usd

    def _now_context(self) -> str:
        """
        Live date/time block for planning prompts. Resolves the user's timezone
        from main's facts (same source the scheduler uses) so a "tomorrow at 3pm"
        request is decomposed against the correct calendar date and zone.
        """
        user_tz = None
        if self._registry:
            main = self._registry.find_by_name("main")
            if main and hasattr(main, "get_user_facts"):
                try:
                    user_tz = main.get_user_facts().get("pref_timezone")
                except Exception:
                    pass
        from .llm_agent import current_time_context

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
                reply = {"result": result, "text": result}
                if resolve_id:
                    reply["_task_id"] = resolve_id
                if self._spawned_by_planner:
                    reply["spawned"] = self._spawned_by_planner
                await self.send(self._reply_to_id, MessageType.RESULT, reply)

        elif msg.type == MessageType.RESULT:
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            task_id = payload.get("_task_id")
            if task_id and task_id in self._result_futures:
                fut = self._result_futures[task_id]
                if not fut.done():
                    fut.set_result(payload)

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

    # ── Core pipeline ──────────────────────────────────────────────────────

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

    @staticmethod
    def _is_pipeline_request(task: str) -> bool:
        """
        Detect reactive/persistent pipeline requests vs one-shot tasks.
        Pipelines use conditional/temporal language: if/when/whenever/monitor/watch/notify.
        Also catches explicit spawn/continuous-agent requests like:
          "spawn an agent to log the mean..."
          "create an agent that subscribes to..."
          "I want an agent to send to a topic random temp..."
        """
        import re

        lowered = task.lower()

        # Explicit pipeline prefix always wins
        if lowered.startswith("pipeline:") or lowered.startswith("pipeline "):
            return True

        patterns = [
            r"\bif\b.*\bthen\b",
            r"\bif\b.*\b(send|notify|alert|turn|open|close|post|message|say|tell|warn|log|print|publish|emit)\b",
            r"\bwhen\b.*\b(detect|open|turn|send|notify|alert|is|becomes|goes|changes|say|warn|log)\b",
            r"\bwhenever\b",
            r"\bmonitor\b",
            r"\bwatch\b",
            r"\bcheck\b.*\b(every|continuously|periodically|if|when)\b",
            r"\balert me\b",
            r"\bnotify me\b",
            r"\btell me\b.*\bif\b",
            r"\bsend me\b.*\b(when|if|discord|message|notification)\b",
            r"\bsend me a\b",
            r"\bautomatically\b",
            r"\bevery time\b",
            r"\bon detection\b",
            r"\bis turned on\b",
            r"\bis turned off\b",
            r"\bturns on\b",
            r"\bturns off\b",
            r"\bopens\b.*\b(send|notify|alert|light|turn)\b",
            r"\b(door|window|sensor|lamp|light|temperature|humidity|motion)\b.*\b(send|notify|discord|message)\b",
            # camera/detect + action = pipeline
            r"\b(camera|detect|yolo|webcam)\b.*\b(turn|open|send|notify|alert)\b",
            r"\b(person|motion|object)\b.*\bdetect.*\b(turn|open|light|send)\b",
            # ── Spawn / continuous agent / app requests ──
            # "spawn an agent to...", "create an agent that...", "I want an agent to...",
            # "spawn an app that...", "build a service that...", etc.
            # NB: matches "agent" OR "app" OR "bot" OR "service" OR "monitor" OR "rule"
            r"\b(spawn|create|make|start|run|launch|deploy|build|set\s+up)\b.*\b(agent|app|bot|service|monitor|rule|pipeline|listener|handler|watcher)\b",
            r"\b(i\s+want|i\s+need|i'd\s+like)\b.*\b(agent|app|bot|service|rule|something)\b.*\b(to|that|which)\b",
            # Periodic / continuous language
            r"\bevery\s+\d+\s*(sec|min|hour|s\b|m\b|h\b)",
            r"\bcontinuously\b",
            r"\bconstantly\b",
            r"\bperiodically\b",
            r"\bkeep\s+(running|publishing|logging|sending|checking)\b",
            r"\b(subscribe|listen)\s+(to|for|on)\b",
            r"\blog\s+(the|every|each|all)\b",
            # ── Clock-time triggers (5pm, 7am, 17:00, every weekday, etc.) ──
            # These should always be pipelines because they need a ScheduledAgent.
            r"\bat\s+\d{1,2}(:\d{2})?\s*(am|pm)?\b",  # 'at 5pm', 'at 09:30', 'at 7 am'
            r"\b(every|each)\s+(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            r"\b(every|each)\s+(weekday|weekend|day|morning|evening|night|afternoon|hour|minute)\b",
            r"\bdaily\b.*\b(at|on|when)\b",
            r"\bweekly\b",
            r"\bnightly\b",
            r"\btomorrow\b.*\b(at|morning|evening|night)\b",
            r"\bremind\s+me\b",
            r"\bschedule\b.*\b(to|for|every|at)\b",
        ]
        return any(re.search(p, lowered) for p in patterns)

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
            from ..core.topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus and self._registry:
                live = {a.name for a in self._registry.all_actors()}
                # Add remotely-running agents from main's known_nodes
                main = self._registry.find_by_name("main")
                if main and hasattr(main, "_known_nodes"):
                    import time as _pt

                    for nd in main._known_nodes.values():
                        if _pt.time() - nd.get("last_seen", 0) < 30:
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
        is_pipeline = PlannerAgent._is_pipeline_request(task)
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
        cache_key = _task_hash(task)
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
        """
        Builds and spawns persistent reactive agents for if/when/wherever rules.

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
        """
        Spawn each agent in the plan, register with main, build the final summary.
        Extracted from _run_pipeline so dry-run can reuse the same execution path
        after a user approval.

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
                    main = self._registry.find_by_name("main")
                    if main and hasattr(main, "_save_to_spawn_registry"):
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
            import hashlib as _hl

            rule_id = _hl.md5(task.encode()).hexdigest()[:8]
            rule = {
                "rule_id": rule_id,
                "task": task,
                "agents": rule_agents,
                "created_at": time.time(),
            }
            # Save into main so it survives planner self-termination
            if self._registry:
                main = self._registry.find_by_name("main")
                if main and hasattr(main, "save_pipeline_rule"):
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
            main = self._registry.find_by_name("main")
            if main and hasattr(main, "get_pipeline_rules"):
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
            "You are reviewing a NEW home-automation rule against rules that "
            "are ALREADY ACTIVE. Flag only two things:\n"
            "  1. DUPLICATE — the new rule does essentially the same thing as "
            "an existing one (same trigger AND same action).\n"
            "  2. CONTRADICTION — the new rule fires on the same or overlapping "
            "condition but takes an OPPOSING action (e.g. one turns a device "
            "ON, the other turns it OFF under the same condition).\n\n"
            f"NEW RULE:\n{task}\n\n"
            "ALREADY-ACTIVE RULES:\n" + "\n".join(existing_lines) + "\n\n"
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
            data = json.loads(self._extract_json_object(response))
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
        """
        Resolve vague data references in a task to concrete MQTT topics or HA entities.

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
        import re

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
            from ..core.topic_bus import get_topic_bus

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
        """
        Peek at one live MQTT message from each registered publish topic.
        Returns formatted lines with actual field names and an example value.

        This is the fallback when observed_samples haven't been captured yet
        (e.g. the producer started before the schema-capture code was deployed).

        Uses a single MQTT connection with a short per-topic timeout so it
        doesn't block planning. Topics that don't publish within the window
        are silently skipped.
        """
        import json as _json

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
                                payload = _json.loads(msg.payload.decode())
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
        """
        Decomposes a reactive pipeline request into persistent agent spawn configs.

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
                from ..config import CONFIG
                from ..core.integrations.home_assistant.ha_helper import (
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
            ha_entities_text
            if ha_entities_text
            else "  (HA not reachable — use entity IDs provided by the user)"
        )

        # ── Resolve real camera stream URLs via home-assistant-agent ───────
        # Mirrors the entity-list delegation above: ground PATTERN 3 (camera
        # detection pipelines) in real stream URLs instead of letting the LLM
        # invent /dev/video0 or guess proxy paths.
        camera_stream_urls: dict[str, str] = {}
        camera_snapshot_urls: dict[str, str] = {}
        try:
            import re as _re_cam

            camera_entity_ids = []
            camera_lines = []
            for line in ha_entities_text.splitlines():
                token = line.strip().split(" ", 1)[0]
                if token.startswith("camera."):
                    camera_lines.append(line.strip())
                    if token not in camera_entity_ids:
                        camera_entity_ids.append(token)

            if camera_entity_ids:
                task_words = {w for w in _re_cam.findall(r"[a-z0-9]+", task.lower()) if len(w) >= 3}
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
            from ..core.topic_bus import get_topic_bus

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
            main = self._registry.find_by_name("main")
            if main and hasattr(main, "get_notification_urls"):
                notification_urls = main.get_notification_urls()

        # Also extract any URL directly mentioned in the task
        import re as _re

        _url_match = _re.search(
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
            feas_prompt = (
                "You are checking whether a reactive HA automation can be built with the available entities.\n\n"
                f"USER REQUEST: {task}\n\n"
                f"AVAILABLE HA ENTITIES:\n{ha_section}\n\n"
                "Return JSON only:\n"
                '{"feasible": true/false, "reason": "<one sentence if not feasible>", "relevant_entities": ["entity_id", ...]}\n\n'
                "Rules — be PERMISSIVE, default to feasible=true:\n"
                "- Match by FUZZY SUBSTRING. 'lamp' matches 'light.wiz_rgbw_*' or "
                "any entity_id/name containing 'lamp', 'light', or 'lamp'-like words.\n"
                "- 'door' matches binary_sensor.*_door, sensor.*_door, etc.\n"
                "- 'occupancy'/'motion'/'presence' match any binary_sensor with those words.\n"
                "- 'temperature' matches sensor.*_temperature.\n"
                "- 'my <X>' / 'the <X>' just means the user's <X> — if ANY entity plausibly matches, feasible=true.\n"
                "- feasible=false ONLY when there is genuinely NO entity whose entity_id, name, or "
                "platform plausibly matches the requested target. If unsure, return feasible=true.\n"
                "- relevant_entities should list the matching entity_ids you'd use.\n"
                "- Camera/webcam/Discord/notification requests: always feasible=true.\n"
                "- Pure logging / observability tasks (no HA target): always feasible=true. "
                "Examples: 'log a heartbeat every hour', 'write a warning when X', 'print uptime'.\n"
                "- Time-based triggers without HA action (just logging or publishing): always feasible=true."
            )
            try:
                feas_resp, _usage = await self.llm.complete(
                    messages=[{"role": "user", "content": feas_prompt}],
                    system=self._now_context() + "\nOutput only valid JSON. No markdown.",
                    max_tokens=400,
                )
                self._accrue_usage(_usage)
                clean = feas_resp.strip()
                for fence in ("```json", "```"):
                    if clean.startswith(fence):
                        clean = clean[len(fence) :]
                    if clean.endswith("```"):
                        clean = clean[:-3]
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
            "You are designing reactive automation pipelines for a multi-agent IoT system.",
            "Output ONLY a valid JSON array — no explanation, no markdown, no code fences.",
            "",
            "═══ SYSTEM ARCHITECTURE ═══",
            "",
            "HomeAssistantStateBridgeAgent (ALWAYS running, NEVER spawn again):",
            "  Publishes every HA state change to MQTT.",
            "  Topic format depends on HA_STATE_BRIDGE_PER_ENTITY config — can be either:",
            "    Flat:       homeassistant/state_changes                          (all entities, one topic)",
            "    Per-entity: homeassistant/state_changes/{domain}/{full_entity_id} (one topic per entity)",
            "  ALWAYS subscribe to the wildcard: homeassistant/state_changes/#",
            "  This catches BOTH formats and never breaks regardless of config.",
            '  Payload always contains: {"entity_id": "light.wiz_...", "domain": "light", "new_state": {"state": "on", ...}, "old_state": {...}}',
            "  Filter by entity_id IN THE PAYLOAD — never rely on the topic path for filtering.",
            "  NOTE: 'state' is NESTED inside new_state — check payload['new_state']['state'].",
            "",
            "═══ AGENT TYPES ═══",
            "",
            'TYPE 1 — "ha_actuator"',
            "  Purpose: call any Home Assistant service (turn_on, turn_off, set_temperature, open_cover, etc.)",
            "  No code needed. Subscribes to an MQTT trigger topic and calls the HA service.",
            "  detection_filter matches TOP-LEVEL keys of the incoming payload only.",
            "  spawn_config schema:",
            '    "type": "ha_actuator"',
            '    "automation_id": "<unique-kebab-id>"',
            '    "description": "<what this does>"',
            '    "mqtt_topics": ["<trigger-topic>"]',
            '    "actions": [{"domain": "<ha-domain>", "service": "<ha-service>", "entity_id": "<entity_id-from-list>", "service_data": {}}]',
            '    "conditions": []',
            '    "detection_filter": {"<top-level-key>": <value>} or null',
            '    "cooldown_seconds": <number>',
            "",
            'TYPE 2 — "scheduled"',
            "  Purpose: fire an event at a SPECIFIC time or interval. THE ONLY correct way",
            "  to express any time-based trigger (5pm, every weekday, every 30 minutes).",
            "  No code. No polling loop. The framework wakes precisely at fire time.",
            "  CRITICAL — when to use scheduled vs dynamic:",
            "    'at 5pm', 'every day at 7am', 'every Monday', 'every 30 minutes',",
            "    'tomorrow at 9am', 'every hour' → ALWAYS use type=scheduled.",
            "    NEVER write a dynamic agent that polls datetime.now() in a loop.",
            "    NEVER write a dynamic agent with `while True: asyncio.sleep(60)` to check time.",
            "  Schedule spec — dict with one of these shapes:",
            '    Daily:    {"type": "daily",    "at": "17:00"}',
            '    Weekly:   {"type": "weekly",   "at": "07:30", "days": ["mon","tue","wed","thu","fri"]}',
            '    Interval: {"type": "interval", "seconds": 1800}',
            '    Once:     {"type": "once",     "at": "2026-12-25T09:00:00"}',
            "  spawn_config schema:",
            '    "type": "scheduled"',
            '    "description": "<what this fires>"',
            '    "schedule": <one of the dicts above>',
            '    "publish_topic": "schedule/<name>/fired"   (optional — defaults to this anyway)',
            "  When the schedule fires, payload published is:",
            '    {"fired_at": "<ISO-8601 UTC>", "schedule_type": "<type>", "agent": "<name>", "manual": false}',
            "  Pair with a downstream consumer (ha_actuator or dynamic agent) that subscribes",
            "  to the publish_topic and performs the actual action. See PATTERN 5 below.",
            "",
            'TYPE 3 — "dynamic"',
            "  Purpose: any logic that needs code — state filtering, webcam, timers, HTTP webhooks, Discord, etc.",
            "  Define these async functions (all optional except at least one must exist):",
            "    async def setup(agent)   — runs once on start, good for subscriptions and init",
            "    async def process(agent) — runs in a loop every poll_interval seconds",
            "  Available APIs (ONLY these — no other agent methods exist):",
            '    await agent.log("message")                        — structured log (ASYNC, must await)',
            '    await agent.publish("topic", {dict})              — publish to MQTT (ASYNC, must await)',
            '    await agent.alert("message")                      — trigger alert (ASYNC, must await)',
            '    await agent.send_to("name", payload)              — delegate to agent (ASYNC, must await)',
            '    await agent.mqtt_get("topic")                     — one-shot MQTT read (ASYNC, must await)',
            '    agent.subscribe("topic", async_callback)          — subscribe to MQTT (SYNC, NO await!)',
            "                                                        callback(payload_dict) per message",
            "                                                        runs as background task, setup() returns immediately",
            '    agent.window("topic", seconds=N)                  — sliding window (SYNC, NO await!)',
            '    agent.recall("key")                               — load persisted value (SYNC, NO await!)',
            '    agent.persist("key", value)                       — save persisted value (SYNC, NO await!)',
            "    agent.declare_contract(...)                        — register topic contract (SYNC, NO await!)",
            '    agent.state["key"]                                — in-memory dict (cleared on restart)',
            "  CRITICAL RULES FOR DYNAMIC AGENT CODE:",
            "    NEVER use await on agent.subscribe(), agent.window(), agent.persist(), agent.recall(), agent.declare_contract()",
            "    NEVER import or use aiomqtt directly — use agent.subscribe() instead",
            "    NEVER hardcode MQTT broker hostnames or ports — agent.subscribe() handles this automatically",
            "    NEVER use asyncio.create_task() for MQTT — agent.subscribe() already creates the background task",
            "    agent.subscribe() is non-blocking — call it in setup() and return immediately",
            "  spawn_config schema:",
            '    "type": "dynamic"',
            '    "description": "<what this does>"',
            '    "install": ["<pip-package>", ...]       — packages to install before running',
            '    "poll_interval": <seconds>              — how often process(agent) runs',
            '    "code": "<full python source as single string with \\n for newlines>"',
            "",
            "═══ CANONICAL WIRING PATTERNS ═══",
            "",
            "PATTERN 1 — HA sensor triggers HA action (door → light, motion → switch, temp → AC):",
            "  Problem: HA state is nested in new_state.state, ha_actuator can only filter top-level keys.",
            "  Solution: use a dynamic filter agent to extract and re-publish the trigger.",
            "  Agent 1 (dynamic, name: '<slug>-state-filter'):",
            "    setup(agent): use agent.subscribe() to listen to homeassistant/state_changes/{domain}/{entity_id}",
            "      Check new_state['state'] against condition, if met: await agent.publish('custom/triggers/<slug>', {'triggered': True})",
            "    agent.subscribe() runs as a background task — setup() must return immediately after calling it.",
            "  Agent 2 (ha_actuator, name: '<slug>-actuator'):",
            "    mqtt_topics: ['custom/triggers/<slug>']",
            "    detection_filter: {'triggered': True}",
            "    actions: [the HA service call with the correct entity_id]",
            "  CONDITION EXAMPLES:",
            "    Binary sensor (door/window/motion): new_state['state'] == 'on'",
            "    Numeric sensor (temperature/humidity): float(new_state.get('state', 0)) > threshold",
            "    Switch/light: new_state['state'] == 'on' or 'off'",
            "  PATTERN 1 CODE TEMPLATE:",
            "    async def setup(agent):",
            "        async def on_state(payload):",
            "            if payload.get('entity_id') != 'light.wiz_rgbw_tunable_02cba0': return",
            "            state = payload.get('new_state', {}).get('state', '')",
            "            if state == 'on':  # adapt condition to user request",
            "                await agent.publish('custom/triggers/<slug>', {'triggered': True, 'state': state})",
            "        # Use wildcard — works regardless of per-entity or flat topic config",
            "        agent.subscribe('homeassistant/state_changes/#', on_state)",
            "",
            "PATTERN 2 — HA sensor triggers notification (Discord, Slack, HTTP webhook):",
            "  ONE dynamic agent using agent.subscribe():",
            "    async def setup(agent):",
            "        async def on_state(payload):",
            "            if payload.get('entity_id') != 'light.wiz_rgbw_tunable_02cba0': return",
            "            state = payload.get('new_state', {}).get('state', '')",
            "            if state == 'on':  # adapt condition",
            "                import httpx",
            "                async with httpx.AsyncClient() as c:",
            "                    await c.post('<WEBHOOK_URL>', json={'content': 'Lamp turned on!'})",
            "                await agent.log('Discord notification sent')",
            "        # Use wildcard — works regardless of per-entity or flat topic config",
            "        agent.subscribe('homeassistant/state_changes/#', on_state)",
            "  Install: httpx",
            "  IMPORTANT: use the exact webhook URL from NOTIFICATION URLS section below.",
            "",
            "PATTERN 3 — Webcam/camera object detection triggers HA action:",
            "  Agent 1 (dynamic, name: '<slug>-camera-detect'):",
            "    setup(agent): the CAMERA STREAM URLS below are mjpeg_proxy URLs (/api/camera_proxy_stream/...)",
            "      and REQUIRE the HA token as a Bearer header. Before calling cv2.VideoCapture, set:",
            "        import os",
            "        os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = f\"headers;Authorization: Bearer {os.environ['HA_TOKEN']}\\r\\n\"",
            "      Then load YOLO model and open the stream with cv2.VideoCapture(<url>)",
            "      using the EXACT URL from CAMERA STREAM URLS below — never /dev/video0 or a guessed proxy path",
            "      IMPORTANT: read the token from os.environ['HA_TOKEN'] — NEVER hardcode the token value.",
            "    process(agent): capture frame, run inference, determine if target object is detected,",
            "      publish {'detected': bool, 'target': '<object-name>', 'objects': [list-of-all-detected]}",
            "      to custom/detections/<slug>",
            "    Install: ultralytics, opencv-python",
            "    poll_interval: 1",
            "  Agent 2 (ha_actuator, name: '<slug>-actuator'):",
            "    mqtt_topics: ['custom/detections/<slug>']",
            "    detection_filter: {'detected': True}",
            "    actions: [HA service call]",
            "  IMPORTANT: publish {'detected': bool} not {'person_detected': bool} — generic for any object.",
            "  IMPORTANT: use the exact camera stream URL from CAMERA STREAM URLS section below.",
            "  In code: target = '<object-name-from-user-request>'; detected = target in set(detected_labels)",
            "",
            "PATTERN 4 — Webcam detection triggers notification:",
            "  Agent 1: same as Pattern 3 agent 1",
            "  Agent 2 (dynamic, name: '<slug>-notify'):",
            "    setup(agent): use agent.subscribe() on custom/detections/<slug>",
            "      When detected=True: POST notification via httpx",
            "",
            "PATTERN 5 — Time-based trigger (clock time, recurring, or once):",
            "  ALWAYS use type=scheduled for ANY clock-time trigger. Two-agent pattern:",
            "  Agent 1 (scheduled, name: '<slug>-trigger'):",
            '    schedule: {"type": "daily", "at": "17:00"} (or weekly/interval/once)',
            "    publish_topic: 'schedule/<slug>-trigger/fired'  (or omit for default)",
            "  Agent 2 (ha_actuator OR dynamic, name: '<slug>-action'):",
            "    Subscribes to 'schedule/<slug>-trigger/fired'",
            "    For HA actions: type=ha_actuator, mqtt_topics=['schedule/<slug>-trigger/fired'],",
            "      detection_filter null (no filtering needed — every fire is a trigger),",
            "      actions=[the HA service call].",
            "    For notifications/custom code: type=dynamic, setup() subscribes via agent.subscribe(),",
            "      callback does the work (POST to webhook, log, etc.)",
            "  EXAMPLES of correct user-request → schedule mapping:",
            '    "turn on lights at 5pm"          → {"type": "daily", "at": "17:00"}',
            '    "every weekday at 7am"           → {"type": "weekly", "at": "07:00", "days": ["mon","tue","wed","thu","fri"]}',
            '    "every Saturday morning"         → {"type": "weekly", "at": "08:00", "days": ["sat"]}',
            '    "every 30 minutes"               → {"type": "interval", "seconds": 1800}',
            '    "every hour"                     → {"type": "interval", "seconds": 3600}',
            '    "tomorrow at 9am, remind me"     → {"type": "once", "at": "<tomorrow>T09:00:00"}',
            "  CRITICAL: NEVER express a clock time as a dynamic agent that polls datetime.now().",
            "  NEVER use 'while True: sleep(60)' to wait for a time. Always use type=scheduled.",
            "",
            "PATTERN 6 — MQTT sensor data + condition → HA action (e.g. 'if temp > 20 turn off lamp'):",
            "  This combines multiple data sources and triggers an HA action. NEVER use httpx for HA!",
            "  Agent 1 (dynamic, name: '<slug>-monitor'):",
            "    setup(agent): subscribe to relevant MQTT topics using agent.subscribe()",
            "      In callback: check conditions, if met → await agent.publish('custom/triggers/<slug>', {'triggered': True})",
            "    Example: subscribe to sensor topic AND HA state topic, check both conditions",
            "  Agent 2 (ha_actuator, name: '<slug>-actuator'):",
            "    mqtt_topics: ['custom/triggers/<slug>']",
            "    detection_filter: {'triggered': True}",
            "    actions: [{'domain': 'light', 'service': 'turn_off', 'entity_id': 'light.xxx'}]",
            "  PATTERN 6 CODE TEMPLATE:",
            "    async def setup(agent):",
            "        agent.state['lamp_on'] = False",
            "        agent.state['temp'] = 0",
            "        async def on_temp(payload):",
            "            agent.state['temp'] = payload.get('temp', 0)  # use EXACT field name from OBSERVED samples",
            "            await check_and_trigger()",
            "        async def on_lamp(payload):",
            "            agent.state['lamp_on'] = payload.get('state') == 'on'",
            "            await check_and_trigger()",
            "        async def check_and_trigger():",
            "            if agent.state['lamp_on'] and agent.state['temp'] > 20:",
            "                await agent.publish('custom/triggers/lamp-temp', {'triggered': True})",
            "                await agent.log('Condition met! Trigger published.')",
            "        agent.subscribe('custom/sensors/temp_humidity', on_temp)",
            "        agent.subscribe('lamp/status', on_lamp)",
            "",
            "PATTERN 7 — One-shot camera snapshot (e.g. 'take a snapshot of the office camera'):",
            "  Use this instead of PATTERN 3 when the task needs a SINGLE still image,",
            "  not a continuous detection loop.",
            "  Agent (dynamic, name: '<slug>-snapshot'):",
            "    setup(agent) or process(agent): fetch the EXACT URL from CAMERA SNAPSHOT URLS below.",
            "    Install: httpx",
            "  PATTERN 7 CODE TEMPLATE:",
            "    async def setup(agent):",
            "        import httpx, os",
            "        headers = {'Authorization': f\"Bearer {os.environ['HA_TOKEN']}\"}",
            "        async with httpx.AsyncClient() as client:",
            "            resp = await client.get('<snapshot-url-from-CAMERA-SNAPSHOT-URLS>', headers=headers)",
            "            image_bytes = resp.content",
            "        # ... process image_bytes (e.g. run YOLO on it once, save to disk, etc.)",
            "  IMPORTANT: read the token from os.environ['HA_TOKEN'] — NEVER hardcode the token value.",
            "  If the result feeds an HA action (e.g. 'if there is a desk, turn on the light'),",
            "  publish the detection result to a topic and pair with an ha_actuator (see PATTERN 3 agent 2).",
            "",
            "═══ GENERAL RULES ═══",
            "",
            "╔══════════════════════════════════════════════════════════════════╗",
            "║  CRITICAL — HOME ASSISTANT ACTIONS                              ║",
            "║  NEVER call HA REST API directly from dynamic agent code!       ║",
            "║  NEVER use httpx/requests to POST to /api/services/*.           ║",
            "║  ALWAYS use an ha_actuator agent for ANY HA service call.       ║",
            "║                                                                 ║",
            "║  CORRECT: dynamic agent publishes trigger → ha_actuator acts    ║",
            "║  WRONG:   dynamic agent calls httpx.post('http://ha/api/...')   ║",
            "╚══════════════════════════════════════════════════════════════════╝",
            "",
            "  If a dynamic agent needs to turn on/off a light, switch, or any HA device:",
            "    1. The dynamic agent publishes a trigger: await agent.publish('custom/triggers/<slug>', {'triggered': True})",
            "    2. A SEPARATE ha_actuator agent subscribes to that trigger and executes the HA service call",
            "  This is Patterns 1 and 5 — ALWAYS follow this two-agent pattern for HA actions.",
            "",
            "- Use EXACT entity_id values from the HA entities list — never invent entity IDs",
            "- For HA service calls (in ha_actuator config, NOT in dynamic agent code):",
            "  light → light.turn_on / light.turn_off",
            "  switch → switch.turn_on / switch.turn_off",
            "  climate → climate.set_temperature / climate.set_hvac_mode",
            "  cover → cover.open_cover / cover.close_cover",
            "  script → script.turn_on",
            "- Multiple rules in one request → output ALL agents for ALL rules",
            "- Each agent does exactly ONE job — keep it minimal",
            "- Replace <slug> consistently across paired agents with a short descriptive kebab-case id",
            "- ALWAYS subscribe to homeassistant/state_changes/# (wildcard) — NEVER to a specific sub-topic",
            "  Filter by entity_id in the payload: if payload.get('entity_id') != 'light.xyz': return",
            "  This works regardless of whether HA_STATE_BRIDGE_PER_ENTITY is on or off",
            "- If user provides a Discord webhook URL, use it directly in code",
            "- If user provides a condition threshold (e.g. 'above 28 degrees'), encode it in the filter agent code",
            "- Dynamic agent code must be a single string with actual \\n newlines (not literal backslash-n)",
            "- TOPIC-BASED WIRING: if LIVE DATA FLOWS shows an agent already publishing relevant data,",
            "  subscribe to that topic instead of spawning a duplicate agent.",
            "  Example: if 'person-detector' publishes 'rpi-kitchen/camera/detections',",
            "  a notification agent should subscribe to that topic, not spawn its own camera agent.",
            "- Use agent.declare_contract() in setup() to declare what topics an agent publishes/subscribes.",
            "  This makes the agent discoverable for future auto-wiring.",
            "- Use agent.window(topic, seconds=N) for temporal reasoning:",
            "  'if motion detected 3+ times in 5 minutes' → agent.window('motion/events', seconds=300).event_count() >= 3",
            "- Use agent.read_world_state(topic) to read retained shared state without subscribing.",
            "- Use agent.publish_world_state(key, data) to share state that other agents can read.",
            "",
            "═══ LIVE DATA FLOWS (topic contracts) ═══",
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
            plan = json.loads(self._extract_json_array(response))
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
        """
        Scan generated dynamic agent code for common LLM mistakes and fix them.
        Currently catches:
          - Raw aiomqtt.Client() usage (should use agent.subscribe() instead)
          - Hardcoded MQTT broker hostnames
          - `await` on synchronous agent API methods (subscribe, window, persist, etc.)
        Logs warnings so the user knows what was fixed.
        """
        import re as _re

        # Synchronous agent API methods that must NOT be awaited
        _SYNC_METHODS = (
            "subscribe",
            "window",
            "persist",
            "recall",
            "declare_contract",
            "agents",
            "nodes",
            "topics",
            "capabilities",
            "increment_processed",
            "increment_errors",
        )
        _sync_pat = r"\bawait\s+(agent\.(?:" + "|".join(_SYNC_METHODS) + r")\s*\()"

        for step in plan:
            sc = step.get("spawn_config", {})
            if sc.get("type") != "dynamic":
                continue
            code = sc.get("code", "")
            if not code:
                continue

            issues = []

            # Strip `await` on sync agent methods
            fixed_code, n_subs = _re.subn(_sync_pat, r"\1", code)
            if n_subs:
                issues.append(f"removed {n_subs} spurious await(s) on sync agent methods")
                sc["code"] = fixed_code
                code = fixed_code

            # Detect raw aiomqtt.Client() — LLM should use agent.subscribe()
            if "aiomqtt.Client(" in code or "aiomqtt.connect(" in code:
                issues.append("raw aiomqtt.Client() — should use agent.subscribe()")
                # Attempt to rewrite: extract topic and replace entire aiomqtt block
                # with agent.subscribe() pattern
                topics = _re.findall(r'await\s+client\.subscribe\(["\']([^"\']+)["\']', code)
                if topics:
                    topic = topics[0]
                    # Build replacement code using agent.subscribe()
                    fixed = self._rewrite_aiomqtt_to_subscribe(code, topic)
                    if fixed:
                        sc["code"] = fixed
                        code = fixed
                        logger.info(
                            f"[{self.name}] Auto-fixed raw aiomqtt in '{step.get('name')}' → agent.subscribe('{topic}')"
                        )

            # Detect direct HA REST API calls — should use ha_actuator instead
            _ha_api_patterns = [
                r"/api/services/",
                r"/api/states/",
                r"httpx.*api/services",
                r"requests\.(post|put|get).*api/services",
                r"aiohttp.*api/services",
            ]
            for pat in _ha_api_patterns:
                if _re.search(pat, code):
                    issues.append(
                        f"DIRECT HA API CALL detected ('{pat[:30]}...') — "
                        f"should use ha_actuator agent instead"
                    )
                    logger.warning(
                        f"[{self.name}] '{step.get('name')}' calls HA API directly! "
                        f"This will likely fail. Should use ha_actuator pattern: "
                        f"dynamic agent publishes trigger → ha_actuator executes HA service call."
                    )
                    break

            if issues:
                logger.warning(
                    f"[{self.name}] Code issues in '{step.get('name')}': {'; '.join(issues)}"
                )

        return plan

    @staticmethod
    def _rewrite_aiomqtt_to_subscribe(code: str, topic: str) -> str:
        """
        Best-effort rewrite of raw aiomqtt MQTT subscription code to use agent.subscribe().
        Extracts the message handling callback and rewires it.
        Returns empty string if rewrite fails (original code kept).
        """
        import re as _re

        # Try to extract the callback body — look for the inner async for loop body
        # Pattern: async for msg/message in client.messages: ... payload handling ...
        match = _re.search(
            r"async\s+for\s+\w+\s+in\s+client\.messages:\s*\n(.*?)(?=\n\s*except|\n\s*$)",
            code,
            _re.DOTALL,
        )
        if not match:
            return ""

        callback_body = match.group(1)

        # Detect how payload is parsed — json.loads(msg.payload) or similar
        payload_parse = ""
        if "json.loads" in callback_body:
            payload_parse = "    # payload is already a dict (parsed by agent.subscribe)\n"

        # Strip leading indentation from callback body
        lines = callback_body.splitlines()
        min_indent = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=4)
        dedented = "\n".join("    " + ln[min_indent:] for ln in lines if ln.strip())

        # Extract any setup code before the aiomqtt block
        pre_match = _re.split(r"async\s+with\s+aiomqtt\.Client", code)[0]
        pre_lines = [
            ln
            for ln in pre_match.splitlines()
            if ln.strip()
            and not ln.strip().startswith("import aiomqtt")
            and not ln.strip().startswith("async def setup")
        ]
        pre_code = (
            "\n".join("    " + ln.strip() for ln in pre_lines if ln.strip()) + "\n"
            if pre_lines
            else ""
        )

        rewritten = (
            f"async def setup(agent):\n"
            f"{pre_code}"
            f"    async def _on_message(payload):\n"
            f"{payload_parse}"
            f"{dedented}\n"
            f"    agent.subscribe('{topic}', _on_message)\n"
            f"    await agent.log('Subscribed to {topic}')\n"
        )

        # Preserve any process() or handle_task() that existed
        import re as _re2

        for fn in ("process", "handle_task"):
            fn_match = _re2.search(rf"async\s+def\s+{fn}\s*\(", code)
            if fn_match:
                rewritten += "\n" + code[fn_match.start() :]
                break

        return rewritten

    # ── Plan cache ─────────────────────────────────────────────────────────

    def _load_cached_plan(self, cache_key: str, workers: list[dict]) -> list | None:
        """Load a cached plan if it exists, is fresh, and all required agents are alive."""
        raw = self.recall(_PLAN_CACHE_KEY) or {}
        entry = raw.get(cache_key)
        if not entry:
            return None

        # TTL check
        age = time.time() - entry.get("timestamp", 0)
        if age > _CACHE_TTL_S:
            logger.info(f"[{self.name}] Cache expired ({age / 3600:.1f}h old)")
            return None

        plan = entry.get("plan", [])
        if not plan:
            return None

        # Validate all agents in the plan are still running
        alive = {w["name"] for w in workers} | {"main", self.name}
        for step in plan:
            agent = step.get("agent", "")
            if agent not in alive and not step.get("spawn_config"):
                logger.info(f"[{self.name}] Cache invalid — agent '{agent}' no longer running")
                return None

        return plan

    def _save_plan_cache(self, cache_key: str, task: str, plan: list):
        """Persist the plan so future similar tasks can reuse it."""
        raw = self.recall(_PLAN_CACHE_KEY) or {}
        # Evict entries older than TTL
        now = time.time()
        raw = {k: v for k, v in raw.items() if now - v.get("timestamp", 0) < _CACHE_TTL_S}
        raw[cache_key] = {
            "task": task[:200],
            "plan": plan,
            "timestamp": now,
        }
        self.persist(_PLAN_CACHE_KEY, raw)

    # ── Worker discovery ───────────────────────────────────────────────────

    def _discover_workers(self) -> list[dict]:
        if not self._registry:
            return []
        # Pull full manifests from main's capability registry (includes schemas)
        main = self._registry.find_by_name("main")
        manifest_map: dict = {}
        if main and hasattr(main, "list_capabilities"):
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
        if main and hasattr(main, "_known_nodes"):
            import time as _dt

            for node_name, nd in main._known_nodes.items():
                if _dt.time() - nd.get("last_seen", 0) > 30:
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

    @staticmethod
    def _extract_json_array(response: str) -> str:
        """Pull a JSON array out of an LLM response that may be fenced or padded
        with prose. Strips a leading ``` / ```json fence and any trailing fence,
        then slices the outermost [...] so stray commentary on either side does
        not break json.loads(). Returns '' if no array delimiters are found.

        Shared by both decompose paths so they parse identically.
        """
        clean = (response or "").strip()
        # Drop an opening fence line (``` or ```json) if present.
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else ""
        # Drop everything from a trailing fence onward.
        if "```" in clean:
            clean = clean[: clean.rfind("```")]
        # Slice to the outermost array.
        start = clean.find("[")
        end = clean.rfind("]")
        if start != -1 and end != -1 and end > start:
            clean = clean[start : end + 1]
        return clean.strip()

    @staticmethod
    def _extract_json_object(response: str) -> str:
        """Like _extract_json_array but slices the outermost {...} object."""
        clean = (response or "").strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else ""
        if "```" in clean:
            clean = clean[: clean.rfind("```")]
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            clean = clean[start : end + 1]
        return clean.strip()

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
            from ..core.topic_bus import get_topic_bus

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

        prompt = f"""You are a task planner for a multi-agent system.
Break the task into steps. Each step is handled by one agent.

AVAILABLE AGENTS (with input/output contracts):
{workers_desc}
{topic_schema_ctx}
TASK: {task}

OUTPUT RULES:
- Respond ONLY with a valid JSON array. No explanation, no markdown.
- Each step object:
  {{
    "step": <int>,
    "agent": "<agent-name>",
    "task": "<what to ask this agent>",
    "parallel": <true|false>,
    "depends_on": [<step ints>],
    "spawn_config": <null or spawn object if agent needs to be created>
  }}
- "parallel": true if this step can run concurrently with other parallel steps
- "depends_on": step numbers whose results this step needs (empty list if none)
- "spawn_config": if the ideal agent for a step does NOT exist in the available list,
  include a spawn config to create it.
  AGENT TYPE RULES:
    Use "llm" ONLY for pure conversation/Q&A/explanation agents (no external APIs or tools).
    Use "dynamic" for anything that fetches data, calls APIs, runs searches, or uses libraries.

    CRITICAL — sync vs async agent API methods:
      SYNCHRONOUS (NO await):
        agent.subscribe(topic, callback)  — fire-and-forget background task
        agent.window(topic, seconds=N)    — returns StreamWindow immediately
        agent.persist(key, val)           — save to disk
        agent.recall(key)                 — load from disk
        agent.declare_contract(...)       — register topic contract
        agent.agents()                    — list running agents
        agent.topics(keyword)             — list known topics
      ASYNC (MUST await):
        await agent.publish(topic, data)  — publish to MQTT
        await agent.log(msg)              — log a message
        await agent.alert(msg)            — trigger alert
        await agent.send_to(name, payload)— delegate to another agent
        await agent.mqtt_get(topic)       — one-shot MQTT read

    NEVER use agent.logger — it does not exist. Use await agent.log(msg) instead.

    CRITICAL — HOME ASSISTANT ACTIONS:
      NEVER call HA REST API directly from dynamic agent code (no httpx/requests to /api/services/).
      For ANY HA device action (turn on/off lights, switches, climate, etc.):
        Use "type": "ha_actuator" — NOT a dynamic agent with httpx.
        If a condition must be checked first, use TWO agents:
          1. Dynamic agent checks condition → publishes trigger to custom/triggers/<slug>
          2. ha_actuator agent subscribes to trigger → executes HA service call
      ha_actuator spawn_config example:
      {{
        "name": "lamp-off-actuator",
        "type": "ha_actuator",
        "description": "Turns off the lamp when triggered",
        "mqtt_topics": ["custom/triggers/lamp-temp"],
        "detection_filter": {{"triggered": true}},
        "actions": [{{"domain": "light", "service": "turn_off", "entity_id": "light.wiz_rgbw_tunable_02cba0"}}]
      }}
  LLM agent example:
  {{
    "name": "translator-agent",
    "type": "llm",
    "system_prompt": "You are an expert translator. Translate text accurately."
  }}
  Dynamic agent example (for weather, news, search, APIs):
  {{
    "name": "weather-agent",
    "type": "dynamic",
    "description": "Fetches live weather data for a city",
    "input_schema":  {{"city": "str — city name to fetch weather for"}},
    "output_schema": {{"city": "str", "temp_c": "str", "description": "str"}},
    "poll_interval": 3600,
    "code": "async def setup(agent):\n    await agent.log('ready')\nasync def process(agent):\n    import asyncio\n    await asyncio.sleep(3600)\nasync def handle_task(agent, payload):\n    import httpx\n    city = payload.get('city', 'Athens')\n    async with httpx.AsyncClient(timeout=10) as c:\n        r = await c.get(f'https://wttr.in/{{city}}?format=j1')\n        d = r.json()\n    cur = d['current_condition'][0]\n    return {{'city': city, 'temp_c': cur['temp_C'], 'description': cur['weatherDesc'][0]['value']}}"
  }}
- The FINAL synthesis step should ALWAYS be assigned to "main" (not any other agent).
  Main will combine results using its LLM. Never assign synthesis to a domain agent.
- Only create new agents when TRULY necessary — prefer existing agents.
- If one agent can handle everything, output a single-step plan.
- Keep it minimal — avoid unnecessary steps.
- IMPORTANT: For any step that combines, summarizes, synthesizes or compares results
  from other steps, ALWAYS use "agent": "main" — never a domain agent.
- Domain agents (weather, news, manual, etc.) are for DATA RETRIEVAL only.
  "main" handles all reasoning, summarization and synthesis.

Example:
[
  {{"step": 1, "agent": "weather-agent", "task": "Get weather in Athens", "parallel": true, "depends_on": [], "spawn_config": null}},
  {{"step": 2, "agent": "news-agent", "task": "Get AI news today", "parallel": true, "depends_on": [], "spawn_config": null}},
  {{"step": 3, "agent": "main", "task": "Summarize the weather and news results", "parallel": false, "depends_on": [1, 2], "spawn_config": null}}
]"""

        try:
            response, _usage = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system=self._now_context()
                + "\nYou are a JSON-only task planner. Output only valid JSON arrays, nothing else.",
                max_tokens=1500,
            )
            self._accrue_usage(_usage)
            plan = json.loads(self._extract_json_array(response))
            if isinstance(plan, list) and plan:
                return plan
        except Exception as e:
            logger.error(f"[{self.name}] Decomposition error: {e}")
        return []

    # ── Missing agent spawning ─────────────────────────────────────────────

    async def _ensure_agents(self, plan: list[dict]) -> list[dict]:
        """
        For any step with a spawn_config, spawn the agent if it's not running.
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

    async def _spawn_agent(self, config: dict) -> Actor | None:
        """Spawn an agent for a plan step. Delegates to the shared SpawnMixin.

        Uses the BLOCKING install path: a pipeline's next step may depend on
        this agent being live, so we wait for any package install to finish
        rather than returning a placeholder.
        """
        return await self._spawn_local_from_config(config, register=True, blocking_install=True)

    # ── Execution ──────────────────────────────────────────────────────────

    async def _execute(self, plan: list[dict]) -> dict:
        results: dict = {}
        completed: set[int] = set()
        remaining: list[dict] = list(plan)

        # ── Validate dependency references up front ────────────────────────
        # A step whose depends_on points at a step number not in the plan can
        # never become ready and would stall the whole batch. Surface it as a
        # failed step (and mark it 'completed' so its dependents can resolve)
        # rather than silently deadlocking.
        valid_ids = {s.get("step") for s in plan}
        for s in list(remaining):
            bad = [d for d in (s.get("depends_on") or []) if d not in valid_ids]
            if bad:
                logger.error(
                    f"[{self.name}] Step {s.get('step')} depends on missing "
                    f"step(s) {bad} — marking failed"
                )
                results[s.get("step")] = {"error": f"unsatisfiable dependency on step(s) {bad}"}
                completed.add(s.get("step"))
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
        """
        After pipeline agents are spawned they sit idle until the next MQTT
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
        import re
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


# ── Utility ────────────────────────────────────────────────────────────────


def _task_hash(task: str) -> str:
    """Stable short hash of a normalized task string for cache keying."""
    normalized = " ".join(task.lower().split())
    return hashlib.md5(normalized.encode()).hexdigest()[:12]
