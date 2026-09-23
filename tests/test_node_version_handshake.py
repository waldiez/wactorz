"""A node runs the same version as main, or it is not given agents.

A node runs the same package main does, and an agent sent to it is built from
that code against the same contract. A node on another release may not know a
field a spawn config carries today, and the failure is not loud: the agent is
handed over, the node does what it can with it, and whatever went wrong shows
up later, somewhere else. So main reads the version every heartbeat carries and
refuses to spawn on, or migrate to, a node that reports a different one --
saying which command brings the node level.

The node's command line is part of the same guard. ``wactorz-node`` exists only
in releases that carry the node runtime, so a unit that starts the node through
it fails with "command not found" on an older install, rather than starting
that install's *server* -- which is what ``wactorz --node`` did on a release
whose parser had never heard of the flag and dropped it.
"""

import shlex
import time
from typing import Any

import pytest

from wactorz._version import __version__
from wactorz.agents import node_service
from wactorz.agents.installer_agent import InstallerAgent
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawns import SpawnService
from wactorz.node import cli as node_cli

# ── The rule itself ────────────────────────────────────────────────────────────


def _node(version: str | None, agents: tuple[str, ...] = ()) -> dict[str, Any]:
    entry: dict[str, Any] = {"last_seen": time.time(), "agents": list(agents)}
    if version is not None:
        entry["version"] = version
    return entry


class TestTheRule:
    def test_a_node_on_the_same_version_is_fine(self) -> None:
        nodes = NodeManager()
        nodes.known["rpi"] = _node(__version__)

        assert nodes.version_mismatch("rpi") is None

    def test_a_node_on_another_version_is_refused_with_the_command_to_fix_it(self) -> None:
        nodes = NodeManager()
        nodes.known["rpi"] = _node("0.0.1")

        why = nodes.version_mismatch("rpi")

        assert why is not None
        assert "0.0.1" in why
        assert __version__ in why
        assert "/deploy rpi" in why

    def test_a_node_that_reports_no_version_is_not_judged_here(self) -> None:
        # A runtime older than the field. The signing and runtime handling read
        # the same heartbeat and already say what to do about one of those.
        nodes = NodeManager()
        nodes.known["rpi"] = _node(None)

        assert nodes.version_mismatch("rpi") is None

    def test_a_node_nobody_has_heard_from_is_not_judged_here(self) -> None:
        # Whether it is online is a different question, answered elsewhere.
        assert NodeManager().version_mismatch("ghost") is None

    async def test_the_version_comes_off_the_heartbeat(self) -> None:
        nodes = NodeManager()

        await nodes.receive_heartbeat("rpi", {"agents": [], "version": "0.0.1"})

        assert nodes.version_mismatch("rpi") is not None


# ── Spawning ───────────────────────────────────────────────────────────────────


class _Main:
    """A MainActor with the surface a remote spawn touches, recording what leaves."""

    def __init__(self, nodes: dict[str, dict[str, Any]]) -> None:
        main = MainActor.__new__(MainActor)
        main.name = "main"
        main.actor_id = "main-id"
        main.manifests = ManifestRegistry(main)
        main.nodes = NodeManager(main, main.manifests)
        main.migration = Migration(main, main.nodes)
        main.spawns = SpawnService(main)
        main._known_nodes = dict(nodes)
        main._registry = None
        main._result_futures = {}
        self.published: list[tuple[str, Any]] = []
        self.desired: list[tuple[str, Any]] = []
        self.saved: list[dict[str, Any]] = []

        async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
            self.published.append((topic, payload))

        async def _desired(node: str, cfg: Any = None, **_kw: Any) -> None:
            self.desired.append((node, cfg))

        setattr(main, "_mqtt_publish", _publish)
        setattr(main, "_update_node_desired_state", _desired)
        setattr(main.spawns, "_save_to_spawn_registry", self.saved.append)
        setattr(main.spawns, "_get_spawn_registry", dict)
        setattr(main, "_get_spawn_registry", dict)
        self.main = main

    def topics(self) -> list[str]:
        return [topic for topic, _ in self.published]


