"""Planning, dry-run, and pipeline execution for MainActor.

Pipeline-rule and pending-plan persistence, plan-proposal formatting, the
dry-run approve / revise / reject flow, planner invocation, and pipeline
execution. Mixed into MainActor; assumes an LLMAgent-derived host plus the
Actor base (self.persist/recall, self.spawn, self.send, self._registry,
self._result_futures).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, ClassVar

from wactorz.llm_factory import provider_for

logger = logging.getLogger(__name__)

#: Persistence key for the rules that shape how a pipeline is planned.
PIPELINE_RULES_KEY = "_pipeline_rules"

#: Persistence key for dry-run proposals awaiting user approval.
#:
#: Public because the `/plans` command reads the same store: a plan proposed
#: here is listed and cleared from there, so the two must name one key.
PENDING_PLANS_KEY = "_pending_plans"

#: Prefixes that skip dry-run and approval for PIPELINE intent.
#:
#: One tuple because three places act on them — the gate below deciding whether
#: to hold a request for approval, the stripper that keeps the marker out of the
#: task text, and the restricted channel refusing the admin surface. A marker
#: added to one of those and missed by another is a guard that catches some
#: spellings of the same thing. Add a marker here, not at a call site.
BYPASS_MARKERS = ("pipeline!", "coordinate!", "@planner!")


def starts_with_bypass(text: str) -> bool:
    """Whether `text` opens with a bypass marker, whatever its case or spacing."""
    return text.lower().lstrip().startswith(BYPASS_MARKERS)


def _strip_dryrun_bypass(text: str) -> str:
    """Strip the bypass marker from the user's text so the planner does not see
    it as part of the task.
    """
    if not text:
        return text
    lowered = text.lower().lstrip()
    for bypass in BYPASS_MARKERS:
        if lowered.startswith(bypass):
            # Find the bypass in the original (case-insensitive) and skip it
            idx = text.lower().find(bypass)
            if idx != -1:
                return text[idx + len(bypass) :].lstrip(" :,-")
    return text


def _parse_plan_envelope(planner_result: str) -> dict | None:
    """Try to parse a planner result string as a plan envelope (the JSON dict
    returned by plan_only mode). Returns the envelope dict if it's a valid
    proposal, or None if the result is a regular answer (e.g. error message,
    feasibility failure, or fallback prose).
    """
    if not planner_result or not planner_result.strip().startswith("{"):
        return None
    try:
        envelope = json.loads(planner_result)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(envelope, dict) and envelope.get("_plan_proposal") is True:
        return envelope
    return None


#: Head-room over the planner's lifetime cap for a reply produced just before it
#: to reach us. Not time for the planner to keep working — it has already gone.
PLANNER_REPLY_GRACE_S = 15.0


if TYPE_CHECKING:
    from ..mixins.host import PlanningHost

    # Typing-only base: states what the host must provide, and is
    # gone at runtime so the real MRO is exactly what it was.
    _Host = PlanningHost
else:
    _Host = object


class PlanningMixin(_Host):
    """Plans, dry-run flow, and pipeline execution. Mix into an LLMAgent host."""

    def get_pipeline_rules(self) -> dict:
        return self.recall(PIPELINE_RULES_KEY) or {}

    def save_pipeline_rule(self, rule: dict):
        rules = self.get_pipeline_rules()
        rules[rule["rule_id"]] = rule
        self.persist(PIPELINE_RULES_KEY, rules)
        logger.info(
            f"[{self.name}] Pipeline rule saved: {rule['rule_id']} agents={rule.get('agents', [])}"
        )

    # ── Pending-plan registry (dry-run / approval flow) ────────────────────
    # When PIPELINE intent fires, the planner runs in plan_only mode and returns
    # a proposal instead of spawning. We store the proposal here, show it to the
    # user, and wait for approval before executing. Persisted so a restart in
    # the middle of an approval flow doesn't lose the user's pending plans.
    #
    # Schema: { plan_id: {
    #     "plan_id":    str,
    #     "task":       str,            # original user request
    #     "created_at": float,
    #     "status":     "pending"|"approved"|"rejected"|"superseded"|"expired",
    #     "envelope":   dict,           # the full plan envelope from the planner
    # } }
    PLAN_TTL_S = 24 * 3600  # auto-expire pending plans after 24h

    def get_pending_plans(self) -> dict:
        plans = self.recall(PENDING_PLANS_KEY) or {}
        # Expire stale entries on every read so we don't have to gc separately
        import time as _t

        now = _t.time()
        expired_ids = [
            pid
            for pid, p in plans.items()
            if p.get("status") == "pending" and (now - p.get("created_at", now)) > self.PLAN_TTL_S
        ]
        if expired_ids:
            for pid in expired_ids:
                plans[pid]["status"] = "expired"
            self.persist(PENDING_PLANS_KEY, plans)
        return plans

    def save_pending_plan(self, plan: dict):
        plans = self.recall(PENDING_PLANS_KEY) or {}
        plans[plan["plan_id"]] = plan
        self.persist(PENDING_PLANS_KEY, plans)

    def update_plan_status(self, plan_id: str, status: str):
        plans = self.recall(PENDING_PLANS_KEY) or {}
        if plan_id in plans:
            plans[plan_id]["status"] = status
            self.persist(PENDING_PLANS_KEY, plans)

    def _most_recent_pending_plan(self) -> dict | None:
        """Returns the most-recently-created plan still in 'pending' status, or None."""
        plans = self.get_pending_plans()
        pending = [p for p in plans.values() if p.get("status") == "pending"]
        if not pending:
            return None
        return max(pending, key=lambda p: p.get("created_at", 0))

    def _format_plan_proposal(self, plan: dict) -> str:
        """Render a pending plan as a human-readable summary for the user.

        Goals (in priority order):
          1. Show what the rule WILL DO in plain English (most important).
          2. Show which inputs / topics it listens to (so user can spot
             "did you really mean THIS sensor?").
          3. Show side effects: notifications sent, devices controlled, files
             written. Anything that affects the world.
          4. Hide raw code by default — link to expansion via /plans show <id>.
          5. Make the approval actions obvious.
        """
        envelope = plan.get("envelope", {})
        agents = envelope.get("plan", []) or envelope.get("agents", [])
        task = plan.get("task", envelope.get("task", "?"))
        plan_id = plan.get("plan_id", "?")

        lines = []
        lines.append(f"**Proposed pipeline** (id `{plan_id}`)")
        lines.append(f"For: _{task}_")
        lines.append("")
        lines.append(f"This will create {len(agents)} agent(s):")

        # Per-agent summary
        for i, step in enumerate(agents, 1):
            name = step.get("name", "?")
            desc = step.get("description") or step.get("spawn_config", {}).get("description", "")
            spawn_cfg = step.get("spawn_config", {})
            agent_type = spawn_cfg.get("type", "dynamic")
            install = spawn_cfg.get("install", []) or []

            lines.append(f"\n  {i}. **{name}** ({agent_type})")
            if desc:
                lines.append(f"     purpose: {desc}")

            # For scheduled agents: render the schedule prominently — this is
            # the field the user most needs to verify (did the LLM correctly
            # interpret "5pm every weekday"?).
            if agent_type == "scheduled":
                schedule_spec = spawn_cfg.get("schedule") or {}
                if isinstance(schedule_spec, dict) and schedule_spec:
                    try:
                        from ..scheduled_agent import describe_schedule

                        tz_name = schedule_spec.get("tz") or self.get_user_facts().get(
                            "pref_timezone"
                        )
                        lines.append(f"     fires: {describe_schedule(schedule_spec, tz_name)}")
                    except Exception:
                        lines.append(f"     fires: {schedule_spec}")
                topic = spawn_cfg.get("publish_topic") or f"schedule/{name}/fired"
                lines.append(f"     publishes: {topic}")

            # Inputs — what it listens to
            subs = step.get("subscribes", []) or spawn_cfg.get("subscribe", []) or []
            if subs:
                lines.append(f"     listens on: {', '.join(subs)}")

            # Outputs — what it publishes
            pubs = step.get("publishes", []) or spawn_cfg.get("publish", []) or []
            if pubs and agent_type != "scheduled":  # already shown above for scheduled
                lines.append(f"     publishes: {', '.join(pubs)}")

            # External side effects — webhooks/notifications/HA actions
            # Best-effort heuristic from the code or spawn_cfg fields
            side_effects = []
            code = spawn_cfg.get("code", "") or ""
            if "webhook" in code.lower() or "notification" in code.lower():
                side_effects.append("sends notification")
            if "discord.com/api/webhooks" in code:
                side_effects.append("posts to Discord")
            if "api.telegram.org" in code:
                side_effects.append("posts to Telegram")
            if "homeassistant" in code.lower() and (
                "turn_on" in code or "turn_off" in code or "call_service" in code
            ):
                side_effects.append("controls Home Assistant device")
            if agent_type == "ha_actuator":
                target = spawn_cfg.get("entity_id") or spawn_cfg.get("target", "?")
                action = spawn_cfg.get("service") or spawn_cfg.get("action", "?")
                side_effects.append(f"calls HA: {action} on {target}")
            if side_effects:
                lines.append(f"     side effects: {'; '.join(side_effects)}")

            # Install requirements — surfaced because user pays the cost
            if install:
                pkgs = ", ".join(install if isinstance(install, list) else [install])
                lines.append(f"     installs: {pkgs}")

        lines.append("")

        # Advisory: duplicate / contradicting active rules (from the planner's
        # semantic check). Shown prominently just above the approval actions so
        # the user sees it before deciding. Advisory only — never blocks.
        warnings = envelope.get("warnings") or ""
        if warnings.strip():
            lines.append("⚠️ **Heads up — possible overlap with existing rules**")
            lines.append(warnings)
            lines.append("")

        lines.append("**To proceed:**")
        lines.append("  Reply **yes** (or **approve**) to spawn the agents above.")
        lines.append("  Reply **no** (or **reject**) to discard this plan.")
        lines.append(
            "  Reply with a correction (e.g. _'use the bedroom sensor instead'_) to revise."
        )
        lines.append(f"  Or run `/plans show {plan_id}` to see the full code.")

        return "\n".join(lines)

    async def delete_pipeline_rule(self, rule_id: str) -> str:
        """Stop all agents for a rule and remove it from registry."""
        rules = self.get_pipeline_rules()
        rule = rules.get(rule_id)
        if not rule:
            return f"No rule found with id '{rule_id}'."
        agents = rule.get("agents", [])
        stopped = []
        for agent_name in agents:
            self._remove_from_spawn_registry(agent_name)
            if self._registry:
                actor = self._registry.find_by_name(agent_name)
                if actor:
                    actor_id = actor.actor_id
                    await actor.stop()
                    await self._registry.unregister(actor_id)
                    await self._clear_agent_manifest(agent_name, actor_id)
                    self._record_agent_deletion(
                        agent_name, reason=f"pipeline rule '{rule_id}' deleted"
                    )
                    stopped.append(agent_name)
        del rules[rule_id]
        self.persist(PIPELINE_RULES_KEY, rules)
        task_preview = rule.get("task", "")[:60]
        return f"Rule '{rule_id}' deleted. Stopped agents: {', '.join(stopped) or 'none running'}.\nRule was: {task_preview}"

    _PLANNING_KEYWORDS: ClassVar[list[str]] = [
        # Coordination signals
        "and then",
        "after that",
        "also",
        "combine",
        "compare",
        "coordinate",
        "plan",
        "pipeline",
        "orchestrate",
        "summarize both",
        "using multiple",
        "all agents",
        "several agents",
        # Multi-step / multi-domain signals
        "first.*then",
        "step by step",
        "in order",
        "weather.*news",
        "news.*weather",
        "manual.*code",
        "search.*analyze",
        # Reactive pipeline signals
        "if.*then",
        "when.*send",
        "when.*turn",
        "when.*open",
        "when.*close",
        "whenever",
        "monitor.*and",
        "watch.*and",
        "detect.*and",
        "notify me",
        "alert me",
        "automatically",
    ]

    async def _needs_planning(self, text: str) -> bool:
        """Heuristic: does this task benefit from multi-agent coordination?
        Keeps main fast — only escalates genuinely complex requests.
        """
        import re

        lowered = text.lower()

        # Explicit user request for coordination
        if any(
            w in lowered
            for w in (
                "coordinate:",
                "plan:",
                "pipeline:",
                "@planner",
                "ask the planner",
                "use the planner",
                "create a pipeline",
                "set up a pipeline",
                "create a rule",
                "set up a rule",
            )
        ):
            return True

        # Keyword heuristic — multiple signals needed to avoid false positives
        hits = sum(1 for kw in self._PLANNING_KEYWORDS if re.search(kw, lowered))
        if hits >= 2:
            return True

        # References two or more known agent names
        if self._registry:
            agent_names = [
                a.name
                for a in self._registry.all_actors()
                if a.name not in {"main", "monitor", "installer"}
            ]
            mentioned = sum(1 for name in agent_names if name in lowered)
            if mentioned >= 2:
                return True

        return False

    async def _run_planner(
        self,
        task: str,
        is_pipeline_intent: bool = False,
        plan_only: bool = False,
        approved_plan: dict | None = None,
    ) -> str | None:
        """Spawn a PlannerAgent, hand it the task, wait for the result.

        is_pipeline_intent: when True, the caller has classified this as a
        reactive-rule task ("if X then Y", "wherever Z happens..."). For these,
        we DELIBERATELY skip the conversation-history enrichment because:
          - Pipelines are imperative declarations, not follow-ups.
          - Including unrelated prior turns has been observed to bleed irrelevant
            context into the planner's LLM (e.g. a prior "door open" pipeline
            poisoning a fresh "camera person detection" pipeline, causing the
            planner to generate a door-themed agent name and code).
          - Pronoun resolution — the main reason enrichment exists — rarely
            applies to pipeline declarations.

        plan_only: when True, the planner builds a plan but does NOT spawn.
        Returns a JSON string containing the plan envelope (use _parse_plan_envelope
        to extract). Used by the dry-run flow.

        approved_plan: when provided, the planner skips planning entirely and
        executes the supplied plan directly. Used after the user approves a
        previously-generated plan.
        """
        from ..planner_agent import PlannerAgent

        # Enrich vague follow-up tasks with recent conversation context
        # so the planner has the full picture (e.g. which entity was found).
        # PIPELINE intent skips this — see docstring.
        # approved_plan also skips: the plan was already built with the right context.
        enriched_task = task
        if (
            not is_pipeline_intent
            and not approved_plan
            and self._conversation_history
            and len(task.split()) < 15
        ):
            # Short/vague task — inject last 3 exchanges as context
            recent = self._conversation_history[-6:]  # 3 user+assistant pairs
            ctx_lines = []
            for m in recent:
                role = "User" if m["role"] == "user" else "Assistant"
                content = str(m["content"])[:300]
                ctx_lines.append(f"{role}: {content}")
            if ctx_lines:
                enriched_task = f"{task}\n\n[Context from recent conversation:]\n" + "\n".join(
                    ctx_lines
                )

        planner_name = f"planner-{uuid.uuid4().hex[:6]}"
        mode = (
            "approved-execute"
            if approved_plan
            else ("plan-only" if plan_only else "plan-and-execute")
        )
        logger.info(
            f"[{self.name}] Spawning planner '{planner_name}' (mode={mode}) for: {enriched_task[:60]}"
        )

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {
                "type": "log",
                "message": f"Complex task detected — spawning planner ({mode})...",
                "timestamp": time.time(),
            },
        )

        task_id = f"plan_{uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._result_futures[task_id] = future

        try:
            # One value feeds both the planner's cap and our wait, so the two
            # cannot drift apart. The planner self-terminates at the cap without
            # answering, so waiting past it is waiting for a reply that can no
            # longer be sent; the grace only covers delivery of one produced
            # just before the cap.
            lifetime_s = PlannerAgent.DEFAULT_MAX_LIFETIME_S
            planner = await self.spawn(
                PlannerAgent,
                name=planner_name,
                llm_provider=provider_for("planner", self.llm),
                task=enriched_task,
                reply_to_id=self.actor_id,
                reply_task_id=task_id,
                auto_terminate=True,
                plan_only=plan_only,
                approved_plan=approved_plan,
                max_lifetime_s=lifetime_s,
                persistence_dir=str(self._persistence_dir.parent),
            )
            if not planner:
                return None

            result_payload = await asyncio.wait_for(
                future, timeout=lifetime_s + PLANNER_REPLY_GRACE_S
            )
            answer = result_payload.get("result") or result_payload.get("text") or ""
            spawned_names = result_payload.get("spawned", [])
            if spawned_names:
                answer += f"\n\n[System: Planner created new agents: {', '.join(spawned_names)} — saved for future use]"
            return answer

        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Planner timed out for: {task[:60]}")
            return "The pipeline is taking longer than expected to set up. Check `/rules` in a moment to see if agents were spawned, or try again."
        except Exception as e:
            logger.error(f"[{self.name}] Planner error: {e}")
            return None
        finally:
            self._result_futures.pop(task_id, None)

    def _dryrun_enabled(self, text: str) -> bool:
        """Decide whether dry-run / approval should gate this PIPELINE request.

        Bypass conditions (always skip approval):
          - Text opens with one of the bypass markers
          - User policy `policy_dryrun` is set to "off" / "false" / "disabled"

        Otherwise dry-run is on by default for PIPELINE intent.
        """
        if not text:
            return True
        if starts_with_bypass(text):
            return False
        # Check user-set policy
        facts = self.get_user_facts()
        for key in ("policy_dryrun", "policy_dry_run", "policy_approval"):
            v = str(facts.get(key, "")).strip().lower()
            if v in ("off", "false", "disabled", "no", "skip"):
                return False
        return True

    async def _propose_or_execute_pipeline(self, text: str) -> str:
        """Top-level entry for PIPELINE intent. Decides between dry-run (build
        plan, ask for approval, store proposal) and immediate execution
        (bypass marker or policy-disabled). Returns the user-facing response.
        """
        if not self._dryrun_enabled(text):
            # Bypass — execute immediately as before
            cleaned = _strip_dryrun_bypass(text)
            result = await self._run_planner(cleaned, is_pipeline_intent=True)
            return result or "Planner did not return a result. Please retry."

        # Dry-run path: get a plan, don't execute
        planner_result = await self._run_planner(text, is_pipeline_intent=True, plan_only=True)
        if not planner_result:
            return "Planner did not return a result. Please retry."

        envelope = _parse_plan_envelope(planner_result)
        if not envelope:
            # Planner returned a regular answer (error, feasibility failure,
            # or fallback prose). Pass it through unchanged.
            return planner_result

        # Store the proposal
        import time as _t
        import uuid as _uuid

        plan_id = _uuid.uuid4().hex[:8]
        proposal = {
            "plan_id": plan_id,
            "task": text,
            "created_at": _t.time(),
            "status": "pending",
            "envelope": envelope,
        }
        self.save_pending_plan(proposal)
        return self._format_plan_proposal(proposal)

    # Approval/rejection vocabulary — exact-match-only after my v2 fix.
    # The previous version used `cleaned.startswith(w + " ")` which silently
    # treated any sentence beginning with "ok " as approval — including
    # corrections like "ok lets go for 55 as a threshold". The new logic
    # requires the message to be SHORT enough that it can only be approval
    # or rejection. See _looks_like_approval / _looks_like_rejection.
    _APPROVE_PHRASES: ClassVar[set[str]] = {
        "yes",
        "y",
        "yep",
        "yeah",
        "yup",
        "ya",
        "ok",
        "okay",
        "k",
        "kk",
        "sure",
        "fine",
        "alright",
        "go",
        "go ahead",
        "do it",
        "send it",
        "ship it",
        "approve",
        "approved",
        "approved!",
        "proceed",
        "confirm",
        "confirmed",
        "spawn it",
        "create it",
        "make it",
    }
    _APPROVE_EMPHASIS: ClassVar[set[str]] = {
        "please",
        "now",
        "go",
        "do it",
        "thanks",
        "thx",
        "ahead",
        "confirm",
        "confirmed",
        "approved",
        "ok",
        "yes",
        "good",
    }
    _REJECT_PHRASES: ClassVar[set[str]] = {
        "no",
        "n",
        "nope",
        "nah",
        "reject",
        "rejected",
        "cancel",
        "skip",
        "discard",
        "drop it",
        "abort",
        "stop",
        "stop it",
        "nevermind",
        "never mind",
        "forget it",
    }

    @classmethod
    def _looks_like_approval(cls, cleaned: str) -> bool:
        """Strict approval detection. Only fires when the message is short
        enough that it cannot also be a correction or a new request.
        """
        if cleaned in cls._APPROVE_PHRASES:
            return True
        # Allow up to a 3-token expansion where every extra token is itself
        # approval-flavored, so "yes please" / "ok do it" / "go ahead now" all
        # match — but "ok lets go for 55 as a threshold" does NOT.
        tokens = cleaned.split()
        if len(tokens) <= 3 and tokens and tokens[0] in cls._APPROVE_PHRASES:
            tail = " ".join(tokens[1:])
            if not tail:
                return True
            # Tail must be entirely emphasis/approval words (or a recognized
            # multi-word approval phrase joined back together).
            if tail in cls._APPROVE_PHRASES or tail in cls._APPROVE_EMPHASIS:
                return True
            if all(t in cls._APPROVE_EMPHASIS or t in cls._APPROVE_PHRASES for t in tokens[1:]):
                return True
        return False

    @classmethod
    def _looks_like_rejection(cls, cleaned: str) -> bool:
        """Strict rejection detection — same shape as approval."""
        if cleaned in cls._REJECT_PHRASES:
            return True
        tokens = cleaned.split()
        # "no thanks", "cancel that", "stop please" — all clearly negative
        return len(tokens) <= 3 and bool(tokens) and tokens[0] in cls._REJECT_PHRASES

    # Correction-intent signals: words that suggest the user is adjusting
    # the pending plan rather than confirming or starting fresh. Used only
    # when a plan is pending — outside that context these words are noise.
    _CORRECTION_HINTS = (
        "actually",
        "instead",
        "rather",
        "let's",
        "lets",
        "make it",
        "change",
        "change it",
        "use ",
        "set it to",
        "set the",
        "should be",
        "needs to be",
        "make that",
        "but ",
        " threshold",
        " interval",
        " every ",
        " seconds",
        " minutes",
        " hours",
        " minutes",
        " degrees",
        "%",
        "celsius",
        "fahrenheit",
        "increase",
        "decrease",
        "raise",
        "lower",
        "higher",
        "lower",
    )

    @classmethod
    def _looks_like_correction(cls, text: str) -> bool:
        """Heuristic: does this message look like an adjustment to a pending
        plan rather than a fresh request? Pure heuristic — false positives
        get a confirm-or-new prompt, false negatives fall through to OTHER
        intent (mildly annoying but not destructive).
        """
        lowered = text.lower()
        # Numbers + units strongly suggest correction ("change to 55%", "every 30s")
        import re

        if re.search(
            r"\b\d+(\.\d+)?\s*(%|c|°|sec|secs|seconds|min|mins|minutes|hour|hours|hr|hrs)\b",
            lowered,
        ):
            return True
        if re.search(r"\b(threshold|interval|frequency|rate|delay)\b", lowered):
            return True
        return any(h in lowered for h in cls._CORRECTION_HINTS)

    async def _handle_pending_plan_response(self, text: str) -> str | None:
        """If there is a pending plan and the user's message looks like a
        response to it (yes/no/correction), handle it and return the result.
        Returns None if there's no pending plan or the message clearly
        isn't a response — the message then flows through normal processing.

        Decision tree (in priority order):
          1. Strict approval match → execute the plan.
          2. Strict rejection match → reject and discard.
          3. Looks-like-correction (numbers/units, "let's", "instead", etc.)
             → revise the pending plan with this feedback.
          4. Anything else → return None, message processed normally
             (collision guard catches spawn-intent later).
        """
        pending = self._most_recent_pending_plan()
        if not pending:
            return None

        cleaned = text.strip().lower().rstrip(".,!?")
        if not cleaned:
            return None

        if self._looks_like_approval(cleaned):
            return await self._execute_pending_plan(pending)
        if self._looks_like_rejection(cleaned):
            return self._reject_pending_plan(pending)
        if self._looks_like_correction(text):
            return await self._revise_pending_plan(pending, text)

        # Not approval, rejection, or correction — leave the plan pending,
        # let the message flow through normal handling. The collision guard
        # downstream will catch spawn-intent messages and ask the user to
        # resolve the pending plan first.
        return None

    async def _revise_pending_plan(self, proposal: dict, correction: str) -> str:
        """The user typed something that looks like an adjustment to a pending
        plan ("let's use 55 instead", "change the interval to 30 seconds").
        Mark the old plan superseded, re-run the planner with the original
        task plus the correction as feedback, and present the new proposal.
        """
        old_id = proposal["plan_id"]
        original_task = proposal["task"]
        revised_task = (
            f"{original_task}\n\n[User correction to the previous plan: {correction.strip()}]"
        )
        logger.info(f"[{self.name}] Revising plan {old_id} with correction: {correction[:80]!r}")
        self.update_plan_status(old_id, "superseded")

        # Re-run planner in plan_only mode with the enriched task
        planner_result = await self._run_planner(
            revised_task, is_pipeline_intent=True, plan_only=True
        )
        if not planner_result:
            return f"Could not revise plan `{old_id}`. The planner did not respond — please retry."

        envelope = _parse_plan_envelope(planner_result)
        if not envelope:
            # Planner returned a regular answer — pass it through
            return planner_result

        import time as _t
        import uuid as _uuid

        new_id = _uuid.uuid4().hex[:8]
        new_proposal = {
            "plan_id": new_id,
            "task": original_task,  # keep original; correction lives in envelope
            "created_at": _t.time(),
            "status": "pending",
            "envelope": envelope,
            "supersedes": old_id,
        }
        self.save_pending_plan(new_proposal)
        formatted = self._format_plan_proposal(new_proposal)
        return (
            f"📝 Got it — revising plan `{old_id}` based on your feedback.\n"
            f"Plan `{old_id}` is now superseded by `{new_id}`:\n\n"
            f"{formatted}"
        )

    async def _execute_pending_plan(self, proposal: dict) -> str:
        """Execute an approved plan by calling the planner with approved_plan set."""
        plan_id = proposal["plan_id"]
        envelope = proposal["envelope"]
        original_task = proposal["task"]
        logger.info(f"[{self.name}] Executing approved plan {plan_id}")
        self.update_plan_status(plan_id, "approved")

        result = await self._run_planner(
            original_task,
            is_pipeline_intent=True,
            approved_plan=envelope,
        )
        return f"✅ Approved plan `{plan_id}`. {result or 'Spawn complete.'}"

    def _reject_pending_plan(self, proposal: dict) -> str:
        plan_id = proposal["plan_id"]
        self.update_plan_status(plan_id, "rejected")
        logger.info(f"[{self.name}] Rejected plan {plan_id}")
        return (
            f"❌ Discarded plan `{plan_id}`. No agents were spawned.\n"
            f"If you'd like to try again with different wording, just ask."
        )

    # Heuristic words that suggest the user wants to spawn / create / build
    # something. Cheap pre-LLM check used by the collision guard. False
    # positives are tolerable because the worst case is an unnecessary
    # warning (one extra message); false NEGATIVES are what we're avoiding
    # because they cause silent duplicate spawns.
    _SPAWN_INTENT_WORDS = (
        "spawn",
        "create",
        "build",
        "make a",
        "add a",
        "set up a",
        "set up an",
        "start a",
        "start an",
        "deploy",
        "launch",
        "i want a",
        "i need a",
        "generate",
        "produce",
        "watch for",
        "monitor",
        "alert me",
        "if ",
        "when ",
        "whenever ",
        "trigger ",
        "schedule",
        "every ",
        "each ",
    )

    def _warn_if_pending_plan_collision(self, text: str) -> str | None:
        """If a plan is already pending and the user types something that looks
        like another spawn / pipeline request, return a warning message and
        do NOT process the request. This stops the silent-duplicate scenario:
          1. user types pipeline request → plan A pending
          2. user types ANOTHER pipeline request → would spawn agents X, Y
          3. user later approves plan A → spawns plan A's agents too
          4. user is now stuck with both, didn't intend either configuration

        The user must resolve the pending plan first (yes/no/cancel) or
        explicitly mark this new request to bypass.

        Returns None if there's no collision and processing should continue.
        """
        pending = self._most_recent_pending_plan()
        if not pending:
            return None
        # Bypass marker means "I know what I'm doing, just do it"
        if not self._dryrun_enabled(text):
            return None
        lowered = text.lower().strip()
        if not any(w in lowered for w in self._SPAWN_INTENT_WORDS):
            return None
        # Spawn-like request while a plan is pending — warn
        pid = pending["plan_id"]
        ptask = pending.get("task", "?")[:80]
        return (
            f"⚠️  You have a pending plan `{pid}` for: _{ptask}_\n\n"
            f"Your new message looks like another spawn or rule request, which would\n"
            f"create separate agents that may conflict with the pending plan once approved.\n\n"
            f"Please resolve the pending plan first:\n"
            f"  • Reply **yes** to approve and spawn it, then send your new request.\n"
            f"  • Reply **no** to discard it, then send your new request.\n"
            f"  • Or send `/plans show {pid}` to inspect, then `/plans approve {pid}` "
            f"or `/plans reject {pid}`.\n\n"
            f"To bypass this check for one-off requests, prefix with `pipeline!` "
            f"(e.g. `pipeline! {text[:40]}...`)."
        )
