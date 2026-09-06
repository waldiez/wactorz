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

import time
from typing import Any

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawns import SpawnService


class _Registry:
    """The actor registry, holding whichever agents are alive locally."""

    def __init__(self, names: tuple[str, ...] = ()) -> None:
        self._by = {n: _LocalAgent(n) for n in names}

    def find_by_name(self, name: str) -> "_LocalAgent | None":
        return self._by.get(name)


class _LocalAgent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name}-id"
        self._persistence_api = None


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
        setattr(main, "_registry", _Registry(local))

        self.published: list[tuple[str, Any]] = []
        self.publish_options: list[dict[str, Any]] = []
        self.saved: list[dict[str, Any]] = []
        self.desired_state: list[tuple[str, Any, Any]] = []
        self._spawn_registry = dict(spawn_registry or {})

        async def _publish(topic: str, payload: Any, **kw: Any) -> None:
            self.published.append((topic, payload))
            self.publish_options.append(kw)

        async def _desired(node: str, cfg: Any = None, remove_name: Any = None) -> None:
            self.desired_state.append((node, cfg, remove_name))

        setattr(main, "_mqtt_publish", _publish)
        setattr(main, "_update_node_desired_state", _desired)
        setattr(main, "_get_spawn_registry", lambda: dict(self._spawn_registry))
        setattr(main, "_save_to_spawn_registry", self.saved.append)
        self.actor = main

    async def migrate(self, agent: str, target: str) -> dict[str, Any]:
        return await self.actor.migrate_agent(agent, target)

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
    -- and the ACL that closes it forbids the write anyway. So main asks the
    source to hand the agent back, then places it on the target itself.
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
