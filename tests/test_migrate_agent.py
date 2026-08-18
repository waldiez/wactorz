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

from wactorz.agents.main_actor import MainActor
from wactorz.agents.manifests import ManifestRegistry
from wactorz.agents.migration import Migration
from wactorz.agents.nodes import NodeManager


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
    def _main(self) -> _Main:
        return _Main(
            spawn_registry={"collector": with_code(node="rpi")},
            nodes={"rpi": online(), "nuc": online()},
        )

    async def test_the_source_node_is_told_to_hand_it_over(self) -> None:
        main = self._main()

        await main.migrate("collector", "nuc")

        topic, payload = main.published_to("/migrate")[0]
        assert topic == "nodes/rpi/migrate"
        assert payload["target_node"] == "nuc"

    async def test_the_registry_is_moved_to_the_target(self) -> None:
        # Written before the move completes, so a restart in between does not
        # bring the agent back up on the machine it is leaving.
        main = self._main()

        await main.migrate("collector", "nuc")

        assert main.saved[0]["node"] == "nuc"

    async def test_both_nodes_desired_state_are_republished(self) -> None:
        # The source must forget it and the target must expect it, or one of
        # them re-spawns it after a restart. Asserted on what goes on the wire:
        # each node reads its own retained desired_state and reconciles.
        main = self._main()

        await main.migrate("collector", "nuc")

        published = {t.split("/")[1]: p for t, p in main.published_to("/desired_state")}
        assert set(published) == {"rpi", "nuc"}
        assert not [a for a in published["rpi"]["agents"] if a["name"] == "collector"]
        assert [a["name"] for a in published["nuc"]["agents"]] == ["collector"]

    async def test_the_desired_state_is_retained(self) -> None:
        # A node reads it on startup, so it has to be there before the node is.
        main = self._main()

        await main.migrate("collector", "nuc")

        options = [
            o
            for (t, _p), o in zip(main.published, main.publish_options, strict=True)
            if t.endswith("/desired_state")
        ]
        assert all(o.get("retain") for o in options)


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

    async def test_the_node_to_node_command_is_not(self) -> None:
        # Stated beside the one above so the difference is deliberate rather
        # than something to tidy into consistency.
        main = _Main(
            spawn_registry={"collector": with_code(node="rpi")},
            nodes={"rpi": online(), "nuc": online()},
        )

        await main.migrate("collector", "nuc")

        assert main.publish_options[0].get("qos") is None

    async def test_it_reports_that_it_is_waiting(self) -> None:
        main = self._main()

        result = await main.migrate("collector", "")

        assert result["success"] is True
        assert "waiting" in result["message"]
