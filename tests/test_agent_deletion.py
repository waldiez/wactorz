"""Deleting an agent, and everything that has to go with it.

Delete is not a stronger stop. A stop keeps the state so the agent can resume;
delete removes every trace so a later spawn under the same name starts clean.
An agent whose state survives its deletion comes back holding someone else's
memory, which is why so much of this is about what gets cleared rather than
what gets stopped.

Three routes, and the branch is chosen before anything is torn down. A remote
agent's node does the work, told over MQTT. A local one is unregistered,
stopped and purged here. An agent in neither place still has its broker side
cleared, because a retained message from a previous incarnation outlives the
agent that published it.

The order inside each route matters. The node is looked up before the registry
entry is removed, and the actor id is derived before the actor is gone —
otherwise the remote agent takes the local branch and keeps running, or the
retained topics can no longer be addressed.

Clearing the broker from every side at once is deliberate: main, the runner and
the monitor all purge, so a delete completes even when one of them is down.
"""

import json
import uuid
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.lifecycle import LifecycleService
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawns import SpawnService

_STORE_LOCKED = "database is locked"


#: The scheme every side derives an agent's id from, so all three agree.
def actor_id_for(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.actor.{name}"))


class _Persistence:
    """The agent's own store, which knows how to erase itself."""

    def __init__(self, *, raises: bool = False) -> None:
        self.purged = 0
        self._raises = raises

    def purge(self) -> None:
        self.purged += 1
        if self._raises:
            raise RuntimeError(_STORE_LOCKED)


class _Agent:
    def __init__(
        self, name: str, persistence: _Persistence | None = None, pdir: Any = None
    ) -> None:
        self.name = name
        self.actor_id = actor_id_for(name)
        self.stopped = 0
        self._persistence_api = persistence
        self._persistence_dir = pdir

    async def stop(self) -> None:
        self.stopped += 1


class _Supervisor:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, name: str) -> None:
        self.released.append(name)


class _Registry:
    def __init__(self, agents: dict[str, _Agent], supervisor: _Supervisor | None = None) -> None:
        self._by = agents
        self.unregistered: list[str] = []
        self._supervisor_ref = supervisor

    def find_by_name(self, name: str) -> _Agent | None:
        return self._by.get(name)

    def all_actors(self) -> list[_Agent]:
        return list(self._by.values())

    async def unregister(self, actor_id: str) -> None:
        self.unregistered.append(actor_id)


