"""A migration in flight survives the process that started it.

Both pending maps were in memory only, so a restart dropped the token and both
halves of the choreography went quiet: an ack arriving afterwards found nothing
waiting and was ignored, and no rollback ever fired. The agent came back locally
from the spawn registry while the target might also be running it — the duplicate
the confirmation work exists to prevent, reached through the one door it left open.

The awkward part is `local_actor`: the stopped local instance, carried so the ack
can purge its persistence through the actor's own API. It cannot be written down,
and there is no by-name purge to fall back on — `_purge_local_agent_persistence`
uses the name for log lines only. What the ack can use instead is the copy
startup restarted, which has to be stopped anyway or the agent runs in two
places. Only when there is no such copy does the state stay behind.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import PENDING_MIGRATIONS_KEY, Migration
from wactorz.agents.main.nodes import NodeManager


class _LocalAgent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name}-id"
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _Registry:
    """The local actor registry — what startup restores agents into."""

    def __init__(self) -> None:
        self.by_name: dict[str, _LocalAgent] = {}

    def find_by_name(self, name: str) -> _LocalAgent | None:
        return self.by_name.get(name)

    async def unregister(self, actor_id: str) -> None:
        for name, agent in list(self.by_name.items()):
            if agent.actor_id == actor_id:
                del self.by_name[name]


class _Main:
    """Main with a persistence surface that survives being rebuilt."""

    def __init__(self, store: dict[str, Any] | None = None) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.migration = Migration(main, main.nodes)
        main._agent_manifests = {}
        self.registry = _Registry()
        setattr(main, "_registry", self.registry)

        self.store: dict[str, Any] = store if store is not None else {}
        self.published: list[tuple[str, Any]] = []
        self.spawned_local: list[dict[str, Any]] = []
        self.purged: list[str] = []

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            self.published.append((topic, payload))

        async def _spawn_remote(cfg: dict[str, Any], node: str, save: bool = False) -> None:
            return None

        async def _spawn_from_config(cfg: dict[str, Any], save: bool = False, **_kw: Any) -> None:
            self.spawned_local.append(cfg)

        async def _purge(actor: Any, name: str) -> None:
            self.purged.append(name)

        setattr(main, "persist", self.store.__setitem__)
        setattr(main, "recall", lambda key, default=None: self.store.get(key, default))
        setattr(main, "_mqtt_publish", _publish)
        setattr(main, "_spawn_remote", _spawn_remote)
        setattr(main, "_spawn_from_config", _spawn_from_config)
        setattr(main, "_purge_local_agent_persistence", _purge)
        setattr(main, "_get_spawn_registry", dict)
        setattr(main, "_save_to_spawn_registry", lambda _c: None)
        setattr(main, "_inject_llm_bridge_code", lambda cfg: cfg)
        setattr(main, "_queue_notification", lambda _n: None)
        self.actor = main
        self.migration = main.migration

    def restarted(self) -> "_Main":
        """A second main over the same store, as after a restart."""
        return _Main(self.store)

    def recorded(self) -> dict[str, Any]:
        return self.store.get(PENDING_MIGRATIONS_KEY, {})


def _spawn_entry(**over: Any) -> dict[str, Any]:
    entry = {
        "agent_name": "collector",
        "from_node": "rpi",
        "target_node": "nuc",
        "config": {"name": "collector", "node": "nuc"},
        "started_at": time.time(),
    }
    entry.update(over)
    return entry


class TestWhatIsWrittenDown:
    def test_a_pending_spawn_is_recorded(self) -> None:
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry()

        main.migration._save_pending()

        assert main.recorded()["spawns"]["tok"]["agent_name"] == "collector"

    def test_the_live_actor_is_not_recorded(self) -> None:
        # It cannot be serialised, and a restart could not use it anyway.
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry(local_actor=object())

        main.migration._save_pending()

        assert "local_actor" not in main.recorded()["spawns"]["tok"]

    def test_what_is_recorded_stays_json_shaped(self) -> None:
        # Stripped explicitly, because nothing downstream would refuse it:
        # `kv_set` serialises with `default=str` and would quietly store the
        # object's repr. This checks that the strip actually happens.
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry(local_actor=object())
        main.migration.pending_returns["ret"] = {"agent_name": "collector", "from_node": "rpi"}

        main.migration._save_pending()

        json.dumps(main.recorded())  # raises if anything unserialisable slipped in


class TestItComesBack:
    def test_a_pending_spawn_survives_a_restart(self) -> None:
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry()
        main.migration._save_pending()

        revived = main.restarted()
        revived.migration.restore()

        assert revived.migration.pending_spawns["tok"]["target_node"] == "nuc"

    def test_a_pending_return_survives_a_restart(self) -> None:
        main = _Main()
        main.migration.pending_returns["ret"] = {
            "agent_name": "collector",
            "from_node": "rpi",
            "started_at": time.time(),
        }
        main.migration._save_pending()

        revived = main.restarted()
        revived.migration.restore()

        assert revived.migration.pending_returns["ret"]["from_node"] == "rpi"

    async def test_an_ack_after_the_restart_completes_the_migration(self) -> None:
        # The point of the whole item: this ack used to be ignored.
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry()
        main.migration._save_pending()

        revived = main.restarted()
        revived.migration.restore()
        await revived.migration.receive_spawn_ack(
            "nodes/nuc/spawn_ack",
            json.dumps({"agent": "collector", "migration_token": "tok"}).encode(),
        )

        assert "tok" not in revived.migration.pending_spawns
        assert [t for t, _p in revived.published if t == "nodes/rpi/stop"]

    async def test_a_rollback_after_the_restart_still_fires(self) -> None:
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry(started_at=0.0)
        main.migration._save_pending()

        revived = main.restarted()
        revived.migration.restore()
        await revived.migration.expire_pending_spawns()

        assert "tok" not in revived.migration.pending_spawns
        assert [t for t, _p in revived.published if t == "nodes/nuc/stop"]

    def test_nothing_recorded_is_harmless(self) -> None:
        main = _Main()

        main.migration.restore()

        assert main.migration.pending_spawns == {}
        assert main.migration.pending_returns == {}

    def test_a_damaged_record_does_not_stop_startup(self) -> None:
        main = _Main()
        main.store[PENDING_MIGRATIONS_KEY] = "this is not a mapping"

        main.migration.restore()

        assert main.migration.pending_spawns == {}


class TestTheLocalLegAcrossARestart:
    """Local → remote, with main restarting between the spawn and the ack.

    The registry is not moved to the target until the ack, so startup sees a
    local agent and restarts it — before `restore` has even run. The ack then
    commits the move. Whatever startup brought back has to be stopped at that
    point, or the agent runs here and on the target for good.
    """

    async def test_the_copy_restarted_at_boot_is_stopped_on_the_ack(self) -> None:
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry(from_node="local")
        main.migration._save_pending()

        revived = main.restarted()
        restarted_copy = _LocalAgent("collector")
        revived.registry.by_name["collector"] = restarted_copy  # startup's doing
        revived.migration.restore()
        await revived.migration.receive_spawn_ack(
            "nodes/nuc/spawn_ack",
            json.dumps({"agent": "collector", "migration_token": "tok"}).encode(),
        )

        assert restarted_copy.stopped, "the agent would now run in two places"
        assert revived.registry.find_by_name("collector") is None
        assert revived.purged == ["collector"], "a stopped copy is what the purge needs"

    async def test_with_no_copy_running_the_state_is_left_in_place(self) -> None:
        # Nothing to stop and nothing to purge through: there is no by-name
        # purge to fall back on, so the stale state stays. The move still
        # completes, which is what matters.
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry(from_node="local")
        main.migration._save_pending()

        revived = main.restarted()
        revived.migration.restore()
        await revived.migration.receive_spawn_ack(
            "nodes/nuc/spawn_ack",
            json.dumps({"agent": "collector", "migration_token": "tok"}).encode(),
        )

        assert "tok" not in revived.migration.pending_spawns
        assert revived.purged == []


class TestAtStartup:
    async def test_a_move_that_timed_out_while_down_is_undone_straight_away(self) -> None:
        # The sweep used to wait a full interval before its first pass, so a
        # move that ran out its time during the downtime sat unresolved for up
        # to two and a half minutes after boot.
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry(started_at=0.0)
        main.migration._save_pending()

        revived = main.restarted()
        setattr(revived.actor, "state", SimpleNamespace(value="running"))
        revived.migration.restore()
        watcher = asyncio.create_task(revived.migration.stalled_migration_watcher())
        try:
            for _ in range(50):
                if "tok" not in revived.migration.pending_spawns:
                    break
                await asyncio.sleep(0)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

        assert "tok" not in revived.migration.pending_spawns
        assert [t for t, _p in revived.published if t == "nodes/nuc/stop"]


class TestEveryChangeIsWrittenDown:
    """The drift guard: nine call sites, and a missed one is silent.

    Asserted after each public entry point rather than by counting calls in the
    source, because the failure is a *divergence* — the record saying something
    the maps do not.
    """

    @staticmethod
    def _agrees(main: _Main) -> bool:
        recorded = main.recorded()
        spawns = {
            token: {k: v for k, v in entry.items() if k != "local_actor"}
            for token, entry in main.migration.pending_spawns.items()
        }
        return recorded.get("returns", {}) == main.migration.pending_returns and (
            recorded.get("spawns", {}) == spawns
        )

    async def test_after_placing_on_a_target(self) -> None:
        main = _Main()

        await main.migration._place_on_target("collector", "rpi", "nuc", {"name": "collector"}, {})

        assert self._agrees(main)

    async def test_after_an_ack(self) -> None:
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry()
        main.migration._save_pending()

        await main.migration.receive_spawn_ack(
            "nodes/nuc/spawn_ack",
            json.dumps({"agent": "collector", "migration_token": "tok"}).encode(),
        )

        assert self._agrees(main)

    async def test_after_a_rollback(self) -> None:
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry(started_at=0.0)
        main.migration._save_pending()

        await main.migration.expire_pending_spawns()

        assert self._agrees(main)

    async def test_after_a_return_expires(self) -> None:
        main = _Main()
        main.migration.pending_returns["ret"] = {
            "agent_name": "collector",
            "from_node": "rpi",
            "started_at": 0.0,
        }
        main.migration._save_pending()

        await main.migration.expire_pending_returns()

        assert self._agrees(main)

    async def test_the_guard_notices_a_change_that_was_not_written_down(self) -> None:
        # Without this, the four checks above would pass just as well against a
        # record nobody ever wrote.
        main = _Main()
        main.migration.pending_spawns["tok"] = _spawn_entry()

        assert not self._agrees(main)
