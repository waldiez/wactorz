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
from typing import Optional

from ..core.actor import Actor, Message, MessageType
from .llm_agent import LLMProvider

logger = logging.getLogger(__name__)

_SKIP_AGENTS    = {"main", "monitor", "installer", "home-assistant-agent", "home-assistant-hardware", "home-assistant-automation", "anomaly-detector", "code-agent"}
_PLAN_CACHE_KEY = "_plan_cache"
_CACHE_TTL_S    = 86400   # 24 hours


class PlannerAgent(Actor):
    """
    On-demand orchestrator. Spawned per complex task, self-terminates when done.
    """

    def __init__(
        self,
        llm_provider:   Optional[LLMProvider] = None,
        task:           str = "",
        reply_to_id:    str = "",
        reply_task_id:  str = "",
        auto_terminate: bool = True,
        **kwargs,
    ):
        kwargs.setdefault("name", "planner")
        super().__init__(**kwargs)
        self.llm              = llm_provider
        self._task            = task
        self._reply_to_id     = reply_to_id
        self._reply_task_id   = reply_task_id
        self._auto_terminate  = auto_terminate
        self._result_futures: dict[str, asyncio.Future] = {}
        self._spawned_by_planner: list[str] = []   # agents we created this run

    def _current_task_description(self) -> str:
        return self._task[:60] if self._task else "waiting for task"

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        await self._log(f"Planner ready. Task: {self._task[:80]}")
        if self._task:
            asyncio.create_task(self._report_plan(self._task))

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            payload   = msg.payload if isinstance(msg.payload, dict) else {"text": str(msg.payload)}
            task_text = payload.get("text") or payload.get("task") or str(msg.payload)
            self._reply_to_id = payload.get("_reply_to") or msg.reply_to or msg.sender_id or self._reply_to_id
            task_id           = payload.get("_task_id")
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
            reply = {"result": result, "text": result}
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

    def _is_pipeline_request(task: str) -> bool:
        """
        Detect reactive/persistent pipeline requests vs one-shot tasks.
        Pipelines use conditional/temporal language: if/when/whenever/monitor/watch/notify.
        """
        import re
        lowered = task.lower()

        # Explicit pipeline prefix always wins
        if lowered.startswith("pipeline:") or lowered.startswith("pipeline "):
            return True

        patterns = [
            r"\bif\b.*\bthen\b",
            r"\bwhen\b.*\b(detect|open|turn|send|notify|alert)\b",
            r"\bwhenever\b",
            r"\bmonitor\b", r"\bwatch\b",
            r"\balert me\b", r"\bnotify me\b",
            r"\bsend me\b.*\bwhen\b",
            r"\bautomatically\b",
            r"\bevery time\b", r"\bon detection\b",
            # camera/detect + action = pipeline
            r"\b(camera|detect|yolo|webcam)\b.*\b(turn|open|send|notify|alert)\b",
            r"\b(person|motion|object)\b.*\bdetect.*\b(turn|open|light|send)\b",
        ]
        return any(re.search(p, lowered) for p in patterns)

    async def _run_plan(self, task: str) -> str:
        workers = self._discover_workers()
        await self._log(f"Workers available: {[w['name'] for w in workers]}")

        # Detect pipeline vs one-shot
        is_pipeline = PlannerAgent._is_pipeline_request(task)
        if is_pipeline:
            await self._log("Pipeline request detected — spawning persistent agents...")
            return await self._run_pipeline(task, workers)

        # ── 1. Check cache ─────────────────────────────────────────────────
        cache_key  = _task_hash(task)
        cached     = self._load_cached_plan(cache_key, workers)
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
        Builds and spawns persistent reactive agents for if/when/whenever rules.

        Flow:
          1. _decompose_pipeline queries HomeAssistantAgent for real entity IDs
          2. LLM produces spawn configs (ha_actuator for HA actions, dynamic for everything else)
          3. Each agent is spawned and registered in main's spawn registry
          4. Rule is saved so it can be listed/deleted later
          5. Summary returned to the user

        Multiple rules in one request are fully supported.
        """
        plan = await self._decompose_pipeline(task, workers)

        if not plan:
            await self._log("Pipeline decomposition failed — falling back to direct answer")
            return await self._llm_answer(task)

        if len(plan) == 1 and "_feasibility_error" in plan[0]:
            error = plan[0]["_feasibility_error"]
            await self._log(f"Pipeline not feasible: {error}")
            return f"Cannot set up this pipeline:\n\n{error}"

        await self._log(f"Pipeline plan: {len(plan)} agent(s)")
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
                continue

            if not spawn_cfg:
                await self._log(f"Step '{name}' has no spawn_config — skipping")
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
                continue

            if actor:
                self._spawned_by_planner.append(name)
                spawned.append(name)
                rule_agents.append(name)

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

        # Persist this rule for listing / deletion
        if rule_agents:
            import hashlib as _hl
            rule_id = _hl.md5(task.encode()).hexdigest()[:8]
            self._save_pipeline_rule({
                "rule_id": rule_id,
                "task": task,
                "agents": rule_agents,
                "created_at": time.time(),
            })

        self._auto_terminate = False

        if not wired:
            return "Pipeline plan generated but no agents could be spawned. Check logs."

        out = ["Pipeline active! Here's what I set up:\n"]
        out += [f"{i+1}. {w}" for i, w in enumerate(wired)]
        out.append("\nThese agents run continuously and react to events automatically.")
        out.append("Use `/rules` to see all active pipeline rules.")
        if spawned:
            out.append(f"\nSpawned: {', '.join(spawned)} — will auto-restore on restart.")
        return "\n".join(out)

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
                        for e in entities_list[:200]:
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
                        logger.info(f"[{self.name}] Got {len(entities_list)} HA entities via home-assistant-agent")
        except Exception as e:
            logger.warning(f"[{self.name}] Could not query home-assistant-agent: {e}")

        # Fallback: fetch directly if HA agent is unavailable
        if not ha_available:
            try:
                from ..config import CONFIG
                from ..core.integrations.home_assistant.ha_helper import fetch_devices_entities_with_location
                ha_url = (CONFIG.ha_url or "").rstrip("/")
                ha_token = (CONFIG.ha_token or "").strip()
                if ha_url and ha_token:
                    devices = await fetch_devices_entities_with_location(ha_url, ha_token, include_states=True)
                    lines = []
                    for device in devices[:150]:
                        area = device.get("area", "")
                        for entity in device.get("entities", []):
                            eid = entity.get("entity_id", "")
                            ename = entity.get("friendly_name") or entity.get("name", "")
                            state = entity.get("state", "")
                            if eid:
                                parts = [eid]
                                if ename: parts.append(f"name={ename}")
                                if area: parts.append(f"area={area}")
                                if state: parts.append(f"state={state}")
                                lines.append("  " + "  ".join(parts))
                    ha_entities_text = "\n".join(lines)
                    ha_available = bool(lines)
                    logger.info(f"[{self.name}] Direct HA fetch: {len(lines)} entities")
            except Exception as e:
                logger.warning(f"[{self.name}] Direct HA fetch failed: {e}")

        ha_section = ha_entities_text if ha_entities_text else \
            "  (HA not reachable — use entity IDs provided by the user)"

        # ── 2. Feasibility check (skip for local/external triggers) ───────
        _local_kw = ("camera", "webcam", "laptop", "detect", "yolo", "person",
                     "object detection", "cv2", "opencv")
        _skip_feasibility = any(kw in task.lower() for kw in _local_kw)

        if ha_available and ha_entities_text and not _skip_feasibility:
            feas_prompt = (
                "Check if this reactive automation can be fulfilled with available HA entities.\n\n"
                f"USER REQUEST: {task}\n\n"
                f"AVAILABLE HA ENTITIES:\n{ha_section}\n\n"
                'Return JSON only:\n'
                '{"feasible": true/false, "reason": "<one sentence if not feasible>", "relevant_entities": ["entity_id", ...]}\n\n'
                "Rules:\n"
                "- feasible=true only if ALL required entity types exist\n"
                "- Camera/webcam/Discord/notification requests: always feasible=true"
            )
            try:
                feas_resp, _ = await self.llm.complete(
                    messages=[{"role": "user", "content": feas_prompt}],
                    system="Output only valid JSON. No markdown.",
                    max_tokens=400,
                )
                clean = feas_resp.strip()
                for fence in ("```json", "```"):
                    if clean.startswith(fence):
                        clean = clean[len(fence):]
                    if clean.endswith("```"):
                        clean = clean[:-3]
                clean = clean.strip()
                feas = json.loads(clean)
                if not feas.get("feasible", True):
                    reason = feas.get("reason", "Cannot fulfill request with available HA entities.")
                    logger.warning(f"[{self.name}] Feasibility failed: {reason}")
                    return [{"_feasibility_error": reason}]
                logger.info(f"[{self.name}] Feasibility OK — relevant: {feas.get('relevant_entities', [])}")
            except Exception as e:
                logger.warning(f"[{self.name}] Feasibility check error (continuing): {e}")

        # ── 3. Decompose into spawn configs ────────────────────────────────
        # Build the prompt as a list of parts to avoid f-string escape issues
        prompt_parts = [
            "You are designing reactive automation pipelines for a multi-agent IoT system.",
            "Output ONLY a valid JSON array — no explanation, no markdown, no code fences.",
            "",
            "SYSTEM ARCHITECTURE:",
            "- HomeAssistantStateBridgeAgent (always running): publishes HA state changes to:",
            "    homeassistant/state_changes/<domain>/<entity_id>",
            '  Payload: {"type": "home_assistant_state_change", "entity_id": "...", "domain": "...",',
            '           "new_state": {"state": "...", "attributes": {}}, "old_state": {...}}',
            "",
            "AGENT TYPES TO SPAWN:",
            "",
            'TYPE 1 — "ha_actuator" — USE FOR: any HA service call (lights, switches, climate, cover)',
            "  No code needed. Subscribes to MQTT topic, calls HA service when payload matches filter.",
            "  spawn_config:",
            "  {",
            '    "type": "ha_actuator",',
            '    "automation_id": "<unique-kebab-slug>",',
            '    "description": "<plain english>",',
            '    "mqtt_topics": ["<topic>"],',
            '    "actions": [{"domain": "<domain>", "service": "<service>", "entity_id": "<exact_from_list>", "service_data": {}}],',
            '    "conditions": [],',
            '    "detection_filter": {"<key>": "<value>"} or null,',
            '    "cooldown_seconds": 10',
            "  }",
            "  IMPORTANT: detection_filter matches TOP-LEVEL payload keys only.",
            "  HA state payloads have state nested in new_state.state — use a dynamic filter agent for these (Pattern A).",
            "",
            'TYPE 2 — "dynamic" — USE FOR: webcam/YOLO, state filtering, timers, Discord, external APIs',
            "  Custom Python. Define async functions. Available APIs ONLY:",
            '    await agent.log("msg")             — log',
            '    await agent.publish("topic", {})   — MQTT publish',
            '    agent.recall("key")                — persistent load',
            '    agent.persist("key", val)          — persistent save',
            '    agent.state["key"]                 — in-memory (lost on restart)',
            "  spawn_config:",
            "  {",
            '    "type": "dynamic",',
            '    "description": "<description>",',
            '    "install": ["<pip-package>"],',
            '    "poll_interval": <seconds>,',
            '    "code": "<python source>"',
            "  }",
            "",
            "WIRING PATTERNS:",
            "",
            "Pattern A — HA state change -> HA action (door opens -> turn on light):",
            "  TWO agents:",
            "  1. dynamic filter agent — subscribes to homeassistant/state_changes/<domain>/<entity_id>,",
            '     checks new_state["state"] == target_value, publishes {"triggered": true} to custom/triggers/<slug>',
            "  2. ha_actuator — subscribes to custom/triggers/<slug>, detection_filter: null",
            "  Dynamic filter code example:",
            "    async def setup(agent):",
            "        import asyncio, json, aiomqtt",
            "        async def _listen():",
            '            async with aiomqtt.Client("localhost", 1883) as c:',
            '                await c.subscribe("homeassistant/state_changes/binary_sensor/binary_sensor.front_door")',
            "                async for msg in c.messages:",
            "                    try:",
            "                        d = json.loads(msg.payload)",
            '                        if d.get("new_state", {}).get("state") == "on":',
            '                            await agent.publish("custom/triggers/door-open", {"triggered": True})',
            "                    except: pass",
            "        asyncio.create_task(_listen())",
            "",
            "Pattern B — Webcam person detection -> HA light/action:",
            "  TWO agents:",
            '  1. dynamic YOLO agent — publishes {"person_detected": bool, "objects": [str]} to custom/detections/<slug>',
            '  2. ha_actuator — subscribes to custom/detections/<slug>, detection_filter: {"person_detected": true}',
            "  YOLO agent code example:",
            "    async def setup(agent):",
            "        import cv2",
            "        from ultralytics import YOLO",
            '        agent.state["model"] = YOLO("yolov8n.pt")',
            '        agent.state["cap"] = cv2.VideoCapture(0)',
            '        await agent.log("YOLO webcam ready")',
            "    async def process(agent):",
            '        cap = agent.state.get("cap")',
            '        model = agent.state.get("model")',
            "        if not cap or not model: return",
            "        ret, frame = cap.read()",
            "        if not ret: return",
            "        results = model(frame, verbose=False)",
            "        detected = [model.names[int(b.cls)] for r in results for b in r.boxes]",
            '        person = "person" in detected',
            '        await agent.publish("custom/detections/SLUG", {"person_detected": person, "objects": list(set(detected))})',
            "",
            "Pattern C — HA state change -> Discord notification:",
            "  ONE dynamic agent: subscribe to HA state topic, filter state, POST to Discord webhook URL",
            "  Use httpx: async with httpx.AsyncClient() as c: await c.post(url, json={'content': msg})",
            "",
            "Pattern D — Webcam detection -> Discord notification:",
            "  TWO agents: YOLO dynamic (Pattern B step 1) + Discord dynamic (subscribes, POSTs webhook)",
            "",
            "RULES:",
            "- Use exact entity_id values from the HA entities list below",
            "- Multiple rules in one request -> produce ALL agents for ALL rules",
            "- Replace SLUG with actual automation slug in code strings",
            "- Each agent does ONE thing — keep minimal",
            "- NEVER spawn HomeAssistantStateBridgeAgent — it is always running",
            "",
            "HOME ASSISTANT ENTITIES:",
            ha_section,
            "",
            "OUTPUT FORMAT — JSON array, each item:",
            '{"name": "<kebab-case>", "description": "<one sentence>", "spawn_config": {...}}',
            "",
            "USER REQUEST:",
            task,
        ]
        prompt = "\n".join(prompt_parts)

        try:
            response, _ = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You are a JSON-only pipeline architect. Output only a valid JSON array. No markdown, no explanation.",
                max_tokens=4000,
            )
            clean = response.strip()
            if clean.startswith("```"):
                clean = "\n".join(clean.split("\n")[1:])
            if "```" in clean:
                clean = clean[:clean.rfind("```")]
            start = clean.find("[")
            end = clean.rfind("]")
            if start != -1 and end != -1:
                clean = clean[start:end + 1]
            plan = json.loads(clean.strip())
            if isinstance(plan, list):
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

        return []

    # ── Plan cache ─────────────────────────────────────────────────────────

    def _load_cached_plan(self, cache_key: str, workers: list[dict]) -> Optional[list]:
        """Load a cached plan if it exists, is fresh, and all required agents are alive."""
        raw = self.recall(_PLAN_CACHE_KEY) or {}
        entry = raw.get(cache_key)
        if not entry:
            return None

        # TTL check
        age = time.time() - entry.get("timestamp", 0)
        if age > _CACHE_TTL_S:
            logger.info(f"[{self.name}] Cache expired ({age/3600:.1f}h old)")
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
            "task":      task[:200],
            "plan":      plan,
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
        for actor in self._registry.all_actors():
            if actor.name in _SKIP_AGENTS or actor.name == self.name:
                continue
            # Prefer manifest data (richer), fall back to live actor attrs
            manifest = manifest_map.get(actor.name, {})
            workers.append({
                "name":          actor.name,
                "type":          type(actor).__name__,
                "description":   (
                    manifest.get("description")
                    or getattr(actor, "description", "")
                    or getattr(actor, "system_prompt", "")[:100]
                    or type(actor).__name__
                ),
                "capabilities":  manifest.get("capabilities", []),
                "input_schema":  manifest.get("input_schema",  {}),
                "output_schema": manifest.get("output_schema", {}),
            })
        return workers

    # ── Decomposition ──────────────────────────────────────────────────────

    async def _decompose(self, task: str, workers: list[dict]) -> list[dict]:
        """LLM breaks task into steps. Can declare missing agents with spawn configs."""
        if not self.llm:
            return []

        def _fmt_worker(w: dict) -> str:
            lines = [f"  - {w['name']} ({w['type']}): {w['description']}"]
            if w.get("capabilities"):
                lines.append(f"    capabilities: {', '.join(w['capabilities'])}")
            if w.get("input_schema"):
                lines.append(f"    input_schema : {w['input_schema']}")
            if w.get("output_schema"):
                lines.append(f"    output_schema: {w['output_schema']}")
            return "\n".join(lines)

        workers_desc = "\n".join(_fmt_worker(w) for w in workers)

        prompt = f"""You are a task planner for a multi-agent system.
