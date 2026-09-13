"""A migration completes when the target says the agent started, not before.

The source stops the agent but keeps its state, so until the target confirms,
the agent still exists exactly where it was. That is what makes a failed
migration recoverable: rollback is a re-spawn on the source, not a repair.
"""

import time
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import SWEEP_INTERVAL_S, TOKEN_TTL_S, Migration
from wactorz.agents.main.nodes import NodeManager


class _Main:
    """The surface the ack path touches, recorded rather than performed."""

    def __init__(self) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.migration = Migration(main, main.nodes)

        self.published: list[tuple[str, Any]] = []
        self.saved: list[dict[str, Any]] = []
        self.spawned: list[tuple[dict[str, Any], str, bool]] = []
        self.notifications: list[dict[str, Any]] = []

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            self.published.append((topic, payload))

        async def _spawn_remote(cfg: dict[str, Any], node: str, save: bool = False) -> None:
            self.spawned.append((cfg, node, save))

        setattr(main, "_mqtt_publish", _publish)
        setattr(main, "_spawn_remote", _spawn_remote)
        setattr(main, "_save_to_spawn_registry", self.saved.append)
        setattr(main, "_get_spawn_registry", lambda: {"collector": {"name": "collector"}})
        setattr(main, "_inject_llm_bridge_code", lambda cfg: cfg)
        setattr(main, "_queue_notification", self.notifications.append)
        self.actor = main
        self.migration = main.migration

    def pending(self, *, age_s: float = 0.0, token: str = "tok") -> str:
        """A migration waiting for the target to confirm, started `age_s` ago."""
        self.migration.pending_spawns[token] = {
            "agent_name": "collector",
            "from_node": "rpi",
            "target_node": "nuc",
            "config": {"name": "collector", "node": "nuc", "_migration_token": token},
            "started_at": time.time() - age_s,
        }
        return token


def ack(token: str = "tok") -> bytes:
    import json

    return json.dumps({"agent": "collector", "migration_token": token, "node": "nuc"}).encode()


@pytest.fixture(name="main")
def main_fixture() -> _Main:
    return _Main()


class TestPlacingOnTheTarget:
    """Handing the agent to the target must not touch the source's copy.

    This is the whole of step 4: the source holds the only intact copy until the
    target confirms, so anything that deletes it earlier reopens the window
    where a dropped message loses the agent.
    """

    async def test_it_spawns_on_the_target_with_a_token(self, main: _Main) -> None:
        await main.migration._place_on_target(
            "collector", "rpi", "nuc", {"name": "collector", "code": "x"}, {"count": 7}
        )

        config, node, save = main.spawned[0]
        assert node == "nuc"
        assert config["_migration_token"]
        assert config["_initial_state"] == {"count": 7}
        assert save is False, "the registry must not move before the agent does"

    async def test_it_does_not_delete_the_source(self, main: _Main) -> None:
        await main.migration._place_on_target(
            "collector", "rpi", "nuc", {"name": "collector", "code": "x"}, {}
        )

        assert not [t for t, _ in main.published if t.endswith("/stop")]

    async def test_it_records_what_it_is_waiting_for(self, main: _Main) -> None:
        # Without this the timeout has nothing to roll back.
        await main.migration._place_on_target(
            "collector", "rpi", "nuc", {"name": "collector", "code": "x"}, {}
        )

        pending = list(main.migration.pending_spawns.values())
        assert len(pending) == 1
        assert pending[0]["from_node"] == "rpi"
        assert pending[0]["target_node"] == "nuc"


class TestTheAck:
    async def test_it_moves_the_registry_to_the_target(self, main: _Main) -> None:
        token = main.pending()

        await main.migration.receive_spawn_ack("nodes/nuc/spawn_ack", ack(token))

        assert main.saved and main.saved[0]["node"] == "nuc"

    async def test_it_rewrites_both_nodes_desired_state(self, main: _Main) -> None:
        # The source must forget the agent and the target must expect it, or one
        # of them re-spawns it after a restart.
        token = main.pending()

        await main.migration.receive_spawn_ack("nodes/nuc/spawn_ack", ack(token))

        # Asserted on what goes on the wire: each node reads its own retained
        # desired_state and reconciles from it.
        nodes = [t.split("/")[1] for t, _ in main.published if t.endswith("/desired_state")]
        assert nodes == ["rpi", "nuc"]

    async def test_only_then_is_the_source_told_to_delete(self, main: _Main) -> None:
        token = main.pending()

        await main.migration.receive_spawn_ack("nodes/nuc/spawn_ack", ack(token))

        assert ("nodes/rpi/stop", {"name": "collector", "delete": True}) in main.published

    async def test_the_internal_token_is_not_written_to_the_registry(self, main: _Main) -> None:
        token = main.pending()

        await main.migration.receive_spawn_ack("nodes/nuc/spawn_ack", ack(token))

        assert "_migration_token" not in main.saved[0]

    async def test_an_unknown_token_changes_nothing(self, main: _Main) -> None:
        # The ack carries authority to delete an agent's only remaining copy.
        main.pending()

        await main.migration.receive_spawn_ack("nodes/nuc/spawn_ack", ack("forged"))

        assert not main.saved
        assert not main.published

    async def test_a_replayed_ack_acts_once(self, main: _Main) -> None:
        # QoS 1 is at-least-once, so the same ack can arrive twice.
        token = main.pending()

        await main.migration.receive_spawn_ack("nodes/nuc/spawn_ack", ack(token))
        await main.migration.receive_spawn_ack("nodes/nuc/spawn_ack", ack(token))

        assert len(main.saved) == 1


