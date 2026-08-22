"""The planner's async paths: routing, scheduling, decomposition, caching.

Driven through a fake LLM and no broker, so these exercise the real methods
rather than a rehearsal of them. The scheduling tests stub `_execute_step`
alone -- what is under test there is which steps run and in what order, not
what an agent does when one reaches it.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.planner.agent import PlannerAgent
from wactorz.agents.planner.parsing import task_hash

BOOM = "agent exploded"

ONE_STEP_PLAN = json.dumps(
    [
        {
            "step": 1,
            "agent": "main",
            "task": "do the thing",
            "parallel": False,
            "depends_on": [],
            "spawn_config": None,
        }
    ]
)


def make_planner(tmp_path: Path, **kwargs: Any) -> tuple[PlannerAgent, FakeProvider]:
    """A planner with an isolated state dir, and the fake it was given.

    The provider is returned rather than read back off the agent, because
    `PlannerAgent.llm` is typed as an optional base provider and a test that
    wants the call log should not have to assert its way past that.
    """
    provider = FakeProvider(script=kwargs.pop("script", {}), intent=kwargs.pop("intent", "OTHER"))
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


class TestExecuteScheduling:
    """Which steps run, in what order, and what happens when they cannot."""

    @staticmethod
    def _record(planner: PlannerAgent) -> list[Any]:
        """Replace step execution with a recorder, keeping the scheduler real."""
        order: list[Any] = []

        async def _fake_step(step: dict[str, Any], prior: dict[str | int, Any]) -> dict:
            order.append(step["step"])
            return {"result": f"step {step['step']} done"}

        planner._execute_step = _fake_step  # pyright: ignore[reportAttributeAccessIssue]
        return order

    async def test_a_dependent_step_runs_after_what_it_needs(self, planner: PlannerAgent) -> None:
        order = self._record(planner)
        plan = [
            {"step": 1, "agent": "a", "task": "t", "parallel": False, "depends_on": []},
            {"step": 2, "agent": "b", "task": "t", "parallel": False, "depends_on": [1]},
        ]

        results = await planner._execute(plan)

        assert order == [1, 2]
        assert set(results) == {1, 2}

    async def test_parallel_steps_all_run(self, planner: PlannerAgent) -> None:
        order = self._record(planner)
        plan = [
            {"step": 1, "agent": "a", "task": "t", "parallel": True, "depends_on": []},
            {"step": 2, "agent": "b", "task": "t", "parallel": True, "depends_on": []},
        ]

        results = await planner._execute(plan)

        assert sorted(order) == [1, 2]
        assert set(results) == {1, 2}

    async def test_a_step_needing_a_step_that_does_not_exist_fails_loudly(
        self, planner: PlannerAgent
    ) -> None:
        """It can never become ready, so it must not stall the rest of the plan."""
        order = self._record(planner)
        plan = [
            {"step": 1, "agent": "a", "task": "t", "parallel": False, "depends_on": [99]},
            {"step": 2, "agent": "b", "task": "t", "parallel": False, "depends_on": []},
        ]

        results = await planner._execute(plan)

        assert order == [2], "the runnable step still had to run"
        assert "unsatisfiable dependency" in results[1]["error"]

    async def test_a_dependency_cycle_is_recorded_for_every_stuck_step(
        self, planner: PlannerAgent
    ) -> None:
        """Nothing in the cycle can run; the plan must not hang or drop it silently."""
        self._record(planner)
        plan = [
            {"step": 1, "agent": "a", "task": "t", "parallel": False, "depends_on": [2]},
            {"step": 2, "agent": "b", "task": "t", "parallel": False, "depends_on": [1]},
        ]

        results = await planner._execute(plan)

        assert set(results) == {1, 2}
        for step in (1, 2):
            assert "circular" in results[step]["error"]

    async def test_a_step_that_raises_becomes_an_error_entry(self, planner: PlannerAgent) -> None:
        """One failing parallel step must not take the whole gather down."""

        async def _boom(step: dict[str, Any], prior: dict[str | int, Any]) -> dict:
            if step["step"] == 1:
                raise RuntimeError(BOOM)
            return {"result": "ok"}

        planner._execute_step = _boom  # pyright: ignore[reportAttributeAccessIssue]
        plan = [
            {"step": 1, "agent": "a", "task": "t", "parallel": True, "depends_on": []},
            {"step": 2, "agent": "b", "task": "t", "parallel": True, "depends_on": []},
        ]

        results = await planner._execute(plan)

        assert BOOM in results[1]["error"]
        assert results[2] == {"result": "ok"}

    async def test_an_empty_plan_executes_nothing(self, planner: PlannerAgent) -> None:
        assert not await planner._execute([])


class TestDecompose:
    """Turning an LLM reply into a plan, or refusing to."""

    async def test_a_valid_json_array_becomes_a_plan(self, tmp_path: Path) -> None:
        planner, _ = make_planner(tmp_path, script={"do the thing": ONE_STEP_PLAN})

        plan = await planner._decompose("do the thing", [])

        assert len(plan) == 1
        assert plan[0]["agent"] == "main"

    async def test_a_fenced_reply_is_still_parsed(self, tmp_path: Path) -> None:
        """Models fence their JSON however often they are told not to."""
        planner, _ = make_planner(
            tmp_path, script={"do the thing": f"```json\n{ONE_STEP_PLAN}\n```"}
        )

        assert len(await planner._decompose("do the thing", [])) == 1

    async def test_an_unparseable_reply_yields_no_plan(self, tmp_path: Path) -> None:
        """The caller falls back to answering directly rather than guessing."""
        planner, _ = make_planner(tmp_path, script={"do the thing": "I cannot help with that."})

        assert not await planner._decompose("do the thing", [])

    async def test_decomposing_charges_the_planner(self, tmp_path: Path) -> None:
        """Cost has to accrue per call, or a runaway planner looks free."""
        planner, _ = make_planner(tmp_path, script={"do the thing": ONE_STEP_PLAN})

        await planner._decompose("do the thing", [])
        after_one = planner.total_cost_usd
        await planner._decompose("do the thing", [])

        assert after_one > 0
        assert planner.total_cost_usd > after_one


class TestRunPlanRouting:
    """Which path a task takes, which is where approval is enforced."""

    @staticmethod
    def _spy(planner: PlannerAgent) -> list[str]:
        taken: list[str] = []

        async def _pipeline(task: str, workers: list[dict]) -> str:
            taken.append("pipeline")
            return "pipeline-result"

        planner._run_pipeline = _pipeline  # pyright: ignore[reportAttributeAccessIssue]
        return taken

    async def test_a_pipeline_request_takes_the_pipeline_path(self, tmp_path: Path) -> None:
        planner, _ = make_planner(tmp_path)
        taken = self._spy(planner)

        result = await planner._run_plan("when the door opens send me a message")

        assert taken == ["pipeline"]
        assert result == "pipeline-result"

    async def test_a_one_shot_question_does_not(self, tmp_path: Path) -> None:
        planner, _ = make_planner(tmp_path, script={"capital of France": ONE_STEP_PLAN})
        taken = self._spy(planner)

        await planner._run_plan("what is the capital of France?")

        assert not taken

    async def test_an_approved_plan_goes_straight_to_the_pipeline(self, tmp_path: Path) -> None:
        """Approved plans are pipeline plans by construction, whatever the wording."""
        planner, _ = make_planner(tmp_path, approved_plan={"steps": []})
        taken = self._spy(planner)

        await planner._run_plan("what is the capital of France?")

        assert taken == ["pipeline"]

    async def test_plan_only_cannot_be_bypassed_by_a_one_shot_task(self, tmp_path: Path) -> None:
        """The one-shot path can spawn too, so plan_only must route for approval."""
        planner, _ = make_planner(tmp_path, plan_only=True)
        taken = self._spy(planner)

        await planner._run_plan("what is the capital of France?")

        assert taken == ["pipeline"], "plan_only silently bypassed approval"


class TestPlanCacheOnTheHotPath:
    """A planner is spawned per request, so the cache only pays off across instances."""

    async def test_a_cached_plan_is_reused_by_the_next_planner(self, tmp_path: Path) -> None:
        planner, _ = make_planner(tmp_path, script={"do the thing": ONE_STEP_PLAN})
        await planner._run_plan("do the thing")

        later, _ = make_planner(tmp_path, script={"do the thing": ONE_STEP_PLAN})
        await later._load_persistent_state()

        assert later._load_cached_plan(task_hash("do the thing"), []) is not None

    async def test_a_cache_hit_skips_asking_the_model_to_plan(self, tmp_path: Path) -> None:
        """The cache exists to save the decompose call; if it still asks, it saves nothing."""
        planner, _ = make_planner(tmp_path, script={"do the thing": ONE_STEP_PLAN})
        await planner._run_plan("do the thing")

        later, later_llm = make_planner(tmp_path, script={"do the thing": ONE_STEP_PLAN})
        await later._load_persistent_state()
        await later._run_plan("do the thing")

        asked_to_plan = [
            m
            for _system, msgs in later_llm.calls
            for m in msgs
            if "You are a task planner" in m.get("content", "")
        ]

        assert not asked_to_plan, "cache hit still sent the decompose prompt"

    async def test_a_different_task_is_not_served_from_the_cache(self, tmp_path: Path) -> None:
        planner, _ = make_planner(tmp_path, script={"do the thing": ONE_STEP_PLAN})
        await planner._run_plan("do the thing")

        later, _ = make_planner(tmp_path)
        await later._load_persistent_state()

        assert later._load_cached_plan(task_hash("something else entirely"), []) is None
