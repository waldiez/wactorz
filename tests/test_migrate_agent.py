"""Moving a running agent to a different machine.

Three things can know where an agent currently is, and they are consulted in
order of how much they know: the spawn registry (which holds the code), the
agent's manifest (which does not), and the live heartbeats (which only name a
node). Migration works from whichever answers first.

Which direction the move goes decides how it is done. Between two nodes, main
tells the source node to hand the agent over. Coming home, main cannot be told
to spawn something it has no code for, so it asks the source to send everything
back — that is the `@main` sentinel, and the reply arrives on the state-return
path. Going out from local, main already has the config and sends it.

The refusals matter as much as the moves: migrating to a node that is not
listening loses the agent, so the checks that come first are pinned first.
"""

import json
import time
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawns import SpawnService


class _Supervisor:
    """Records the entries it is told to forget."""

    def __init__(self) -> None:
        self.dropped: list[str] = []

    def drop_supervised(self, name: str) -> None:
        self.dropped.append(name)


class _Registry:
    """The actor registry, holding whichever agents are alive locally."""

    def __init__(self, names: tuple[str, ...] = ()) -> None:
        self._by = {n: _LocalAgent(n) for n in names}
        self._supervisor_ref: _Supervisor | None = None

    def find_by_name(self, name: str) -> "_LocalAgent | None":
        return self._by.get(name)

    async def unregister(self, actor_id: str) -> None:
        for name, agent in list(self._by.items()):
            if agent.actor_id == actor_id:
                del self._by[name]


