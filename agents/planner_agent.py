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

    async def _run_plan(self, task: str) -> str:
        workers = self._discover_workers()
        await self._log(f"Workers available: {[w['name'] for w in workers]}")

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
        if not self._registry:
            return None
        target = self._registry.find_by_name(agent_name)
        if not target:
            logger.warning(f"[{self.name}] Agent '{agent_name}' not found for delegation")
            return {"error": f"Agent '{agent_name}' not found"}

        import uuid
        task_id = str(uuid.uuid4())[:8]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._result_futures[task_id] = future

        await self.send(target.actor_id, MessageType.TASK, {
            "text": task, "_task_id": task_id, "_reply_to": self.actor_id
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