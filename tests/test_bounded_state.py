"""In-memory maps that grow from untrusted input must have a ceiling.

Three of them are fed by whatever reaches the broker or the monitor, and none
had a removal path outside an explicit delete or reset. A client publishing
under fresh agent ids therefore grows the dashboard's live-state map for as
long as it keeps publishing, on an endpoint with no authentication.

The bound has to survive the flood without being *used* by it: evicting by age
alone would let a burst of invented ids push out the agents actually running.
"""

import time
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.main_actor import MainActor
from wactorz.agents.monitor_agent import MonitorActor
from wactorz.web import events, runtime


@pytest.fixture(autouse=True)
def _restore_runtime() -> Any:
    agents = dict(runtime.state["agents"])
    yield
    runtime.state["agents"] = agents


class TestLiveAgentMap:
    def test_a_flood_of_unknown_ids_does_not_grow_it_without_bound(self) -> None:
        runtime.state["agents"] = {}

        for i in range(events.MAX_TRACKED_AGENTS * 3):
            events.update_agent(f"flood-{i}", "status", {"state": "running"})

        assert len(runtime.state["agents"]) <= events.MAX_TRACKED_AGENTS

    def test_a_live_agent_survives_the_flood(self) -> None:
        runtime.state["agents"] = {}
        events.update_agent("real-agent", "status", {"state": "running"})

        for i in range(events.MAX_TRACKED_AGENTS * 2):
            events.update_agent(f"flood-{i}", "status", {"state": "running"})
            if i % 50 == 0:
                # A running agent keeps heartbeating, which is what keeps it
                # off the stalest list. Evicting by age would drop it instead.
                events.update_agent("real-agent", "heartbeat", {"ts": time.time()})

        assert "real-agent" in runtime.state["agents"]

    def test_normal_operation_evicts_nothing(self) -> None:
        runtime.state["agents"] = {}

        for i in range(20):
            events.update_agent(f"agent-{i}", "status", {"state": "running"})

        assert len(runtime.state["agents"]) == 20


class TestPendingNotifications:
    """They drain only when someone chats, so an idle system never empties them."""

    def test_the_queue_stops_growing(self, tmp_path: Path) -> None:
        main = MainActor(llm_provider=None, persistence_dir=str(tmp_path))

        for i in range(main.MAX_PENDING_NOTIFICATIONS * 4):
            main._queue_notification({"severity": "warning", "message": f"n{i}"})

        assert len(main._pending_notifications) == main.MAX_PENDING_NOTIFICATIONS

    def test_the_newest_notices_are_the_ones_kept(self, tmp_path: Path) -> None:
        main = MainActor(llm_provider=None, persistence_dir=str(tmp_path))

        for i in range(main.MAX_PENDING_NOTIFICATIONS + 5):
            main._queue_notification({"severity": "warning", "message": f"n{i}"})

        messages = [n["message"] for n in main._pending_notifications]
        assert messages[-1] == f"n{main.MAX_PENDING_NOTIFICATIONS + 4}"
        assert "n0" not in messages


class _Registry:
    def __init__(self, actors: list[Any]) -> None:
        self._actors = actors

    def all_actors(self) -> list[Any]:
        return self._actors


class _Live:
    def __init__(self, actor_id: str) -> None:
        self.actor_id = actor_id


class TestMonitorTracking:
    @staticmethod
    def _monitor(tmp_path: Path, live: list[str]) -> MonitorActor:
        monitor = MonitorActor(persistence_dir=str(tmp_path))
        monitor._registry = _Registry([_Live(a) for a in live])  # type: ignore[assignment]
        return monitor

    def test_a_departed_actor_is_forgotten(self, tmp_path: Path) -> None:
        monitor = self._monitor(tmp_path, live=["still-here"])
        old = time.time() - monitor.heartbeat_timeout * 10
        monitor._last_seen = {"still-here": time.time(), "long-gone": old}
        monitor._alert_state = {"long-gone": True}
        monitor._last_notified = {"long-gone": old}
        monitor._error_registry = {"long-gone": {"name": "long-gone"}}

        monitor._forget_departed()

        assert "long-gone" not in monitor._last_seen
        assert monitor._alert_state == {}
        assert monitor._last_notified == {}
        # The registry's own cleanup only fires for a live actor reporting zero
        # errors, so an agent that departs while degraded is never cleared by
        # it — and it counts toward the `degraded` figure in system health.
        assert monitor._error_registry == {}

    def test_a_departed_agents_error_stops_counting_as_degraded(self, tmp_path: Path) -> None:
        monitor = self._monitor(tmp_path, live=["still-here"])
        old = time.time() - monitor.heartbeat_timeout * 10
        monitor._last_seen = {"still-here": time.time(), "gone": old}
        monitor._error_registry = {"gone": {"name": "gone"}, "still-here": {"name": "still-here"}}

        monitor._forget_departed()

        # A live agent's error is still a real one and must survive the sweep.
        assert list(monitor._error_registry) == ["still-here"]

    def test_a_registered_actor_is_kept_however_quiet(self, tmp_path: Path) -> None:
        monitor = self._monitor(tmp_path, live=["quiet"])
        monitor._last_seen = {"quiet": time.time() - monitor.heartbeat_timeout * 10}

        monitor._forget_departed()

        # Silence is what the monitor exists to notice — forgetting a silent
        # actor would drop the alert it is about to raise.
        assert "quiet" in monitor._last_seen

    def test_a_recently_seen_stranger_is_kept(self, tmp_path: Path) -> None:
        # Any sender counts as a liveness signal, including a remote agent that
        # is not in this process's registry. It gets a grace window to be
        # registered rather than being dropped on the next sweep.
        monitor = self._monitor(tmp_path, live=[])
        monitor._last_seen = {"remote": time.time()}

        monitor._forget_departed()

        assert "remote" in monitor._last_seen
