"""The reactive-pipeline path: modes, per-step outcomes, and the rule it leaves.

A pipeline request produces persistent agents rather than one answer, so what
matters here is which mode was taken, what got spawned, and what is recorded
when a step cannot be spawned -- main reports per-agent status from that record.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.planner import pipeline as pipeline_mod
from wactorz.agents.planner.agent import PlannerAgent
from wactorz.agents.planner.pipeline import pipeline_summary

SPAWN_ERROR = "no module named cv2"


class FakeActor:
    """Stands in for a registered agent; only the name is ever read."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"id-{name}"


class FakeRegistry:
    def __init__(self, *names: str) -> None:
        self._actors = [FakeActor(n) for n in names]

    def find_by_name(self, name: str) -> FakeActor | None:
        return next((a for a in self._actors if a.name == name), None)

    def all_actors(self) -> list[FakeActor]:
        return list(self._actors)


class FakeMain:
    """Records what the planner asks main to remember."""

    def __init__(self, rules: dict[str, dict[str, Any]] | None = None) -> None:
        self.saved_rules: list[dict[str, Any]] = []
        self.spawn_registry: list[dict[str, Any]] = []
        self._rules = rules or {}

    def get_pipeline_rules(self) -> dict[str, dict[str, Any]]:
        return self._rules

    def save_pipeline_rule(self, rule: dict[str, Any]) -> None:
        self.saved_rules.append(rule)

    def _save_to_spawn_registry(self, cfg: dict[str, Any]) -> None:
        self.spawn_registry.append(cfg)


def make_planner(tmp_path: Path, **kwargs: Any) -> tuple[PlannerAgent, FakeProvider]:
    provider = FakeProvider(script=kwargs.pop("script", {}))
    planner = PlannerAgent(
        llm_provider=provider,
        persistence_dir=str(tmp_path),
        auto_terminate=False,
        **kwargs,
    )
    return planner, provider


@pytest.fixture(name="planner")
def planner_fixture(tmp_path: Path) -> PlannerAgent:
    planner, _ = make_planner(tmp_path)
    return planner


def use_main(monkeypatch: pytest.MonkeyPatch, main: FakeMain | None) -> None:
    """Point the pipeline module's `find_main_actor` at a stand-in.

    Patched rather than registered, because the real lookup insists on a
    MainActor instance and building one would drag in the whole orchestrator.
    """
    monkeypatch.setattr(pipeline_mod, "find_main_actor", lambda _registry: main)