class _Main:
    """A MainActor with the surface deletion touches, stubbed."""

    def __init__(
        self,
        *,
        local: dict[str, _Agent] | None = None,
        spawn_registry: dict[str, dict[str, Any]] | None = None,
        heartbeat_node: str = "",
        supervisor: _Supervisor | None = None,
        manifests: dict[str, Any] | None = None,
        topics: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.actor_id = "main-id"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.spawns = SpawnService(main)
        main.lifecycle = LifecycleService(main)
        main._agent_manifests = dict(manifests or {})
        main._topic_registry = dict(topics or {})
        main._conversation_history = []
        self.registry = _Registry(dict(local or {}), supervisor)
        setattr(main, "_registry", self.registry)

        self.published: list[tuple[str, Any, dict[str, Any]]] = []
        self.desired_state: list[tuple[str, Any]] = []
        self.removed_from_registry: list[str] = []
        self.deletions: list[tuple[str, str]] = []
        self._spawn_registry = dict(spawn_registry or {})

        async def _publish(topic: str, payload: Any, **kw: Any) -> None:
            self.published.append((topic, payload, kw))

        async def _desired(node: str, config: Any = None, remove_name: Any = None) -> None:
            self.desired_state.append((node, remove_name))

        def _record(name: str, reason: str = "user request") -> None:
            self.deletions.append((name, reason))

        setattr(main.spawns, "_get_spawn_registry", lambda: dict(self._spawn_registry))
        setattr(main.spawns, "_remove_from_spawn_registry", self.removed_from_registry.append)
        setattr(main, "_mqtt_publish", _publish)
        setattr(main, "_update_node_desired_state", _desired)
        setattr(main, "_node_running_agent", lambda _n: heartbeat_node)
        setattr(main.lifecycle, "_record_agent_deletion", _record)
        self.actor = main

    async def delete(self, name: str) -> None:
        await self.actor.delete_spawned_agent(name)

    def published_topics(self) -> list[str]:
        return [t for t, _, _ in self.published]

    def retained_purges(self) -> list[str]:
        """Topics cleared by an empty retained payload."""
        return [t for t, payload, kw in self.published if payload == b"" and kw.get("retain")]


class TestChoosingHowToDelete:
    """Which route is taken, decided before anything is torn down."""

    async def test_a_remote_agent_is_deleted_by_its_node(self) -> None:
        main = _Main(spawn_registry={"sensor": {"node": "rpi"}})

        await main.delete("sensor")

        assert ("nodes/rpi/stop", {"name": "sensor", "delete": True}, {"qos": 1}) in main.published

    async def test_the_node_is_told_to_delete_rather_than_stop(self) -> None:
        # The same topic carries both; only the flag says the state file and
        # the retained topics go too.
        main = _Main(spawn_registry={"sensor": {"node": "rpi"}})

        await main.delete("sensor")

        payload = next(p for t, p, _ in main.published if t == "nodes/rpi/stop")
        assert payload["delete"] is True

    async def test_the_node_stops_expecting_the_agent(self) -> None:
        # Left in the desired state, the runner reconciles it back into
        # existence after the next reboot.
        main = _Main(spawn_registry={"sensor": {"node": "rpi"}})

        await main.delete("sensor")

        assert main.desired_state == [("rpi", "sensor")]

    async def test_a_heartbeat_finds_a_node_the_registry_has_forgotten(self) -> None:
        # Without this the agent takes the local branch, nothing is sent to its
        # node, and it keeps running there forever.
        main = _Main(heartbeat_node="rpi")

        await main.delete("sensor")

        assert "nodes/rpi/stop" in main.published_topics()

    async def test_a_local_agent_is_stopped_here(self) -> None:
        agent = _Agent("collector")
        main = _Main(local={"collector": agent})

        await main.delete("collector")

        assert agent.stopped == 1
        assert main.registry.unregistered == [agent.actor_id]

    async def test_an_agent_nothing_knows_about_still_clears_the_broker(self) -> None:
        # A retained message outlives whatever published it, so the topics are
        # purged even with no agent to stop.
        main = _Main()

        await main.delete("ghost")

        assert main.retained_purges()
        assert main.deletions == [("ghost", "deleted (no live actor found)")]


class TestLeavingNothingBehind:
    """What a delete has to clear, on every route."""

    async def test_the_spawn_registry_entry_goes(self) -> None:
        # Left there, the agent is spawned again on the next restart.
        main = _Main(spawn_registry={"sensor": {"node": "rpi"}})

        await main.delete("sensor")

        assert main.removed_from_registry == ["sensor"]

    async def test_a_local_agents_persistence_is_purged(self) -> None:
        store = _Persistence()
        main = _Main(local={"collector": _Agent("collector", store)})

        await main.delete("collector")

        assert store.purged == 1

    async def test_the_supervisor_stops_holding_it(self) -> None:
        # Still held, its factory brings the agent back at the next restart.
        supervisor = _Supervisor()
        main = _Main(local={"collector": _Agent("collector")}, supervisor=supervisor)

        await main.delete("collector")

        assert supervisor.released == ["collector"]

    async def test_the_manifest_stops_being_reported(self) -> None:
        main = _Main(
            local={"collector": _Agent("collector")},
            manifests={"collector": {"name": "collector"}},
        )

        await main.delete("collector")

        assert not main.actor._agent_manifests

    async def test_the_agents_topics_are_forgotten(self) -> None:
        main = _Main(
            local={"collector": _Agent("collector")},
            topics={"sensors/temp": [{"name": "collector"}]},
        )

        await main.delete("collector")

        assert not main.actor._topic_registry

    async def test_a_topic_another_agent_publishes_survives(self) -> None:
        main = _Main(
            local={"collector": _Agent("collector")},
            topics={"sensors/temp": [{"name": "collector"}, {"name": "other"}]},
        )

        await main.delete("collector")

        assert main.actor._topic_registry == {"sensors/temp": [{"name": "other"}]}

    async def test_the_retained_manifest_is_cleared_at_the_broker(self) -> None:
        # Otherwise the agent is reloaded from the retained message on restart,
        # having been deleted.
        agent = _Agent("collector")
        main = _Main(local={"collector": agent})

        await main.delete("collector")

        assert f"agents/{agent.actor_id}/manifest" in main.retained_purges()

    async def test_every_per_agent_topic_is_cleared(self) -> None:
        agent = _Agent("collector")
        main = _Main(local={"collector": agent})

        await main.delete("collector")

        purged = main.retained_purges()
        for metric in ("status", "heartbeat", "metrics", "logs", "manifest", "errors"):
            assert f"agents/{agent.actor_id}/{metric}" in purged

    async def test_a_remote_agents_topics_are_cleared_from_here_too(self) -> None:
        # The runner purges them as well. Doing it from both sides is what makes
        # a delete complete while one side is offline.
        main = _Main(spawn_registry={"sensor": {"node": "rpi"}})

        await main.delete("sensor")

        assert f"agents/{actor_id_for('sensor')}/status" in main.retained_purges()


class TestDerivingTheIdBeforeItIsGone:
    """The topics stay addressable after the actor has been torn down."""

    async def test_a_remote_agents_id_follows_the_shared_scheme(self) -> None:
        # Main never saw the actor, so it derives the id the same way the runner
        # and the monitor do. A different answer would purge nothing.
        main = _Main(spawn_registry={"sensor": {"node": "rpi"}})

        await main.delete("sensor")

        assert f"agents/{actor_id_for('sensor')}/manifest" in main.retained_purges()

    async def test_a_live_actors_own_id_is_used(self) -> None:
        agent = _Agent("collector")
        agent.actor_id = "an-id-that-is-not-derived"
        main = _Main(local={"collector": agent})

        await main.delete("collector")

        assert "agents/an-id-that-is-not-derived/manifest" in main.retained_purges()


class TestWhenPurgingFails:
    """A store that will not erase must not leave the delete half-done."""

    async def test_a_failing_purge_falls_back_to_the_filesystem(self, tmp_path: Any) -> None:
        state_dir = tmp_path / "collector"
        state_dir.mkdir()
        (state_dir / "state.pkl").write_bytes(b"x")
        store = _Persistence(raises=True)
        main = _Main(local={"collector": _Agent("collector", store, pdir=state_dir)})

        await main.delete("collector")

        assert store.purged == 1
        assert not state_dir.exists()

    async def test_a_successful_purge_leaves_the_filesystem_alone(self, tmp_path: Any) -> None:
        # The store owns the erasing; going behind it would delete a directory
        # it may have already replaced.
        state_dir = tmp_path / "collector"
        state_dir.mkdir()
        main = _Main(local={"collector": _Agent("collector", _Persistence(), pdir=state_dir)})

        await main.delete("collector")

        assert state_dir.exists()

    async def test_an_agent_with_no_store_at_all_is_still_deleted(self) -> None:
        main = _Main(local={"collector": _Agent("collector")})

        await main.delete("collector")

        assert main.deletions == [("collector", "deleted")]


def delete_block(**cfg: Any) -> str:
    """A delete block as the model writes one."""
    return f"<delete>{json.dumps(cfg)}</delete>"


class TestDeleteBlocksTheModelWrote:
    """`<delete>` blocks, and the names main refuses to act on.

    The protected list is the guard that matters. Everything in it is
    housekeeping the system needs to keep working — deleting the catalogue or
    the installer leaves a running system with no way to spawn or deploy
    anything, and the request to do so arrives as ordinary model output.
    """

    def _main(self, **kw: Any) -> "_Main":
        return _Main(**kw)

    async def test_a_named_agent_is_deleted(self) -> None:
        main = self._main(local={"collector": _Agent("collector")})

        clean, deleted, missing = await main.actor._process_delete_commands(
            delete_block(name="collector")
        )

        assert deleted == ["collector"]
        assert not missing
        assert "<delete>" not in clean

    async def test_a_bare_name_is_accepted_too(self) -> None:
        # The model is given a forgiving format rather than one it has to get
        # exactly right.
        main = self._main(local={"collector": _Agent("collector")})

        _, deleted, _ = await main.actor._process_delete_commands("<delete>collector</delete>")

        assert deleted == ["collector"]

    async def test_the_agent_key_is_accepted_as_well_as_name(self) -> None:
        main = self._main(local={"collector": _Agent("collector")})

        _, deleted, _ = await main.actor._process_delete_commands(delete_block(agent="collector"))

        assert deleted == ["collector"]

    @pytest.mark.parametrize(
        "protected",
        ["main", "monitor", "installer", "catalog", "code-agent", "anomaly-detector"],
    )
    async def test_a_protected_agent_is_refused(self, protected: str) -> None:
        # Deleting the catalogue or the installer leaves a running system that
        # can no longer spawn or deploy anything.
        main = self._main(local={protected: _Agent(protected)})

        _, deleted, missing = await main.actor._process_delete_commands(
            delete_block(name=protected)
        )

        assert not deleted
        assert not missing
        assert not main.registry.unregistered

    async def test_an_unknown_name_is_reported_rather_than_ignored(self) -> None:
        # The reader asked for something to be deleted; silence would read as
        # having done it.
        main = self._main()

        _, deleted, missing = await main.actor._process_delete_commands(delete_block(name="ghost"))

        assert not deleted
        assert missing == ["ghost"]

    async def test_an_agent_only_in_the_spawn_registry_is_deletable(self) -> None:
        # It is not running, but it is persisted, so deleting it is meaningful:
        # otherwise it comes back on the next restart.
        main = self._main(spawn_registry={"sensor": {"node": "rpi"}})

        _, deleted, missing = await main.actor._process_delete_commands(delete_block(name="sensor"))

        assert deleted == ["sensor"]
        assert not missing

    async def test_an_agent_known_only_by_manifest_is_deletable(self) -> None:
        main = self._main(manifests={"remote-thing": {"name": "remote-thing"}})

        _, deleted, _ = await main.actor._process_delete_commands(delete_block(name="remote-thing"))

        assert deleted == ["remote-thing"]

    async def test_a_malformed_block_is_skipped(self) -> None:
        main = self._main()

        _, deleted, missing = await main.actor._process_delete_commands("<delete>{</delete>")

        assert not deleted
        assert not missing

    async def test_an_empty_block_deletes_nothing(self) -> None:
        main = self._main()

        _, deleted, missing = await main.actor._process_delete_commands("<delete></delete>")

        assert not deleted
        assert not missing

    async def test_every_block_is_read(self) -> None:
        main = self._main(local={"a": _Agent("a"), "b": _Agent("b")})

        _, deleted, _ = await main.actor._process_delete_commands(
            delete_block(name="a") + delete_block(name="b")
        )

        assert deleted == ["a", "b"]

    async def test_the_blocks_are_stripped_from_the_reply(self) -> None:
        main = self._main(local={"a": _Agent("a")})

        clean, _, _ = await main.actor._process_delete_commands(
            f"Removing that. {delete_block(name='a')} Done."
        )

        assert "<delete>" not in clean
        assert "Removing that." in clean
