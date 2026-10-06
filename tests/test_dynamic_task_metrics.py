"""A generated agent's tasks are counted by how they end, and its work is timed.

`handle_task` and `process()` carry everything a generated agent does. A task is
completed when it returns, failed when it raises, and failed and timed out when
it is still running at its timeout; a cancelled one is not counted, since that
is the agent being stopped. Each call is timed into Prometheus, and the recent
p50/p95 go into the agent's metrics frame, which is how a node's agents report.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.core.actor import ActorState
from wactorz.monitoring import agent_metrics
from wactorz.monitoring.agent_metrics import RecentDurations
from wactorz.monitoring.prometheus import PrometheusMonitor


def _agent(tmp_path: Path, handle_task: Any = None) -> DynamicAgent:
    # A name of its own: the histograms are shared by every test in the process.
    agent = DynamicAgent(
        name=f"probe-{uuid.uuid4().hex[:8]}", code="", persistence_dir=str(tmp_path)
    )
    agent._fn_handle_task = handle_task
    agent.state = ActorState.RUNNING
    return agent


def _task_count(agent: DynamicAgent, outcome: str) -> float:
    """How many of ``agent``'s tasks the histogram holds with ``outcome``."""
    for metric in agent_metrics.TASK_DURATION.collect():
        for sample in metric.samples:
            if (
                sample.name.endswith("_count")
                and sample.labels.get("agent") == agent.name
                and sample.labels.get("outcome") == outcome
            ):
                return sample.value
    return 0.0


async def _answers(_agent: Any, payload: Any) -> Any:
    return {"echo": payload}


async def _raises(_agent: Any, _payload: Any) -> Any:
    raise ValueError("bad input")


async def _never_returns(_agent: Any, _payload: Any) -> Any:
    await asyncio.Event().wait()


class TestCountingTasks:
    async def test_a_task_that_returns_is_completed(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, _answers)

        assert await agent._run_handle_task({"n": 1}) == {"echo": {"n": 1}}

        assert (agent.metrics.tasks_completed, agent.metrics.tasks_failed) == (1, 0)
        assert _task_count(agent, agent_metrics.COMPLETED) == 1

    async def test_a_task_that_raises_failed(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, _raises)

        # Boxed on the way out, so it crosses the task boundary intact.
        with pytest.raises(BaseException) as raised:
            await agent._run_handle_task({})

        assert isinstance(getattr(raised.value, "original", None), ValueError)

        assert (agent.metrics.tasks_completed, agent.metrics.tasks_failed) == (0, 1)
        assert agent.metrics.tasks_timed_out == 0
        assert _task_count(agent, agent_metrics.FAILED) == 1

    async def test_a_task_still_running_at_its_timeout_failed_and_timed_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path, _never_returns)
        monkeypatch.setattr(agent, "_HANDLE_TASK_TIMEOUT", 0.05)

        with pytest.raises(asyncio.TimeoutError):
            await agent._run_handle_task({})

        assert (agent.metrics.tasks_failed, agent.metrics.tasks_timed_out) == (1, 1)
        assert _task_count(agent, agent_metrics.TIMED_OUT) == 1

    async def test_a_cancelled_task_is_not_counted(self, tmp_path: Path) -> None:
        # Cancelled is the agent being stopped, not the task ending.
        agent = _agent(tmp_path, _never_returns)
        running = asyncio.create_task(agent._run_handle_task({}))
        await asyncio.sleep(0)

        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        assert (agent.metrics.tasks_completed, agent.metrics.tasks_failed) == (0, 0)


class TestTimingTheProcessLoop:
    async def test_a_cycle_is_timed(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)

        async def _cycle(_agent: Any) -> None:
            return None

        await agent._one_process_cycle(_cycle)

        assert "process_p50_s" in agent._build_metrics()

    async def test_a_cycle_that_runs_out_of_time_is_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path)
        monkeypatch.setattr(agent, "_PROCESS_TIMEOUT", 0.05)

        async def _stuck(_agent: Any) -> None:
            await asyncio.Event().wait()

        with pytest.raises(asyncio.TimeoutError):
            await agent._one_process_cycle(_stuck)

        assert agent_metrics.PROCESS_TIMEOUTS.labels(agent=agent.name)._value.get() == 1
        assert "process_p50_s" not in agent._build_metrics(), "a timeout is counted, not timed"


class TestTheMetricsFrame:
    async def test_it_carries_the_task_percentiles_once_there_are_tasks(
        self, tmp_path: Path
    ) -> None:
        agent = _agent(tmp_path, _answers)
        assert "task_p50_s" not in agent._build_metrics()

        await agent._run_handle_task({})
        frame = agent._build_metrics()

        assert {"task_p50_s", "task_p95_s"} <= frame.keys()
        assert frame["tasks_completed"] == 1
        assert frame["tasks_timed_out"] == 0


class TestRecentDurations:
    def test_percentiles_by_nearest_rank(self) -> None:
        recent = RecentDurations()
        for seconds in range(1, 101):
            recent.add(float(seconds))

        assert recent.summary("task") == {"task_p50_s": 50.0, "task_p95_s": 95.0}

    def test_only_the_latest_window_counts(self) -> None:
        recent = RecentDurations()
        for _ in range(agent_metrics.WINDOW):
            recent.add(100.0)
        for _ in range(agent_metrics.WINDOW):
            recent.add(1.0)

        assert recent.summary("t") == {"t_p50_s": 1.0, "t_p95_s": 1.0}


def _samples(text: str) -> dict[str, float]:
    """Every sample in a `/metrics` page, as `name{labels}` to its value."""
    samples = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            samples[name] = float(value)
    return samples


async def test_prometheus_serves_them_with_their_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(tmp_path, _never_returns)
    monkeypatch.setattr(agent, "_HANDLE_TASK_TIMEOUT", 0.05)
    with pytest.raises(asyncio.TimeoutError):
        await agent._run_handle_task({})

    samples = _samples(PrometheusMonitor(lambda: None).render().decode())

    labels = f'agent="{agent.name}",outcome="timed_out"'
    assert samples[f"wactorz_agent_task_duration_seconds_count{{{labels}}}"] == 1.0


def test_an_agents_series_go_when_it_stops(tmp_path: Path) -> None:
    # Names can be model-authored and one-off; kept for every agent ever run,
    # the series would grow for the life of the process.
    name = f"probe-{uuid.uuid4().hex[:8]}"
    agent_metrics.TASK_DURATION.labels(agent=name, outcome=agent_metrics.COMPLETED).observe(1)
    agent_metrics.PROCESS_TIMEOUTS.labels(agent=name).inc()

    agent_metrics.forget(name)

    text = PrometheusMonitor(lambda: None).render().decode()
    assert name not in text


async def test_stopping_an_agent_forgets_its_series(tmp_path: Path) -> None:
    agent = _agent(tmp_path, _answers)
    await agent._run_handle_task({})

    await agent.on_stop()

    assert agent.name not in PrometheusMonitor(lambda: None).render().decode()


def test_the_names_prometheus_serves(tmp_path: Path) -> None:
    text = PrometheusMonitor(lambda: None).render().decode()

    for family in (
        "wactorz_agent_task_duration_seconds",
        "wactorz_agent_process_duration_seconds",
        "wactorz_agent_process_timeouts_total",
        "wactorz_actor_tasks_timed_out_total",
    ):
        assert f"# TYPE {family} " in text, family
