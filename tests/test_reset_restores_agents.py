"""A reset must not leave the dashboard blank while the agents are running.

`reset all` stops everything except the kept agents — `main`, the monitor, the
installer, the catalog and the HA family are never stopped, only forgotten when
`state["agents"]` is cleared. The dashboard then has no way to ask what is
running: the list refills one heartbeat at a time, up to a full interval later,
so a deliberate operation looked like everything had disappeared.

The registry is authoritative and in memory, so it can answer immediately.
"""

import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wactorz.web import api_reset, events, runtime


def _actor(name: str, protected: bool = False, actor_id: str | None = None) -> Any:
    """An actor answering the two things the rebuild asks of it."""
    aid = actor_id or f"id-{name}"
    return types.SimpleNamespace(
        name=name,
        actor_id=aid,
        protected=protected,
        # The all-scope handler zeroes live counters on the way through.
        metrics=types.SimpleNamespace(messages_processed=0),
        total_cost_usd=0.0,
        total_input_tokens=0,
        total_output_tokens=0,
        _build_heartbeat=lambda: {
            "actor_id": aid,
            "name": name,
            "state": "running",
            "cpu": 1.5,
            "memory_mb": 32,
            "task": "idle",
            "protected": protected,
        },
    )


@pytest.fixture(name="clean_state")
def clean_state_fixture() -> Any:
    """Live state and the deleted-id list are module globals; isolate them."""
    agents = dict(runtime.state["agents"])
    deleted = list(runtime.deleted_agent_ids)
    registry = runtime.registry
    runtime.state["agents"].clear()
    runtime.deleted_agent_ids.clear()
    yield
    runtime.state["agents"].clear()
    runtime.state["agents"].update(agents)
    runtime.deleted_agent_ids.clear()
    runtime.deleted_agent_ids.extend(deleted)
    runtime.registry = registry


class TestRebuildFromRegistry:
    def test_it_restores_every_running_agent(self, clean_state: None) -> None:
        registry = MagicMock()
        registry.all_actors.return_value = [_actor("main", protected=True), _actor("worker")]

        assert events.rebuild_from_registry(registry) == 2
        assert sorted(a["name"] for a in runtime.state["agents"].values()) == ["main", "worker"]

    def test_the_entries_match_what_a_heartbeat_would_have_written(self, clean_state: None) -> None:
        # The rebuild goes through the same fold as the broker listener, so the
        # two cannot describe an agent differently.
        registry = MagicMock()
        registry.all_actors.return_value = [_actor("worker")]
        events.rebuild_from_registry(registry)

        entry = runtime.state["agents"]["id-worker"]
        assert entry["state"] == "running"
        assert entry["cpu"] == 1.5
        assert entry["mem"] == 32
        assert entry["task"] == "idle"

    def test_it_carries_the_protected_flag(self, clean_state: None) -> None:
        # `reset all` keeps an agent that is protected. An entry without the flag
        # reads as disposable, and the next reset tears down a system agent.
        registry = MagicMock()
        registry.all_actors.return_value = [_actor("main", protected=True), _actor("worker")]
        events.rebuild_from_registry(registry)

        assert runtime.state["agents"]["id-main"]["protected"] is True
        assert runtime.state["agents"]["id-worker"]["protected"] is False

    def test_one_bad_actor_does_not_lose_the_others(self, clean_state: None) -> None:
        bad = types.SimpleNamespace(name="bad", actor_id="id-bad")
        bad._build_heartbeat = MagicMock(side_effect=RuntimeError("no"))
        registry = MagicMock()
        registry.all_actors.return_value = [bad, _actor("worker")]

        assert events.rebuild_from_registry(registry) == 1
        assert "id-worker" in runtime.state["agents"]

    def test_no_registry_is_not_an_error(self, clean_state: None) -> None:
        assert events.rebuild_from_registry(None) == 0

    def test_a_deleted_agent_is_not_resurrected(self, clean_state: None) -> None:
        # Deletion is remembered so a late frame cannot re-admit an agent. The
        # rebuild must respect that rather than reinstating it from the registry.
        registry = MagicMock()
        registry.all_actors.return_value = [_actor("gone")]
        runtime.mark_deleted("id-gone")

        events.rebuild_from_registry(registry)

        assert "id-gone" not in runtime.state["agents"]


class TestResetRepublishesTheSurvivors:
    async def test_the_dashboard_is_told_who_is_still_running(self, clean_state: None) -> None:
        from tests.test_reset import _make_request  # reuse the request builder

        registry = MagicMock()
        registry.all_actors.return_value = [_actor("main", protected=True)]
        runtime.registry = registry
        broadcast = AsyncMock()

        with (
            patch("wactorz.web.ws.broadcast", new=broadcast),
            patch("wactorz.reset.reset_all"),
            patch("wactorz.web.lifecycle.purge_agent_retained", new=AsyncMock()),
        ):
            resp = await api_reset.reset_handler(_make_request({"scope": "all"}))

        assert resp.status == 200
        # One frame: the reset carries the survivors itself, rebuilt from the
        # registry before the broadcast — the client never sits on an empty
        # list waiting for heartbeats.
        kinds = [c.args[0].get("type") for c in broadcast.await_args_list]
        assert kinds == ["reset"]
        assert [a["name"] for a in broadcast.await_args_list[-1].args[0]["state"]["agents"]] == [
            "main"
        ]