class _Persistence:
    """An agent's persisted keys, as `PersistenceAPI.all()` returns them."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def all(self) -> dict[str, Any]:
        return dict(self.values)


class _LocalAgent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name}-id"
        self._persistence_api: _Persistence | None = None
        self.stopped = False
        #: Keys the agent writes while stopping, as an `on_stop` would.
        self.writes_on_stop: dict[str, Any] = {}

    def holding(self, **values: Any) -> "_LocalAgent":
        self._persistence_api = _Persistence(dict(values))
        return self

    async def stop(self) -> None:
        if self._persistence_api is not None:
            self._persistence_api.values.update(self.writes_on_stop)
        self.stopped = True


class _Main:
    """A MainActor with the surface `migrate_agent` touches, stubbed."""

    def __init__(
        self,
        *,
        spawn_registry: dict[str, dict[str, Any]] | None = None,
        manifests: dict[str, dict[str, Any]] | None = None,
        nodes: dict[str, dict[str, Any]] | None = None,
        local: tuple[str, ...] = (),
    ) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.spawns = SpawnService(main)
        main.migration = Migration(main, main.nodes)
        main.nodes.known = dict(nodes or {})
        main._agent_manifests = dict(manifests or {})
        self.registry = _Registry(local)
        setattr(main, "_registry", self.registry)

        self.published: list[tuple[str, Any]] = []
        self.publish_options: list[dict[str, Any]] = []
        self.saved: list[dict[str, Any]] = []
        self.desired_state: list[tuple[str, Any, Any]] = []
        self.spawned_remote: list[tuple[dict[str, Any], str, bool]] = []
        self.spawned_local: list[dict[str, Any]] = []
        self.purged: list[tuple[Any, str]] = []
        self.notifications: list[dict[str, Any]] = []
        self.recorded: dict[str, Any] = {}
        self._spawn_registry = dict(spawn_registry or {})

        async def _publish(topic: str, payload: Any, **kw: Any) -> None:
            self.published.append((topic, payload))
            self.publish_options.append(kw)

        async def _desired(node: str, cfg: Any = None, remove_name: Any = None) -> None:
            self.desired_state.append((node, cfg, remove_name))

        async def _spawn_remote(cfg: dict[str, Any], node: str, save: bool = False) -> None:
            self.spawned_remote.append((cfg, node, save))

        async def _spawn_from_config(cfg: dict[str, Any], save: bool = False, **_kw: Any) -> None:
            self.spawned_local.append(cfg)

        async def _purge(actor: Any, name: str) -> None:
            self.purged.append((actor, name))

        setattr(main, "_mqtt_publish", _publish)
        setattr(main, "_update_node_desired_state", _desired)
        setattr(main, "_get_spawn_registry", lambda: dict(self._spawn_registry))
        setattr(main, "_save_to_spawn_registry", self.saved.append)
        setattr(main, "_spawn_remote", _spawn_remote)
        setattr(main, "_spawn_from_config", _spawn_from_config)
        setattr(main, "_purge_local_agent_persistence", _purge)
        setattr(main, "_queue_notification", self.notifications.append)
        setattr(main, "persist", self.recorded.__setitem__)
        self.actor = main

    async def migrate(self, agent: str, target: str, *, force: bool = False) -> dict[str, Any]:
        return await self.actor.migrate_agent(agent, target, force=force)

    def local(self, name: str = "collector") -> _LocalAgent:
        agent = self.registry.find_by_name(name)
        assert agent is not None
        return agent

    def published_to(self, suffix: str) -> list[tuple[str, Any]]:
        return [(t, p) for t, p in self.published if t.endswith(suffix)]


def online(agents: tuple[str, ...] = ()) -> dict[str, Any]:
    """A node heard from just now."""
    return {"last_seen": time.time(), "agents": list(agents)}


def silent(agents: tuple[str, ...] = ()) -> dict[str, Any]:
    """A node that has stopped reporting."""
    return {"last_seen": time.time() - 3600, "agents": list(agents)}


def with_code(node: str = "", **over: Any) -> dict[str, Any]:
    return {"name": "collector", "code": "print(1)", "node": node, **over}


class TestRefusingTheMove:
    """Checked before anything is published — a bad move loses the agent."""

    async def test_an_agent_nothing_knows_about_is_refused(self) -> None:
        main = _Main()

        result = await main.migrate("ghost", "rpi")

        assert result["success"] is False
        assert "not found anywhere" in result["message"]

    async def test_an_agent_already_on_the_target_is_refused(self) -> None:
        main = _Main(spawn_registry={"collector": with_code(node="rpi")})

        result = await main.migrate("collector", "rpi")

        assert result["success"] is False
        assert "already on" in result["message"]

    async def test_a_local_agent_asked_to_stay_local_is_refused(self) -> None:
        main = _Main(local=("collector",))

        result = await main.migrate("collector", "")

        assert result["success"] is False
        assert "already on" in result["message"]

    async def test_an_unknown_target_node_is_refused(self) -> None:
        main = _Main(spawn_registry={"collector": with_code(node="rpi")}, nodes={"rpi": online()})

        result = await main.migrate("collector", "nowhere")

        assert result["success"] is False

    async def test_a_silent_target_node_is_refused(self) -> None:
        # It exists but has stopped reporting; sending the agent there loses it.
        main = _Main(
            spawn_registry={"collector": with_code(node="rpi")},
            nodes={"rpi": online(), "nuc": silent()},
        )

        result = await main.migrate("collector", "nuc")

        assert result["success"] is False

    async def test_a_refusal_publishes_nothing(self) -> None:
        main = _Main(
            spawn_registry={"collector": with_code(node="rpi")},
            nodes={"rpi": online(), "nuc": silent()},
        )

        await main.migrate("collector", "nuc")

        assert not main.published

    async def test_the_refusal_names_the_nodes_that_would_work(self) -> None:
        main = _Main(
            spawn_registry={"collector": with_code(node="rpi")},
            nodes={"rpi": online(), "nuc": silent(), "pi2": online()},
        )

        result = await main.migrate("collector", "nuc")

        assert "pi2" in result["message"]


class TestFindingWhereTheAgentIs:
    """Registry, then manifest, then heartbeats — most informative first."""

    async def test_the_registry_is_consulted_first(self) -> None:
        main = _Main(
            spawn_registry={"collector": with_code(node="rpi")},
            manifests={"collector": {"node": "stale-node"}},
            nodes={"rpi": online(), "nuc": online()},
        )

        await main.migrate("collector", "nuc")

        assert main.published_to("/migrate")[0][0] == "nodes/rpi/migrate"

    async def test_the_manifest_answers_when_the_registry_does_not(self) -> None:
        main = _Main(
            manifests={"collector": {"node": "rpi"}},
            nodes={"rpi": online(), "nuc": online()},
        )

        await main.migrate("collector", "nuc")

        assert main.published_to("/migrate")[0][0] == "nodes/rpi/migrate"

    async def test_a_heartbeat_answers_when_neither_does(self) -> None:
        # Nothing recorded it, but a node is reporting it right now.
        main = _Main(nodes={"rpi": online(("collector",)), "nuc": online()})

        await main.migrate("collector", "nuc")

        assert main.published_to("/migrate")[0][0] == "nodes/rpi/migrate"


class TestBetweenTwoNodes:
    """Node-to-node migration is routed through main, in two legs.

    The source used to publish `nodes/{target}/spawn` itself. That is lateral
    remote code execution -- generated code on one node placing code on another
    -- and a node holding a signing key refuses a spawn main did not sign. So main
    asks the source to hand the agent back, then places it on the target itself.
    """

    def _main(self) -> _Main:
        return _Main(
            spawn_registry={"collector": with_code(node="rpi")},
            nodes={"rpi": online(), "nuc": online()},
        )

    async def test_the_source_hands_the_agent_back_to_main(self) -> None:
        main = self._main()

        await main.migrate("collector", "nuc")

        topic, payload = main.published_to("/migrate")[0]
        assert topic == "nodes/rpi/migrate"
        assert payload["target_node"] == "@main", "the source should not address the target"
        assert payload["return_token"]

    async def test_the_source_is_never_asked_to_write_the_targets_topics(self) -> None:
        # The whole point of the routing change: nothing tells one node to
        # publish into another node's namespace.
        main = self._main()

        await main.migrate("collector", "nuc")

        assert not [t for t, _ in main.published if t.startswith("nodes/nuc/")]

    async def test_the_hand_over_is_durable(self) -> None:
        # Sent while the source may be reconnecting; at QoS 0 it would be lost
        # and the migration would hang with the agent still on the source.
        main = self._main()

        await main.migrate("collector", "nuc")

        assert main.publish_options[0].get("qos") == 1

    async def test_nothing_is_committed_before_the_agent_has_moved(self) -> None:
        # The registry and both nodes' desired state used to be written the
        # moment the command went out, so a migration that never completed left
        # them claiming the agent had moved. Now they move on the ack.
        main = self._main()

        await main.migrate("collector", "nuc")

        assert not main.saved, "the registry moved before the agent did"
        assert not main.published_to("/desired_state")

    async def test_the_migration_is_recorded_as_pending(self) -> None:
        main = self._main()

        await main.migrate("collector", "nuc")

        pending = list(main.actor.migration.pending_returns.values())
        assert len(pending) == 1
        assert pending[0]["target_node"] == "nuc"
        assert pending[0]["from_node"] == "rpi"


class TestComingHome:
    """Main cannot spawn what it has no code for, so it asks for everything."""

    def _main(self, **over: Any) -> _Main:
        return _Main(
            spawn_registry={"collector": {"name": "collector", "node": "rpi"}},
            nodes={"rpi": online()},
            **over,
        )

    async def test_the_source_is_asked_to_send_it_back(self) -> None:
        main = self._main()

        await main.migrate("collector", "")

        topic, payload = main.published_to("/migrate")[0]
        assert topic == "nodes/rpi/migrate"
        assert payload["target_node"] == "@main"

    async def test_a_return_token_is_issued(self) -> None:
        main = self._main()

        await main.migrate("collector", "")

        assert main.published_to("/migrate")[0][1]["return_token"]

    async def test_the_token_is_remembered_so_the_reply_is_recognised(self) -> None:
        # The state-return listener acts on the config in the reply, and only a
        # message quoting a token main issued is acted on.
        main = self._main()

        await main.migrate("collector", "")

        token = main.published_to("/migrate")[0][1]["return_token"]
        assert token in main.actor.migration.pending_returns

    async def test_the_remembered_token_names_where_it_is_coming_from(self) -> None:
        main = self._main()

        await main.migrate("collector", "")

        waiting = next(iter(main.actor.migration.pending_returns.values()))
        assert waiting["from_node"] == "rpi"
        assert waiting["agent_name"] == "collector"

    async def test_the_request_is_sent_reliably(self) -> None:
        # At-least-once, unlike the node-to-node command: a dropped request
        # strands the agent on its node with main waiting for a reply that
        # will never come, and the token expiring is the only way out.
        main = self._main()

        await main.migrate("collector", "")

        assert main.publish_options[0].get("qos") == 1

    async def test_the_node_to_node_command_is_durable_too(self) -> None:
        # This used to assert the opposite, and said the difference was
        # deliberate: coming home went out at QoS 1, node-to-node at QoS 0.
        # It was a defect either way -- a dropped migrate leaves the agent on
        # the source while main waits -- and both paths are the same command
        # now, so the asymmetry is gone rather than tidied away.
        main = _Main(
            spawn_registry={"collector": with_code(node="rpi")},
            nodes={"rpi": online(), "nuc": online()},
        )

        await main.migrate("collector", "nuc")

        assert main.publish_options[0].get("qos") == 1

    async def test_it_reports_that_it_is_waiting(self) -> None:
        main = self._main()

        result = await main.migrate("collector", "")

        assert result["success"] is True
        assert "waiting" in result["message"]


class TestGoingOut:
    """Local → remote, which has the same two copies to reconcile as the rest.

    The local state is this leg's equivalent of the source node's copy: it is
    the only intact one until the target says the agent started, so nothing may
    delete it before then.
    """

    @staticmethod
    def _main() -> _Main:
        return _Main(
            spawn_registry={"collector": with_code()},
            nodes={"nuc": online()},
            local=("collector",),
        )

    async def _migrated(self) -> tuple[_Main, str, dict[str, Any]]:
        main = self._main()
        await main.migrate("collector", "nuc")
        token, pending = next(iter(main.actor.migration.pending_spawns.items()))
        return main, token, pending

    async def test_the_agent_is_handed_over_with_a_token(self) -> None:
        main, token, _pending = await self._migrated()

        config, node, save = main.spawned_remote[0]
        assert node == "nuc"
        assert config["_migration_token"] == token
        assert save is False, "the registry must not move before the agent does"

    async def test_the_local_state_is_kept_until_the_target_confirms(self) -> None:
        main, _token, _pending = await self._migrated()

        assert main.purged == [], "purging here loses the agent if the spawn fails"

    async def test_the_local_instance_is_stopped_all_the_same(self) -> None:
        # Stopped, not deleted: two copies running would both answer.
        main = self._main()
        agent = main.registry.find_by_name("collector")

        await main.migrate("collector", "nuc")

        assert agent is not None and agent.stopped

    async def test_the_local_instance_leaves_supervision(self) -> None:
        # Stopped but still in its entry, it would be stopped a second time at
        # shutdown, running its on_stop and saves again. A rollback spawns it
        # afresh, which gives it an entry of its own.
        main = self._main()
        supervisor = _Supervisor()
        main.registry._supervisor_ref = supervisor

        await main.migrate("collector", "nuc")

        assert supervisor.dropped == ["collector"]

    async def test_the_migration_is_recorded_so_it_can_be_rolled_back(self) -> None:
        _main, _token, pending = await self._migrated()

        assert pending["from_node"] == "local"
        assert pending["target_node"] == "nuc"
        assert pending["local_actor"] is not None

    async def test_the_ack_purges_the_local_copy(self) -> None:
        main, token, _pending = await self._migrated()

        await main.actor.migration.receive_spawn_ack(
            "nodes/nuc/spawn_ack",
            json.dumps({"agent": "collector", "migration_token": token}).encode(),
        )

        assert [name for _actor, name in main.purged] == ["collector"]

    async def test_the_ack_does_not_publish_a_desired_state_for_local(self) -> None:
        # There is no node called "local" to reconcile.
        main, token, _pending = await self._migrated()

        await main.actor.migration.receive_spawn_ack(
            "nodes/nuc/spawn_ack",
            json.dumps({"agent": "collector", "migration_token": token}).encode(),
        )

        assert [t for t, _p in main.published_to("/desired_state")] == ["nodes/nuc/desired_state"]

    async def test_a_target_that_never_confirms_brings_the_agent_home(self) -> None:
        main, token, _pending = await self._migrated()
        main.actor.migration.pending_spawns[token]["started_at"] = 0.0

        await main.actor.migration.expire_pending_spawns()

        assert main.spawned_local, "the agent must be started here again"
        assert main.purged == [], "the copy it comes back to must still be there"

    async def test_a_rollback_withdraws_the_agent_from_the_target(self) -> None:
        # The stop is transient; the desired state is retained. Leaving the
        # agent in it means the target's next reboot spawns a second copy
        # beside the one that was just restored here.
        main, token, _pending = await self._migrated()
        main.actor.migration.pending_spawns[token]["started_at"] = 0.0

        await main.actor.migration.expire_pending_spawns()

        published = main.published_to("/desired_state")
        assert published, "the target was never told to forget the agent"
        _topic, payload = published[-1]
        assert [a["name"] for a in payload["agents"]] == []

    async def test_the_stop_that_undoes_the_spawn_is_durable(self) -> None:
        # The spawn is no longer retained, so this is what corrects a node that
        # was away while the migration was rolled back: it has to be queued
        # behind that spawn, which means surviving the same absence.
        main, token, _pending = await self._migrated()
        main.actor.migration.pending_spawns[token]["started_at"] = 0.0

        await main.actor.migration.expire_pending_spawns()

        stops = [
            options
            for (topic, _payload), options in zip(main.published, main.publish_options, strict=True)
            if topic == "nodes/nuc/stop"
        ]
        assert stops, "the target was never told to drop the agent"
        assert all(o.get("qos") == 1 for o in stops)

    async def test_what_comes_home_carries_no_stale_snapshot(self) -> None:
        # Local state was never purged, so it is both intact and newer than the
        # snapshot that was shipped out.
        main, token, _pending = await self._migrated()
        main.actor.migration.pending_spawns[token]["started_at"] = 0.0

        await main.actor.migration.expire_pending_spawns()

        restored = main.spawned_local[0]
        assert "_initial_state" not in restored
        assert "node" not in restored
        assert restored["replace"] is True


class TestTheStateThatGoesOut:
    """What a local agent takes with it, and when it is refused instead."""

    @staticmethod
    def _main(**values: Any) -> _Main:
        main = TestGoingOut._main()
        main.local().holding(**values)
        return main

    @staticmethod
    def shipped(main: _Main) -> dict[str, Any]:
        (config, _node, _save), *_ = main.spawned_remote
        return config.get("_initial_state", {})

    async def test_what_the_agent_writes_while_stopping_goes_with_it(self) -> None:
        # The local copy is purged once the target confirms, so a write left
        # there -- an LLM agent's last turn, a final counter -- is gone.
        main = self._main(turns=1)
        main.local().writes_on_stop = {"turns": 2}

        await main.migrate("collector", "nuc")

        assert self.shipped(main) == {"turns": 2}

    async def test_state_that_cannot_travel_keeps_the_agent_here(self) -> None:
        main = self._main(count=3, capture=object())
        agent = main.local()

        result = await main.migrate("collector", "nuc")

        assert result["success"] is False
        assert "capture" in result["message"] and "--force" in result["message"]
        assert not agent.stopped
        assert not main.spawned_remote
        assert not main.actor.migration.pending_spawns

    async def test_a_forced_move_ships_the_rest(self) -> None:
        main = self._main(count=3, capture=object())

        result = await main.migrate("collector", "nuc", force=True)

        assert result["success"] is True
        assert self.shipped(main) == {"count": 3}

    async def test_what_a_forced_move_left_is_named_when_it_completes(self) -> None:
        main = self._main(count=3)
        main.local().writes_on_stop = {"capture": object()}
        await main.migrate("collector", "nuc")
        token = next(iter(main.actor.migration.pending_spawns))

        await main.actor.migration.receive_spawn_ack(
            "nodes/nuc/spawn_ack",
            json.dumps({"agent": "collector", "migration_token": token}).encode(),
        )

        (notice,) = main.notifications
        assert notice["severity"] == "warning"
        assert "capture" in notice["message"]

    async def test_state_over_the_limit_keeps_the_agent_here(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wactorz.agents.main.migration.MIGRATION_MAX_STATE_BYTES", 100)
        main = self._main(history="x" * 200)

        result = await main.migrate("collector", "nuc", force=True)

        assert result["success"] is False
        assert "limit" in result["message"]
        assert not main.local().stopped

    async def test_growing_over_the_limit_while_stopping_starts_it_here_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wactorz.agents.main.migration.MIGRATION_MAX_STATE_BYTES", 100)
        main = self._main(history="x")
        main.local().writes_on_stop = {"history": "x" * 200}

        result = await main.migrate("collector", "nuc")

        assert result["success"] is False
        assert not main.spawned_remote
        (restored,) = main.spawned_local
        assert restored["replace"] is True
        assert "_initial_state" not in restored, "its own store is newer than any snapshot"

    async def test_the_record_of_the_migration_holds_no_snapshot(self) -> None:
        # The local copy is kept until the ack and is what a rollback restarts
        # from, so the record only needs to say where the agent is going.
        main = self._main(history="x" * 1000)

        await main.migrate("collector", "nuc")

        assert "_initial_state" in main.spawned_remote[0][0]
        (entry,) = main.actor.migration.pending_spawns.values()
        assert "_initial_state" not in entry["config"]
        recorded = main.recorded["_pending_migrations"]["spawns"]
        assert "x" * 1000 not in json.dumps(recorded)


class TestTheStateThatComesBack:
    """A node checks its own agent's state, on terms main sends with the request."""

    async def test_the_request_carries_the_terms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("wactorz.agents.main.migration.MIGRATION_MAX_STATE_BYTES", 1234)
        main = _Main(spawn_registry={"collector": with_code("rpi")}, nodes={"rpi": online()})

        await main.migrate("collector", "local", force=True)

        ((_topic, request),) = main.published_to("/migrate")
        assert request["force"] is True
        assert request["max_state_bytes"] == 1234

    async def test_unforced_is_the_default(self) -> None:
        main = _Main(
            spawn_registry={"collector": with_code("rpi")},
            nodes={"rpi": online(), "nuc": online()},
        )

        await main.migrate("collector", "nuc")

        ((_topic, request),) = main.published_to("/migrate")
        assert request["force"] is False

    async def test_placing_it_on_another_node_records_no_snapshot(self) -> None:
        main = _Main(
            spawn_registry={"collector": with_code("rpi")},
            nodes={"rpi": online(), "nuc": online()},
        )
        await main.migrate("collector", "nuc")
        ((_topic, request),) = main.published_to("/migrate")

        await main.actor.migration.receive_state_return(
            "nodes/rpi/state_return",
            json.dumps(
                {
                    "agent": "collector",
                    "return_token": request["return_token"],
                    "config": with_code("rpi"),
                    "state": {"history": "x" * 1000},
                }
            ).encode(),
        )

        assert main.spawned_remote[0][0]["_initial_state"] == {"history": "x" * 1000}
        (entry,) = main.actor.migration.pending_spawns.values()
        assert "_initial_state" not in entry["config"]


class TestAnAgentANodeCannotRun:
    """A built-in agent stays where it is, rather than arriving on a node empty.

    The node would start an agent with nothing to run and confirm it, and the
    migration would then purge the only copy that worked.
    """

    @staticmethod
    def _main() -> _Main:
        return _Main(
            spawn_registry={"flic": {"name": "flic", "type": "native", "node": ""}},
            nodes={"rpi": online()},
            local=("flic",),
        )

    async def test_it_is_refused_with_the_reason(self) -> None:
        main = self._main()

        result = await main.migrate("flic", "rpi")

        assert result["success"] is False
        assert "native agent" in result["message"]
        assert "stays on 'local'" in result["message"]

    async def test_nothing_is_stopped_or_sent(self) -> None:
        main = self._main()
        agent = main.local("flic")

        await main.migrate("flic", "rpi")

        assert not agent.stopped
        assert not main.spawned_remote
        assert not main.actor.migration.pending_spawns