class TestTheSweepHasATrigger:
    """The recovery runs on a timer, not when a message arrives.

    `state_return` and `spawn_ack` carry migration traffic and nothing else, so
    the failure being recovered from -- a node going away mid-migration --
    produces no message. Driven by the message loop alone the net would spring
    only on the next unrelated migration, which may never come.
    """

    def test_the_watcher_is_started_with_the_other_listeners(self) -> None:
        source = Path("wactorz/agents/main/actor.py").read_text(encoding="utf-8")

        assert "_stalled_migration_watcher()" in source, "nothing starts the sweep"

    def test_the_interval_is_shorter_than_the_timeout(self) -> None:
        # Otherwise a stalled migration waits an extra full timeout to be seen.
        assert SWEEP_INTERVAL_S < TOKEN_TTL_S

    async def test_the_sweep_covers_both_legs(self, main: _Main) -> None:
        # Leg one: asked to hand back, never did. Leg two: placed, never
        # confirmed. One sweep has to recover either.
        main.migration.pending_returns["t1"] = {
            "agent_name": "collector",
            "from_node": "rpi",
            "started_at": time.time() - TOKEN_TTL_S - 1,
        }
        main.pending(age_s=TOKEN_TTL_S + 1, token="t2")

        await main.migration.sweep_stalled_migrations()

        assert not main.migration.pending_returns
        assert not main.migration.pending_spawns
        assert len(main.spawned) == 2


class TestWhenTheHandBackNeverArrives:
    """Leg one: the source stopped the agent and never sent it.

    The source stops before publishing `state_return`, so a source that dies in
    between leaves the agent stopped, intact, and running nowhere. Forgetting
    the token is not enough.
    """

    def _stalled(self, main: _Main) -> None:
        main.migration.pending_returns["tok"] = {
            "agent_name": "collector",
            "from_node": "rpi",
            "started_at": time.time() - TOKEN_TTL_S - 1,
        }

    async def test_the_agent_is_restarted_where_it_was(self, main: _Main) -> None:
        self._stalled(main)

        await main.migration.expire_pending_returns()

        config, node, _save = main.spawned[0]
        assert node == "rpi"
        assert config["node"] == "rpi"

    async def test_a_hand_back_still_in_flight_is_left_alone(self, main: _Main) -> None:
        main.migration.pending_returns["tok"] = {
            "agent_name": "collector",
            "from_node": "rpi",
            "started_at": time.time(),
        }

        await main.migration.expire_pending_returns()

        assert not main.spawned
        assert main.migration.pending_returns


class TestWhenTheTargetNeverConfirms:
    async def test_the_agent_goes_back_to_the_source(self, main: _Main) -> None:
        # The source was stopped but not deleted, so putting it back is a
        # re-spawn rather than anything to reconstruct.
        main.pending(age_s=TOKEN_TTL_S + 1)

        await main.migration.expire_pending_spawns()

        assert main.spawned
        config, node, _save = main.spawned[0]
        assert node == "rpi"
        assert config["node"] == "rpi"

    async def test_the_source_is_not_told_to_delete(self, main: _Main) -> None:
        # It holds the only copy left. Named exactly: the rollback *does* stop
        # the target, so a check for any "/stop" would pass for the wrong reason.
        main.pending(age_s=TOKEN_TTL_S + 1)

        await main.migration.expire_pending_spawns()

        assert "nodes/rpi/stop" not in [t for t, _ in main.published]

    async def test_the_registry_still_points_at_the_source(self, main: _Main) -> None:
        main.pending(age_s=TOKEN_TTL_S + 1)

        await main.migration.expire_pending_spawns()

        assert main.spawned[0][0]["node"] == "rpi"

    async def test_a_migration_still_in_flight_is_left_alone(self, main: _Main) -> None:
        main.pending(age_s=1.0)

        await main.migration.expire_pending_spawns()

        assert not main.spawned
        assert main.migration.pending_spawns

    async def test_the_targets_copy_is_cleared_first(self, main: _Main) -> None:
        # The ack may have been lost rather than never sent, in which case the
        # target IS running the agent. Putting it back on the source without
        # clearing the target leaves two -- the duplicate this all exists to
        # avoid. An agent that never started ignores the stop.
        main.pending(age_s=TOKEN_TTL_S + 1)

        await main.migration.expire_pending_spawns()

        assert ("nodes/nuc/stop", {"name": "collector", "delete": True}) in main.published

    async def test_the_failure_is_announced(self, main: _Main) -> None:
        # An operator asked for this; silence would leave them guessing.
        main.pending(age_s=TOKEN_TTL_S + 1)

        await main.migration.expire_pending_spawns()

        assert any("failed" in str(n.get("message", "")).lower() for n in main.notifications)