class TestSpawningOnANode:
    async def test_a_node_on_another_version_is_not_sent_the_agent(self) -> None:
        fixture = _Main({"rpi": _node("0.0.1")})

        await fixture.main.spawns._spawn_remote(
            {"name": "collector", "code": "print(1)", "node": "rpi"}, "rpi", save=True
        )

        assert "nodes/rpi/spawn" not in fixture.topics()
        assert fixture.desired == []
        assert fixture.saved == []

    async def test_the_refusal_is_said_where_the_user_looks(self) -> None:
        fixture = _Main({"rpi": _node("0.0.1")})

        await fixture.main.spawns._spawn_remote(
            {"name": "collector", "code": "print(1)", "node": "rpi"}, "rpi", save=True
        )

        (entry,) = [payload for topic, payload in fixture.published if topic.endswith("/logs")]
        assert entry["type"] == "error"
        assert "collector" in entry["message"]
        assert "0.0.1" in entry["message"]
        assert "/deploy rpi" in entry["message"]

    async def test_a_node_on_the_same_version_is_sent_the_agent(self) -> None:
        fixture = _Main({"rpi": _node(__version__)})

        await fixture.main.spawns._spawn_remote(
            {"name": "collector", "code": "print(1)", "node": "rpi"}, "rpi", save=True
        )

        assert "nodes/rpi/spawn" in fixture.topics()
        assert fixture.desired and fixture.desired[0][0] == "rpi"


# ── Migrating ──────────────────────────────────────────────────────────────────


def _migrating_main(nodes: dict[str, dict[str, Any]], on: str = "") -> Any:
    fixture = _Main(nodes)
    main = fixture.main
    registry = {"collector": {"name": "collector", "code": "print(1)", "node": on}}
    main._agent_manifests = {}
    setattr(main, "_get_spawn_registry", lambda: dict(registry))
    setattr(main.spawns, "_get_spawn_registry", lambda: dict(registry))
    setattr(main, "recall", lambda *_a, **_k: {})
    setattr(main, "persist", lambda *_a, **_k: None)
    return fixture


class TestMigratingToANode:
    async def test_a_target_on_another_version_refuses_before_anything_moves(self) -> None:
        fixture = _migrating_main({"rpi": _node("0.0.1")})

        result = await fixture.main.migration.migrate_agent("collector", "rpi")

        assert result["success"] is False
        assert "0.0.1" in result["message"]
        assert "/deploy rpi" in result["message"]
        assert "stays on 'local'" in result["message"]
        assert not any(t.startswith("nodes/rpi/") for t in fixture.topics())

    async def test_migrating_home_is_never_a_version_question(self) -> None:
        # `local` is this server; there is no version to disagree with.
        fixture = _migrating_main({"rpi": _node("0.0.1", agents=("collector",))}, on="rpi")

        result = await fixture.main.migration.migrate_agent("collector", "local")

        assert "version" not in result.get("message", "")


# ── The node's own command ─────────────────────────────────────────────────────


class TestTheNodeCommand:
    def test_it_is_a_console_script_of_its_own(self) -> None:
        from importlib.metadata import entry_points

        scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
        assert scripts.get("wactorz-node") == "wactorz.node.cli:main"

    def test_it_reads_the_name_the_unit_passes(self) -> None:
        args = node_cli.get_args(
            ["--node", "rpi", "--mqtt-broker", "10.0.0.1", "--mqtt-port", "1883"]
        )

        assert node_cli.node_name_from(args) == "rpi"
        assert node_cli.broker_host(args) == "10.0.0.1"
        assert node_cli.broker_port(args) == 1883

    def test_it_falls_back_to_the_environment_the_deploy_writes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WACTORZ_NODE", "rpi-env")
        monkeypatch.setenv("WACTORZ_BROKER", "10.0.0.2")
        monkeypatch.setenv("WACTORZ_PORT", "8883")

        args = node_cli.get_args([])

        assert node_cli.node_name_from(args) == "rpi-env"
        assert node_cli.broker_host(args) == "10.0.0.2"
        assert node_cli.broker_port(args) == 8883

    def test_a_server_flag_is_an_error_not_a_server(self) -> None:
        # The whole point: a parser that does not accept server flags cannot be
        # talked into running the server.
        with pytest.raises(SystemExit) as exit_:
            node_cli.get_args(["--interface", "rest"])
        assert exit_.value.code == 2

    @pytest.mark.parametrize("system", [True, False])
    def test_the_unit_starts_the_node_through_it(self, system: bool) -> None:
        unit = node_service.unit_file("/home/pi", "pi", system=system)

        exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        assert exec_start.startswith("ExecStart=/home/pi/wactorz/venv/bin/wactorz-node ")
        assert "--node ${WACTORZ_NODE}" in exec_start

    def test_the_unsupervised_launch_does_too(self) -> None:
        command = InstallerAgent._nohup_launch("rpi", "10.0.0.1", 1883)

        assert "nohup ~/wactorz/venv/bin/wactorz-node " in command
        assert "--node rpi" in command
        assert shlex.split(command.split("nohup ", 1)[1].split(" >", 1)[0])[0].endswith(
            "wactorz-node"
        )
