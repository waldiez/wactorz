"""Running a plan: making sure its agents exist, then driving the steps.

Steps marked parallel run together; the rest wait on what they depend on. The
final synthesis is always handed back to main rather than a domain agent.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from ...core.actor import Actor, MessageType
from ..mixins.spawning import SpawnPlaceholder

if TYPE_CHECKING:
    from .hosts import ExecutionHost

    # Typing-only base: it states what the host must provide and is gone
    # at runtime, so the real MRO is exactly what it was.
    _Host = ExecutionHost
else:
    _Host = object

logger = logging.getLogger(__name__)


class ExecutionMixin(_Host):
    """Running a plan's steps. Mix into a PlannerAgent host."""

    async def _ensure_agents(self, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            except Exception:
                logger.exception("[%s] Spawn of '%s' failed", self.name, agent_name)
                step["agent"] = "main"

        return plan

    async def _spawn_agent(self, config: dict[str, Any]) -> Actor | SpawnPlaceholder | None:
        """Spawn an agent for a plan step. Delegates to the shared SpawnMixin.

        Uses the BLOCKING install path: a pipeline's next step may depend on
        this agent being live, so we wait for any package install to finish
        rather than returning a placeholder.
        """
        return await self._spawn_local_from_config(config, register=True, blocking_install=True)

    async def _execute(self, plan: list[dict[str, Any]]) -> dict[str | int, Any]:
        results: dict[str | int, Any] = {}
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
                    "[%s] Step %s depends on missing step(s) %s — marking failed",
                    self.name,
                    step,
                    bad,
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
                    "[%s] Plan deadlock — unschedulable steps %s (circular depends_on?)",
                    self.name,
                    stuck,
                )
                for s in remaining:
                    results[s.get("step", "")] = {
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

    async def _execute_step(
        self, step: dict[str, Any], prior: dict[str | int, Any]
    ) -> dict[str, Any]:
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

    async def _delegate(
        self, agent_name: str, task: str, timeout: float = 60.0
    ) -> dict[str, Any] | None:
        return await self._delegate_with_payload(agent_name, {"text": task}, timeout=timeout)

    async def _delegate_with_payload(
        self, agent_name: str, payload: dict[str, Any], timeout: float = 60.0
    ) -> dict[str, Any] | None:
        if not self._registry:
            return None
        target = self._registry.find_by_name(agent_name)
        if not target:
            logger.warning("[%s] Agent '%s' not found for delegation", self.name, agent_name)
            return {"error": f"Agent '{agent_name}' not found"}

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
            logger.warning("[%s] Timeout from '%s'", self.name, agent_name)
            return {"error": f"Timeout from {agent_name}"}
        finally:
            self._result_futures.pop(task_id, None)

    async def _synthesize(
        self, task: str, plan: list[dict[str, Any]], results: dict[str | int, Any]
    ) -> str:
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
        except Exception:
            logger.exception("[%s] Synthesis failed", self.name)
            return "\n\n".join(results_text)
        else:
            return response