Break the task into steps. Each step is handled by one agent.

AVAILABLE AGENTS (with input/output contracts):
{workers_desc}

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
    In dynamic agent code ALWAYS use: await agent.log(msg), await agent.publish(topic, dict), agent.state dict, agent.recall(key), agent.persist(key, val).
    NEVER use agent.logger — it does not exist. Use await agent.log(msg) instead.
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
            response, _ = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You are a JSON-only task planner. Output only valid JSON arrays, nothing else.",
                max_tokens=1500,
            )
            clean = response.strip()
            # Strip markdown fences
            if clean.startswith("```"):
                clean = "\n".join(clean.split("\n")[1:])
            if clean.endswith("```"):
                clean = "\n".join(clean.split("\n")[:-1])
            plan = json.loads(clean.strip())
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
        """
        if not self._registry:
            return plan

        for step in plan:
            spawn_config = step.get("spawn_config")
            if not spawn_config:
                continue

            agent_name = spawn_config.get("name") or step.get("agent")
            existing   = self._registry.find_by_name(agent_name)

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
                    # Brief pause to let agent initialise
                    await asyncio.sleep(1.0)
                    await self._log(f"'{agent_name}' ready.")
                else:
                    await self._log(f"Failed to spawn '{agent_name}' — step will use main as fallback")
                    step["agent"] = "main"
            except Exception as e:
                logger.error(f"[{self.name}] Spawn of '{agent_name}' failed: {e}")
                step["agent"] = "main"

        return plan

    async def _spawn_agent(self, config: dict) -> Optional[Actor]:
        """Spawn an agent from a config dict — same logic as MainActor._spawn_from_config."""
        agent_type = config.get("type", "dynamic")
        name       = config.get("name", "spawned-agent")

        if agent_type == "ha_actuator":
            from .home_assistant_actuator_agent import (
                HomeAssistantActuatorAgent, ActuatorConfig,
                ActuatorAction, ActuatorCondition,
            )
            # Ensure automation_id is unique — append short hash if needed
            automation_id = config.get("automation_id", name)
            if self._registry and self._registry.find_by_name(f"actuator-{automation_id[:20]}"):
                import hashlib
                suffix = hashlib.md5(f"{automation_id}{time.time()}".encode()).hexdigest()[:4]
                automation_id = f"{automation_id}-{suffix}"
                name = f"actuator-{automation_id[:20]}"
            actuator_config = ActuatorConfig(
                automation_id = automation_id,
                description   = config.get("description", ""),
                mqtt_topics   = config.get("mqtt_topics", []),
                actions       = [ActuatorAction.from_dict(a) for a in config.get("actions", [])],
                conditions    = [ActuatorCondition.from_dict(c) for c in config.get("conditions", [])],
                detection_filter = config.get("detection_filter"),
                cooldown_seconds = float(config.get("cooldown_seconds", 10.0)),
            )
            actor = await self.spawn(
                HomeAssistantActuatorAgent,
                config=actuator_config,
                name=name,
                persistence_dir=str(self._persistence_dir.parent),
            )
            await self._register_with_main(config)
            return actor

        if agent_type == "llm":
            from .llm_agent import LLMAgent
            actor = await self.spawn(
                LLMAgent,
                name=name,
                llm_provider=self.llm,
                system_prompt=config.get("system_prompt", "You are a helpful assistant."),
                persistence_dir=str(self._persistence_dir.parent),
            )
            # Save to main's spawn registry so it persists across restarts
            await self._register_with_main(config)
            return actor

        if agent_type == "dynamic":
            code = config.get("code", "").strip()
            if not code:
                logger.warning(f"[{self.name}] Dynamic spawn config has no code for '{name}'")
                return None
            from .dynamic_agent import DynamicAgent
            actor = await self.spawn(
                DynamicAgent,
                name=name,
                code=code,
                poll_interval=float(config.get("poll_interval", 1.0)),
                description=config.get("description", ""),
                input_schema=config.get("input_schema", {}),
                output_schema=config.get("output_schema", {}),
                llm_provider=self.llm,
                persistence_dir=str(self._persistence_dir.parent),
            )
            await self._register_with_main(config)
            return actor

        if agent_type == "manual":
            from .manual_agent import ManualAgent
            actor = await self.spawn(
                ManualAgent,
                name=name,
                llm_provider=self.llm,
                persistence_dir=str(self._persistence_dir.parent),
            )
            await self._register_with_main(config)
            return actor

        logger.warning(f"[{self.name}] Unknown agent type: '{agent_type}'")
        return None

    async def _register_with_main(self, config: dict):
        """Tell main to add this agent to its spawn registry so it survives restarts."""
        if not self._registry:
            return
        main = self._registry.find_by_name("main")
        if main and hasattr(main, "_save_to_spawn_registry"):
            main._save_to_spawn_registry(config)
            logger.info(f"[{self.name}] Registered '{config.get('name')}' with main's spawn registry")

    # ── Execution ──────────────────────────────────────────────────────────

    async def _execute(self, plan: list[dict]) -> dict:
        results:   dict       = {}
        completed: set[int]   = set()
        remaining: list[dict] = list(plan)

        while remaining:
            ready = [
                s for s in remaining
                if all(d in completed for d in (s.get("depends_on") or []))
            ]
            if not ready:
                logger.error(f"[{self.name}] Plan deadlock — aborting remaining steps")
                break

            parallel   = [s for s in ready if s.get("parallel", False)]
            sequential = [s for s in ready if not s.get("parallel", False)]

            if parallel:
                await self._log(f"Parallel: steps {[s['step'] for s in parallel]}")
                outputs = await asyncio.gather(
                    *[self._execute_step(s, results) for s in parallel],
                    return_exceptions=True,
                )
                for step, out in zip(parallel, outputs):
                    results[step["step"]] = out if not isinstance(out, Exception) else {"error": str(out)}
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
        task_text  = step.get("task", "")
        depends_on = step.get("depends_on") or []

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
            await self._log(f"  → falling back to @main for this step")
            fallback = await self._llm_answer(
                f"The agent '{agent_name}' failed. Do your best to answer: {task_text}"
            )
            return {"result": fallback, "fallback": True, "original_error": result["error"]}
        return result

    # ── Delegation ─────────────────────────────────────────────────────────

    async def _delegate(self, agent_name: str, task: str, timeout: float = 60.0) -> Optional[dict]:
        return await self._delegate_with_payload(agent_name, {"text": task}, timeout=timeout)

    async def _delegate_with_payload(self, agent_name: str, payload: dict, timeout: float = 60.0) -> Optional[dict]:
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

        await self.send(target.actor_id, MessageType.TASK, {
            **payload, "_task_id": task_id, "_reply_to": self.actor_id
        })
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Timeout from '{agent_name}'")
            return {"error": f"Timeout from {agent_name}"}
        finally:
            self._result_futures.pop(task_id, None)

    # ── Synthesis ──────────────────────────────────────────────────────────

    async def _synthesize(self, task: str, plan: list[dict], results: dict) -> str:
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
            f"RESULTS:\n" + "\n\n".join(results_text) +
            "\n\nSynthesize into a single, clear, well-structured answer for the user. "
            "Do not mention agent names, step numbers, or internal system details."
        )
        try:
            response, _ = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You synthesize multi-agent results into clean, user-facing answers.",
                max_tokens=2048,
            )
            return response
        except Exception as e:
            logger.error(f"[{self.name}] Synthesis failed: {e}")
            return "\n\n".join(results_text)

    async def _llm_answer(self, task: str) -> str:
        if not self.llm:
            return f"[No LLM available: {task}]"
        try:
            response, _ = await self.llm.complete(
                messages=[{"role": "user", "content": task}],
                system="You are a helpful assistant.",
                max_tokens=2048,
            )
            return response
        except Exception as e:
            return f"[LLM error: {e}]"

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _deferred_stop(self):
        await asyncio.sleep(2.0)
        await self._log("Self-terminating.")
        if self._registry:
            await self._registry.unregister(self.actor_id)
        await self.stop()

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