class TestSpawnPipelineStepOutcomes:
    """Every outcome is recorded, because main reports per-agent status from it."""

    async def test_a_nameless_step_is_skipped_without_a_record(self, planner: PlannerAgent) -> None:
        outcome = await planner._spawn_pipeline_step({"name": "  "}, "task")

        assert outcome.wired is None
        assert outcome.spawned is None
        assert not planner._spawn_results

    async def test_an_agent_already_running_is_reused_not_respawned(
        self, planner: PlannerAgent
    ) -> None:
        planner._registry = FakeRegistry("watcher")  # pyright: ignore[reportAttributeAccessIssue]

        outcome = await planner._spawn_pipeline_step({"name": "watcher"}, "task")

        assert outcome.spawned is None, "an already-running agent must not be spawned again"
        assert outcome.rule_agent == "watcher", "but it still belongs to the rule"
        assert planner._spawn_results["watcher"]["status"] == "already_running"

    async def test_a_step_with_no_spawn_config_is_recorded_as_such(
        self, planner: PlannerAgent
    ) -> None:
        outcome = await planner._spawn_pipeline_step({"name": "ghost"}, "task")

        assert outcome.wired is None
        assert planner._spawn_results["ghost"] == {
            "ok": False,
            "status": "no_config",
            "error": "missing spawn_config",
        }

    async def test_a_spawn_that_raises_is_reported_not_swallowed(
        self, planner: PlannerAgent
    ) -> None:
        async def _boom(config: dict[str, Any]) -> None:
            raise RuntimeError(SPAWN_ERROR)

        planner._spawn_agent = _boom  # pyright: ignore[reportAttributeAccessIssue]
        step = {"name": "cam", "spawn_config": {"type": "dynamic", "code": "pass"}}

        outcome = await planner._spawn_pipeline_step(step, "task")

        assert outcome.wired is not None
        assert SPAWN_ERROR in outcome.wired
        assert planner._spawn_results["cam"]["status"] == "spawn_failed"
        assert outcome.rule_agent is None, "a failed agent must not join the rule"

    async def test_a_spawn_returning_nothing_is_distinguished_from_a_crash(
        self, planner: PlannerAgent
    ) -> None:
        async def _none(config: dict[str, Any]) -> None:
            return None

        planner._spawn_agent = _none  # pyright: ignore[reportAttributeAccessIssue]
        step = {"name": "cam", "spawn_config": {"type": "dynamic", "code": "pass"}}

        await planner._spawn_pipeline_step(step, "task")

        assert planner._spawn_results["cam"]["status"] == "spawn_returned_none"

    async def test_a_successful_spawn_is_registered_for_restore(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without this a restart loses every agent the rule created."""
        main = FakeMain()
        use_main(monkeypatch, main)
        planner._registry = FakeRegistry()  # pyright: ignore[reportAttributeAccessIssue]

        async def _ok(config: dict[str, Any]) -> FakeActor:
            return FakeActor("watcher")

        planner._spawn_agent = _ok  # pyright: ignore[reportAttributeAccessIssue]
        step = {
            "name": "watcher",
            "description": "watches",
            "spawn_config": {"type": "dynamic", "code": "pass"},
            "mqtt_topics": [],
        }

        outcome = await planner._spawn_pipeline_step(step, "when the door opens")

        assert outcome.spawned == "watcher"
        assert planner._spawned_by_planner == ["watcher"]
        assert len(main.spawn_registry) == 1
        assert main.spawn_registry[0]["_rule"] is True
        assert main.spawn_registry[0]["_rule_task"] == "when the door opens"


class TestPipelineRulePersistence:
    async def test_the_rule_is_saved_on_main_not_the_planner(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The planner self-terminates, so a rule kept here would vanish with it."""
        main = FakeMain()
        use_main(monkeypatch, main)
        planner._registry = FakeRegistry()  # pyright: ignore[reportAttributeAccessIssue]

        planner._persist_pipeline_rule("when the door opens", ["watcher"])

        assert len(main.saved_rules) == 1
        assert main.saved_rules[0]["agents"] == ["watcher"]
        assert main.saved_rules[0]["rule_id"]

    async def test_no_main_means_nothing_is_saved_and_nothing_raises(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_main(monkeypatch, None)
        planner._registry = FakeRegistry()  # pyright: ignore[reportAttributeAccessIssue]

        planner._persist_pipeline_rule("task", ["watcher"])


class TestPipelineSummary:
    def test_nothing_spawned_says_so(self) -> None:
        assert "no agents could be spawned" in pipeline_summary([], [], "")

    def test_a_resolution_note_leads_the_summary(self) -> None:
        out = pipeline_summary(["**a** — does a"], ["a"], "topic `sensors/x`")

        assert out.splitlines()[0].endswith("topic `sensors/x`")

    def test_spawned_agents_are_named_with_the_restore_promise(self) -> None:
        out = pipeline_summary(["**a** — does a"], ["a"], "")

        assert "auto-restore on restart" in out
        assert "a" in out


class TestRunPipelineModes:
    """Which of the three modes a request takes."""

    @staticmethod
    def _plan(planner: PlannerAgent, plan: list[dict[str, Any]]) -> None:
        async def _decompose(task: str, workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return plan

        async def _resolve(task: str) -> tuple[str, str]:
            return task, ""

        planner._decompose_pipeline = _decompose  # pyright: ignore[reportAttributeAccessIssue]
        planner._resolve_data_references = _resolve  # pyright: ignore[reportAttributeAccessIssue]

    async def test_an_approved_plan_is_executed_without_planning_again(
        self, tmp_path: Path
    ) -> None:
        planner, llm = make_planner(
            tmp_path, approved_plan={"plan": [{"name": "watcher"}], "resolution_note": "note"}
        )
        executed: list[Any] = []

        async def _execute(plan: list[dict[str, Any]], task: str, resolution_note: str = "") -> str:
            executed.append((plan, resolution_note))
            return "done"

        planner._execute_pipeline_plan = _execute  # pyright: ignore[reportAttributeAccessIssue]

        result = await planner._run_pipeline("anything", [])

        assert result == "done"
        assert executed[0][1] == "note", "the approved envelope carries its own note"
        assert not llm.calls, "an approved plan must not re-plan"

    async def test_an_empty_approved_plan_says_so_rather_than_spawning(
        self, tmp_path: Path
    ) -> None:
        planner, _ = make_planner(tmp_path, approved_plan={"plan": []})

        assert "empty" in await planner._run_pipeline("anything", [])

    async def test_an_infeasible_request_is_refused_with_the_reason(self, tmp_path: Path) -> None:
        planner, _ = make_planner(tmp_path)
        self._plan(planner, [{"_feasibility_error": "no light entity exists"}])

        result = await planner._run_pipeline("turn on the lamp", [])

        assert "Cannot set up this pipeline" in result
        assert "no light entity exists" in result

    async def test_plan_only_returns_a_proposal_and_spawns_nothing(self, tmp_path: Path) -> None:
        planner, _ = make_planner(tmp_path, plan_only=True)
        self._plan(planner, [{"name": "watcher", "spawn_config": {"type": "dynamic"}}])
        spawned: list[Any] = []
        planner._execute_pipeline_plan = lambda *a, **k: spawned.append(a)  # pyright: ignore[reportAttributeAccessIssue]

        result = await planner._run_pipeline("when the door opens", [])
        envelope = json.loads(result)

        assert envelope["_plan_proposal"] is True
        assert envelope["plan"][0]["name"] == "watcher"
        assert not spawned, "plan_only spawned an agent"

    async def test_a_failed_decomposition_falls_back_to_a_direct_answer(
        self, tmp_path: Path
    ) -> None:
        planner, _ = make_planner(tmp_path)
        self._plan(planner, [])

        async def _answer(task: str) -> str:
            return "here is a direct answer"

        planner._llm_answer = _answer  # pyright: ignore[reportAttributeAccessIssue]

        assert await planner._run_pipeline("when the door opens", []) == "here is a direct answer"
