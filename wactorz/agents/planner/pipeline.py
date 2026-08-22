"""The reactive-pipeline path: standing rules that outlive the request.

A pipeline request produces persistent agents rather than a one-shot answer, so
this path gathers the live context an automation needs -- Home Assistant
entities, camera URLs, topic samples, notification targets -- checks the new
rule against the ones already active, and spawns what the plan asks for.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, NamedTuple

from ...core.actor import MessageType
from ..lookup import find_main_actor
from ..prompts.planner_prompts import (
    HA_FEASIBILITY_PROMPT,
    PIPELINE_DESIGN_PROMPT,
    RULE_CONFLICT_PROMPT,
)
from .parsing import extract_json_array, extract_json_object

if TYPE_CHECKING:
    from .hosts import PipelineHost

    # Typing-only base: it states what the host must provide and is gone
    # at runtime, so the real MRO is exactly what it was.
    _Host = PipelineHost
else:
    _Host = object

logger = logging.getLogger(__name__)


class PipelineMixin(_Host):
    """The reactive-pipeline path. Mix into a PlannerAgent host."""

    def _load_pipeline_rules(self) -> list[dict[str, Any]]:
        return self.recall("_pipeline_rules") or []

    def _save_pipeline_rule(self, rule: dict[str, Any]) -> None:
        rules = self._load_pipeline_rules()
        rules = [r for r in rules if r.get("rule_id") != rule["rule_id"]]
        rules.append(rule)
        self.persist("_pipeline_rules", rules)

    async def _run_pipeline(self, task: str, workers: list[dict[str, Any]]) -> str:
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
        self, plan: list[dict[str, Any]], task: str, resolution_note: str = ""
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
            outcome = await self._spawn_pipeline_step(step, task)
            if outcome.wired:
                wired.append(outcome.wired)
            if outcome.spawned:
                spawned.append(outcome.spawned)
            if outcome.rule_agent:
                rule_agents.append(outcome.rule_agent)

        # Agents wait for MQTT changes, but if the entity is already in the
        # target state before they spawned they would never receive a trigger.
        if spawned:
            asyncio.create_task(self._bootstrap_ha_entity_states(task, plan))

        if rule_agents:
            self._persist_pipeline_rule(task, rule_agents)

        self._auto_terminate = False
        return _pipeline_summary(wired, spawned, resolution_note)

    async def _spawn_pipeline_step(self, step: dict[str, Any], task: str) -> _StepOutcome:
        """Bring one step's agent into being, recording why if it cannot be.

        Every outcome is written to `_spawn_results` so the reply to main can
        say what actually happened per agent rather than reporting one status
        for the whole plan.
        """
        name = step.get("name", "").strip()
        if not name:
            await self._log("Step missing name — skipping")
            return _StepOutcome()

        if self._registry and self._registry.find_by_name(name):
            await self._log(f"'{name}' already running — skipping")
            self._spawn_results[name] = {"ok": True, "status": "already_running"}
            return _StepOutcome(wired=f"**{name}** (already active)", rule_agent=name)

        spawn_cfg = step.get("spawn_config")
        if not spawn_cfg:
            await self._log(f"Step '{name}' has no spawn_config — skipping")
            self._spawn_results[name] = {
                "ok": False,
                "status": "no_config",
                "error": "missing spawn_config",
            }
            return _StepOutcome()

        spawn_cfg = dict(spawn_cfg)
        spawn_cfg["name"] = name
        await self._log(f"Spawning '{name}' (type={spawn_cfg.get('type', 'dynamic')})...")
        try:
            actor = await self._spawn_agent(spawn_cfg)
        except Exception as e:
            await self._log(f"Spawn failed for '{name}': {e}")
            self._spawn_results[name] = {"ok": False, "status": "spawn_failed", "error": str(e)}
            return _StepOutcome(wired=f"**{name}** — spawn failed: {e}")

        if not actor:
            self._spawn_results[name] = {"ok": False, "status": "spawn_returned_none"}
            return _StepOutcome(wired=f"**{name}** — failed to spawn")

        self._spawned_by_planner.append(name)
        self._spawn_results[name] = {"ok": True, "status": "spawned"}
        self._register_for_restore(spawn_cfg, name, task)

        topics = spawn_cfg.get("mqtt_topics", [])
        label = f"**{name}** — {step.get('description', '')}"
        if topics:
            label += "\n  listens: " + ", ".join(topics)
        await asyncio.sleep(0.3)
        return _StepOutcome(wired=label, spawned=name, rule_agent=name)

    def _register_for_restore(self, spawn_cfg: dict[str, Any], name: str, task: str) -> None:
        """Record the agent in main's spawn registry so a restart brings it back."""
        if not self._registry:
            return
        main = find_main_actor(self._registry)
        if not main:
            return
        registry_cfg = dict(spawn_cfg)
        registry_cfg["name"] = name
        registry_cfg["_rule"] = True
        registry_cfg["_rule_task"] = task[:200]
        main._save_to_spawn_registry(registry_cfg)

    def _persist_pipeline_rule(self, task: str, rule_agents: list[str]) -> None:
        """Save the rule on main, so it survives this planner terminating."""
        if not self._registry:
            return
        main = find_main_actor(self._registry)
        if not main:
            return
        rule_id = hashlib.md5(task.encode(), usedforsecurity=False).hexdigest()[:8]
        main.save_pipeline_rule(
            {
                "rule_id": rule_id,
                "task": task,
                "agents": rule_agents,
                "created_at": time.time(),
            }
        )
        logger.info("[%s] Pipeline rule %s saved to main", self.name, rule_id)

    async def _check_rule_conflicts(self, task: str, plan: list[dict[str, Any]]) -> str:
        """Compare the proposed pipeline against already-active rules and flag
        duplicates or contradictions.

        Returns a short human-readable advisory (shown at approval time, or
        prepended to the immediate-execute summary), or "" when there's nothing
        notable, no LLM, or no existing rules. This is ADVISORY ONLY - it never
        blocks the plan.

        Two things are flagged:
          - DUPLICATE      - same trigger AND same action as an existing rule.
          - CONTRADICTION  - same/overlapping trigger, OPPOSING action.
        """
        if not self.llm:
            return ""

        existing_lines, by_id = _active_rule_lines(self._active_rules())
        if not existing_lines:
            return ""

        prompt = (
            RULE_CONFLICT_PROMPT.format(task=task) + "\n".join(existing_lines) + "\n\n"
            "Respond with ONLY a JSON object:\n"
            '{"conflict": <true|false>, "items": [{"rule_id": "<id>", '
            '"kind": "duplicate|contradiction", "reason": "<one short sentence>"}]}\n'
            "Be conservative - if there is no CLEAR duplicate or contradiction, "
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
            logger.debug("[%s] Rule-conflict check failed: %s", self.name, e)
            return ""

        return _describe_rule_conflicts(data, by_id)

    def _active_rules(self) -> list[dict[str, Any]]:
        """Rules already in force, read from main as the authoritative store."""
        if not self._registry:
            return []
        main = find_main_actor(self._registry)
        if not main:
            return []
        try:
            return list(main.get_pipeline_rules().values())
        except Exception:
            return []

    async def _decompose_pipeline(
        self, task: str, workers: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Decomposes a reactive pipeline request into persistent agent spawn configs.

        Flow:
          1. Query HomeAssistantAgent for live entities (delegates — no duplication)
          2. Feasibility check — surface clear error if required HA entities are missing
          3. LLM produces spawn configs with real entity IDs and correct MQTT wiring
        """
        if not self.llm:
            return []

        ha_entities_text, ha_available, ha_section = await self._gather_ha_entities()

        camera_section, camera_snapshot_section = await self._gather_camera_context(
            task, ha_entities_text
        )

        topic_bus_section, topic_samples_section = await self._gather_topic_bus_context()

        notif_section = await self._gather_notification_urls(task)

        if ha_available and ha_entities_text and not _skips_ha_feasibility(task):
            verdict = await self._check_ha_feasibility(task, ha_section)
            if verdict is not None:
                return verdict

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
                logger.info("[%s] Pipeline plan: %s step(s)", self.name, len(plan))
                for i, step in enumerate(plan):
                    sc = step.get("spawn_config", {})
                    logger.info(
                        "[%s]   step %s: name=%s  type=%s  topics=%s",
                        self.name,
                        i + 1,
                        step.get("name"),
                        sc.get("type"),
                        sc.get("mqtt_topics", []),
                    )
                return plan
        except Exception:
            logger.exception("[%s] Pipeline decomposition failed", self.name)
        return []

    async def _gather_notification_urls(self, task: str) -> str:
        """Notification webhook URLs main has stored, plus any named in the task."""
        # ── Fetch stored notification URLs from main ──────────────────────
        notification_urls: dict[str, Any] = {}
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
        return notif_section

    async def _gather_topic_bus_context(self) -> tuple[str, str]:
        """Live data flows and wiring opportunities from the TopicBus."""
        # ── Fetch TopicBus context (live data flows + wiring opportunities) ─
        topic_bus_section = ""
        topic_samples_section = ""
        try:
            from ...core.topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus and bus.registry.all_contracts():
                topic_bus_section = bus.to_planner_context()
                logger.info(
                    "[%s] TopicBus: %s contracts", self.name, len(bus.registry.all_contracts())
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

        return topic_bus_section, topic_samples_section

    async def _gather_camera_context(self, task: str, ha_entities_text: str) -> tuple[str, str]:
        """Real camera stream and snapshot URLs, resolved via home-assistant-agent.

        Grounds vision pipelines in URLs that exist rather than letting the
        model invent /dev/video0 or guess a proxy path.
        """
        # ── Resolve real camera stream URLs via home-assistant-agent ───────
        # Mirrors the entity-list delegation above: ground PATTERN 3 (camera
        # detection pipelines) in real stream URLs instead of letting the LLM
        # invent /dev/video0 or guess proxy paths.
        camera_stream_urls, camera_snapshot_urls = await self._fetch_camera_urls(
            task, ha_entities_text
        )
        camera_section, camera_snapshot_section = _camera_sections(
            camera_stream_urls, camera_snapshot_urls
        )

        return camera_section, camera_snapshot_section

    async def _gather_ha_entities(self) -> tuple[str, bool, str]:
        """The Home Assistant entity list, formatted for the prompt."""
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
                            "[%s] Got %s HA entities via home-assistant-agent (formatted %s lines for prompt)",
                            self.name,
                            len(entities_list),
                            len(lines),
                        )
        except Exception as e:
            logger.warning("[%s] Could not query home-assistant-agent: %s", self.name, e)

        # Fallback: fetch directly if HA agent is unavailable
        if not ha_available:
            ha_entities_text, ha_available = await self._fetch_ha_entities_directly()

        ha_section = (
            ha_entities_text or "  (HA not reachable — use entity IDs provided by the user)"
        )

        return ha_entities_text, ha_available, ha_section

    async def _fetch_ha_entities_directly(self) -> tuple[str, bool]:
        """Read Home Assistant over HTTP when its agent is not answering.

        The agent is preferred because it already holds the list; this is the
        path for an install where it never started.
        """
        ha_entities_text = ""
        ha_available = False
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
                logger.info("[%s] Direct HA fetch: %s entities", self.name, len(lines))
        except Exception as e:
            logger.warning("[%s] Direct HA fetch failed: %s", self.name, e)
        return ha_entities_text, ha_available

    async def _fetch_camera_urls(
        self, task: str, ha_entities_text: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Stream and snapshot URLs per camera entity, from home-assistant-agent."""
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
                candidates = _camera_candidates(camera_entity_ids, task)

                logger.debug(
                    "[%s] Camera candidates for '%s': %s", self.name, task[:60], candidates
                )

                await self._resolve_camera_urls(
                    candidates, camera_stream_urls, camera_snapshot_urls
                )

                if camera_stream_urls:
                    logger.debug(
                        "[%s] Resolved %s camera stream URL(s)", self.name, len(camera_stream_urls)
                    )
                if camera_snapshot_urls:
                    logger.debug(
                        "[%s] Resolved %s camera snapshot URL(s)",
                        self.name,
                        len(camera_snapshot_urls),
                    )
        except Exception as e:
            logger.warning("[%s] Could not resolve camera stream/snapshot URLs: %s", self.name, e)
        return camera_stream_urls, camera_snapshot_urls

    async def _resolve_camera_urls(
        self,
        candidates: list[str],
        camera_stream_urls: dict[str, str],
        camera_snapshot_urls: dict[str, str],
    ) -> None:
        """Fill in stream and snapshot URLs for each candidate camera entity."""
        for eid in candidates:
            result = await self._delegate_with_payload(
                "home-assistant-agent",
                {"operation": "get_camera_stream_url", "camera_entity_id": eid},
                timeout=20.0,
            )
            logger.debug("[%s] get_camera_stream_url(%s) -> %s", self.name, eid, result)
            if not result or result.get("error"):
                continue
            streams = (result.get("data") or {}).get("streams", {})
            url = streams.get("camera_source") or streams.get("mjpeg_proxy") or streams.get("hls")
            if url:
                camera_stream_urls[eid] = url

            snap_result = await self._delegate_with_payload(
                "home-assistant-agent",
                {"operation": "get_camera_snapshot_url", "camera_entity_id": eid},
                timeout=20.0,
            )
            logger.debug("[%s] get_camera_snapshot_url(%s) -> %s", self.name, eid, snap_result)
            if snap_result and not snap_result.get("error"):
                snap_url = (snap_result.get("data") or {}).get("snapshot_url")
                if snap_url:
                    camera_snapshot_urls[eid] = snap_url

    async def _check_ha_feasibility(
        self, task: str, ha_section: str
    ) -> list[dict[str, Any]] | None:
        """Ask whether the request is buildable from the entities that exist.

        Returns a one-item plan carrying the refusal when it is not, and None
        when planning should continue -- including when the check itself fails,
        because an unavailable checker must not block a workable request.
        """
        if not self.llm:
            return None
        try:
            feas_resp, _usage = await self.llm.complete(
                messages=[
                    {
                        "role": "user",
                        "content": HA_FEASIBILITY_PROMPT.format(task=task, ha_section=ha_section),
                    }
                ],
                system=self._now_context() + "\nOutput only valid JSON. No markdown.",
                max_tokens=400,
            )
            self._accrue_usage(_usage)
            clean = feas_resp.strip()
            for fence in ("```json", "```"):
                clean = clean.removeprefix(fence)
                clean = clean.removesuffix("```")
            feas = json.loads(clean.strip())
        except Exception as e:
            logger.warning("[%s] Feasibility check error (continuing): %s", self.name, e)
            return None

        if not feas.get("feasible", True):
            reason = feas.get("reason", "Cannot fulfill request with available HA entities.")
            logger.warning("[%s] Feasibility failed: %s", self.name, reason)
            return [{"_feasibility_error": reason}]

        logger.info(
            "[%s] Feasibility OK — relevant: %s", self.name, feas.get("relevant_entities", [])
        )
        return None

    async def _bootstrap_ha_entity_states(
        self, task: str, plan: list[dict[str, Any]] | None = None
    ) -> None:
        """Publish current HA states so freshly-spawned agents fire immediately.

        A spawned pipeline agent sits idle until the next MQTT change arrives.
        If the entity is already in the desired state (the lights are on
        already) that change never comes and the agent never fires, so the
        current states are fetched and republished to
        homeassistant/state_changes/# as a bootstrap event.
        """
        entity_ids = _ha_entity_ids_in(plan, task)
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


def _active_rule_lines(
    existing: list[dict[str, Any]],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """One prompt line per active rule, capped so the prompt stays bounded."""
    lines: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for r in existing[:30]:
        rid = r.get("rule_id", "?")
        rtask = (r.get("task") or "").strip().replace("\n", " ")[:160]
        if rtask:
            by_id[rid] = r
            lines.append(f"- [{rid}] {rtask}")
    return lines, by_id


def _describe_rule_conflicts(data: object, by_id: dict[str, dict[str, Any]]) -> str:
    """Render the checker's verdict, or "" when it found nothing clear."""
    if not isinstance(data, dict) or not data.get("conflict"):
        return ""

    lines = []
    for it in (data.get("items") or [])[:5]:
        if not isinstance(it, dict):
            continue
        kind = (it.get("kind") or "overlap").lower()
        rid = it.get("rule_id", "?")
        reason = (it.get("reason") or "").strip()
        rtask = (by_id.get(rid, {}).get("task") or "").strip().replace("\n", " ")[:120]
        bit = ("Duplicate of" if kind.startswith("dup") else "May contradict") + f" rule [{rid}]"
        if rtask:
            bit += f' ("{rtask}")'
        if reason:
            bit += f" - {reason}"
        lines.append(bit)
    if not lines:
        return ""
    return "This pipeline may overlap with existing rules:\n" + "\n".join(
        f"  \u2022 {ln}" for ln in lines
    )


#: Home Assistant domains an entity id may start with. Anything else that looks
#: like `word.word` in generated code is a Python attribute, not an entity.
HA_DOMAINS = frozenset(
    {
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
)
_HA_ENTITY_RE = re.compile(r"\b([a-z_][a-z0-9_]*\.[a-z0-9_]+)\b")


def _ha_entity_ids_in(plan: list[dict[str, Any]] | None, task: str) -> list[str]:
    """Entity ids a spawned pipeline will react to, most reliable source first.

    Generated code carries the literal entity_id, so it is read before the
    ha_actuator actions, the per-entity topic paths, and finally the enriched
    task text. Order matters only for readability -- ids are de-duplicated.
    """
    seen: set[str] = set()
    entity_ids: list[str] = []

    def _add(eid: str) -> None:
        eid = eid.strip().lower()
        if eid and eid not in seen and eid.split(".")[0] in HA_DOMAINS:
            seen.add(eid)
            entity_ids.append(eid)

    for step in plan or []:
        cfg = step.get("spawn_config") or {}
        for m in _HA_ENTITY_RE.finditer(cfg.get("code", "")):
            _add(m.group(1))
        for action in cfg.get("actions") or []:
            _add(action.get("entity_id", ""))
        for topic in cfg.get("mqtt_topics") or []:
            m = re.search(r"homeassistant/state_changes/[^/]+/([^/#]+)", topic)
            if m:
                _add(m.group(1))

    for m in _HA_ENTITY_RE.finditer(task.lower()):
        _add(m.group(1))

    return entity_ids


class _StepOutcome(NamedTuple):
    """What one pipeline step contributed, empty fields meaning "nothing"."""

    wired: str | None = None
    spawned: str | None = None
    rule_agent: str | None = None


def _pipeline_summary(wired: list[str], spawned: list[str], resolution_note: str) -> str:
    """The reply the user sees once the pipeline is up."""
    if not wired:
        return "Pipeline plan generated but no agents could be spawned. Check logs."

    out = ["Pipeline active! Here's what I set up:\n"]
    if resolution_note:
        out.insert(0, f"\U0001f4e1 **Data source resolved:** {resolution_note}\n")
    out += [f"{i + 1}. {w}" for i, w in enumerate(wired)]
    out.append("\nThese agents run continuously and react to events automatically.")
    out.append("Use `/rules` to see all active pipeline rules.")
    if spawned:
        out.append(f"\nSpawned: {', '.join(spawned)} — will auto-restore on restart.")
    return "\n".join(out)


#: Words meaning the pipeline does not touch Home Assistant: vision pipelines
#: (they need cv2) and external webhook integrations.
_NON_HA_KEYWORDS = (
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

#: Verbs that mean an HA service call is coming, so a real entity must exist.
_HA_ACTION_VERBS = (
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


def _skips_ha_feasibility(task: str) -> bool:
    """True when the request clearly asks for nothing of Home Assistant.

    Deliberately narrow. An earlier version also skipped on "message" and
    "notify", which sent "when X happens log a warning message" straight past
    the check -- and that masked real HA-target bugs, because most log-style
    requests appeared to work whether or not the entity list was intact.

    A task naming both ("send Discord AND turn off the lamp") is still checked,
    because the lamp half needs an entity.
    """
    lowered = task.lower()
    has_ha_verb = any(v in lowered for v in _HA_ACTION_VERBS)
    has_skip_kw = any(kw in lowered for kw in _NON_HA_KEYWORDS)
    return has_skip_kw and not has_ha_verb


def _camera_sections(
    camera_stream_urls: dict[str, str], camera_snapshot_urls: dict[str, str]
) -> tuple[str, str]:
    """Render the resolved camera URLs for the prompt."""
    camera_section = ""
    camera_snapshot_section = ""
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
    return camera_section, camera_snapshot_section


def _camera_candidates(camera_entity_ids: list[str], task: str) -> list[str]:
    """Camera entities the task plausibly refers to, or all of them.

    A task that clearly means a camera but names no particular one still gets
    the full list, because guessing nothing is worse than offering several.
    """
    task_words = {w for w in re.findall(r"[a-z0-9]+", task.lower()) if len(w) >= 3}
    candidates = [eid for eid in camera_entity_ids if any(w in eid.lower() for w in task_words)]
    if not candidates and any(kw in task.lower() for kw in ("camera", "webcam", "stream")):
        candidates = camera_entity_ids[:5]
    return candidates
