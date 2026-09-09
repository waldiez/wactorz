"""A migration snapshot instructs one spawn; it must not become a standing order.

The runner treats `_initial_state` as ground truth and deletes the agent's state
file to apply it. That is correct exactly once, on arrival. Left in a record the
runner reads again -- the spawn registry, or a node's retained desired state --
it is re-applied on every reconcile, so a node reboot rolls the agent back to the
moment it was migrated and discards everything since.
"""

from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawns import SpawnService, without_transient_keys

SNAPSHOT = {"conversation_history": ["hello"], "count": 7}


class _Host:
    """The persistence surface `SpawnService` writes through."""

    name = "main"

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def recall(self, key: str, default: Any = None) -> Any:
        return self.store.get(key, default)

    def persist(self, key: str, value: Any) -> None:
        self.store[key] = value


class _Main:
    """Enough of main to publish a desired state and record what went out."""

    def __init__(self) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.migration = Migration(main, main.nodes)

        self.published: list[tuple[str, Any]] = []
        self.host = _Host()
        # The real registry reader, so what a stored entry does to a published
        # desired state is exercised rather than stubbed past.
        spawns = SpawnService(self.host)  # type: ignore[arg-type]

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            self.published.append((topic, payload))

        setattr(main, "_mqtt_publish", _publish)
        setattr(main, "_get_spawn_registry", spawns._get_spawn_registry)
        setattr(main, "_inject_llm_bridge_code", lambda cfg: cfg)
        self.migration = main.migration

    def seed_legacy(self, **config: Any) -> None:
        """A registry entry as it was recorded before the snapshot was stripped."""
        self.host.store["_spawned_agents"] = {config["name"]: config}

    def desired_agents(self) -> list[dict[str, Any]]:
        topic, payload = next(t for t in self.published if t[0].endswith("/desired_state"))
        assert topic
        return payload["agents"]


@pytest.fixture(name="main")
def main_fixture() -> _Main:
    return _Main()


def test_the_helper_leaves_everything_else_alone() -> None:
    config = {"name": "collector", "node": "rpi", "code": "x", "_initial_state": SNAPSHOT}

    assert without_transient_keys(config) == {"name": "collector", "node": "rpi", "code": "x"}


def test_the_helper_does_not_mutate_what_it_is_given() -> None:
    # The caller still has to hand the snapshot to the spawn message.
    config = {"name": "collector", "_initial_state": SNAPSHOT}

    without_transient_keys(config)

    assert config["_initial_state"] == SNAPSHOT


def test_the_registry_records_the_agent_without_its_arrival_snapshot() -> None:
    host = _Host()
    spawns = SpawnService(host)  # type: ignore[arg-type]

    spawns._save_to_spawn_registry({"name": "collector", "node": "nuc", "_initial_state": SNAPSHOT})

    stored = host.store["_spawned_agents"]["collector"]
    assert "_initial_state" not in stored, "a restart would rewind the agent to this snapshot"
    assert stored["node"] == "nuc"


async def test_the_retained_desired_state_carries_no_snapshot(main: _Main) -> None:
    # This is the message the runner reconciles from after a reboot, so it is
    # the one that would re-apply the snapshot for as long as the agent lives.
    await main.migration.update_desired_state(
        "nuc", {"name": "collector", "node": "nuc", "_initial_state": SNAPSHOT}
    )

    assert main.desired_agents() == [{"name": "collector", "node": "nuc"}]


def test_an_entry_recorded_before_the_fix_reads_back_clean() -> None:
    # Upgrading does not rewrite the store, so the agents that already hit this
    # are the ones whose entries still carry a snapshot.
    host = _Host()
    host.store["_spawned_agents"] = {
        "collector": {"name": "collector", "node": "nuc", "_initial_state": SNAPSHOT}
    }
    spawns = SpawnService(host)  # type: ignore[arg-type]

    assert spawns._get_spawn_registry() == {"collector": {"name": "collector", "node": "nuc"}}


async def test_a_republish_does_not_carry_a_snapshot_left_by_an_older_version(main: _Main) -> None:
    main.seed_legacy(name="collector", node="nuc", _initial_state=SNAPSHOT)

    # Any republish at all -- this one removes an unrelated agent -- must not
    # hand the stale snapshot back to the node.
    await main.migration.update_desired_state("nuc", remove_name="other")

    assert main.desired_agents() == [{"name": "collector", "node": "nuc"}]
