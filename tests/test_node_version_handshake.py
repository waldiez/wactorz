"""A node runs the same release series as main, or it is not given agents.

A node runs the same package main does, and an agent sent to it is built from
that code against the same contract. A node on another release may not know a
field a spawn config carries today, and the failure is not loud: the agent is
handed over, the node does what it can with it, and whatever went wrong shows
up later, somewhere else. So main reads the version every heartbeat carries and
refuses to spawn on, or migrate to, a node on another series -- saying which
command brings the node level. Versions that differ in the patch number alone
work together, so a fix to the server does not mean deploying every node again.

The node checks too. Main states its version on every command it sends a node,
and a node refuses a spawn from a server on another series: main's own check
depends on a heartbeat it may not have heard, or heard before the node was
installed again.

The node's command line is part of the same guard. ``wactorz-node`` exists only
in releases that carry the node runtime, so a unit that starts the node through
it fails with "command not found" on an older install, rather than starting
that install's *server* -- which is what ``wactorz --node`` did on a release
whose parser had never heard of the flag and dropped it.
"""

import asyncio
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
from wactorz.core import compatibility, node_signing
from wactorz.node import cli as node_cli
from wactorz.node import signing as node_side
from wactorz.node.runner import NodeRunner

# ── Which versions work together ───────────────────────────────────────────────


class TestWhichVersionsWorkTogether:
    @pytest.mark.parametrize(
        ("one", "other"),
        [
            ("1.4.2", "1.4.2"),
            ("1.4.2", "1.4.9"),
            ("1.4.0", "1.4.12.1"),
            ("1.4", "1.4.3"),
            ("1.4.0.dev3", "1.4.1"),
            ("nightly", "nightly"),
        ],
    )
    def test_the_same_series_does(self, one: str, other: str) -> None:
        assert compatibility.compatible(one, other)
        assert compatibility.compatible(other, one)

    @pytest.mark.parametrize(
        ("one", "other"),
        [
            ("1.4.2", "1.5.0"),
            ("1.4.2", "2.4.2"),
            # The minor number is read whole, not by its first digit.
            ("1.4.0", "1.40.0"),
            ("1.4.2", "nightly"),
            ("nightly", "weekly"),
            ("1.4.2", ""),
        ],
    )
    def test_another_series_or_an_unreadable_version_does_not(self, one: str, other: str) -> None:
        assert not compatibility.compatible(one, other)
        assert not compatibility.compatible(other, one)


def _same_series() -> str:
    """Another version in this one's series."""
    series = compatibility.series(__version__)
    assert series is not None
    return f"{series[0]}.{series[1]}.999"


def _another_series() -> str:
    series = compatibility.series(__version__)
    assert series is not None
    return f"{series[0]}.{series[1] + 1}.0"


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

    def test_a_node_a_patch_release_away_is_fine(self) -> None:
        nodes = NodeManager()
        nodes.known["rpi"] = _node(_same_series())

        assert nodes.version_mismatch("rpi") is None

    def test_a_node_on_the_next_series_is_refused(self) -> None:
        nodes = NodeManager()
        nodes.known["rpi"] = _node(_another_series())

        assert nodes.version_mismatch("rpi") is not None

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


# ── The node's own check ───────────────────────────────────────────────────────


class _Command:
    """A control message as the node receives it, with the properties main set."""

    def __init__(
        self, topic: str, payload: bytes, pairs: list[tuple[str, str]], retain: bool = False
    ) -> None:
        self.topic = topic
        self.payload = payload
        self.retain = retain
        self.properties = type("Properties", (), {"UserProperty": pairs})()


@pytest.fixture(name="runner")
def runner_fixture(tmp_path: Any) -> tuple[NodeRunner, list[tuple[str, Any]], list[Any]]:
    """A node, what it published, and the spawns it went on to start."""
    runner = NodeRunner("localhost", 1883, "rpi", state_dir=str(tmp_path))
    published: list[tuple[str, Any]] = []
    spawned: list[Any] = []

    async def _publish(topic: str, data: Any, retain: bool = False, **_kw: Any) -> None:
        published.append((topic, data))

    async def _spawn(config: Any) -> None:
        spawned.append(config)

    runner.publish = _publish  # type: ignore[method-assign]
    runner.spawn_agent = _spawn  # type: ignore[method-assign]
    return runner, published, spawned


async def _spawn_from(runner: NodeRunner, pairs: list[tuple[str, str]]) -> None:
    config = {"name": "collector"}
    await runner._on_spawn("nodes/rpi/spawn", config, _Command("nodes/rpi/spawn", b"{}", pairs))
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _desired_from(runner: NodeRunner, pairs: list[tuple[str, str]], retained: bool) -> None:
    topic = "nodes/rpi/desired_state"
    desired = {"agents": [{"name": "collector"}]}
    await runner._on_desired_state(topic, desired, _Command(topic, b"{}", pairs, retained))
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestMainStatesItsVersion:
    def test_every_command_to_a_node_names_it(self, tmp_path: Any, monkeypatch: Any) -> None:
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(node_signing, "_secret", None)
        monkeypatch.setattr(node_signing, "_last_sequence", None)

        pairs = node_signing.node_control_properties("nodes/rpi/spawn", b"{}")

        assert pairs is not None
        assert dict(pairs)[node_signing.VERSION_PROPERTY] == __version__

    def test_what_is_not_a_command_names_nothing(self) -> None:
        assert node_signing.node_control_properties("agents/x/logs", b"{}") is None


class TestTheNodeChecksToo:
    async def test_a_spawn_from_a_server_on_another_series_is_refused(
        self, runner: tuple[NodeRunner, list[tuple[str, Any]], list[Any]]
    ) -> None:
        node, published, spawned = runner
        server = _another_series()

        await _spawn_from(node, [(node_signing.VERSION_PROPERTY, server)])

        assert spawned == []
        ((topic, said),) = published
        assert topic == "agents/rpi/logs"
        assert said["type"] == "error"
        assert "collector" in said["message"]
        assert server in said["message"]
        assert __version__ in said["message"]
        assert "/deploy rpi" in said["message"]

    async def test_so_is_an_agent_it_adds_to_the_desired_state(
        self, runner: tuple[NodeRunner, list[tuple[str, Any]], list[Any]]
    ) -> None:
        # Main writes the desired state right after a spawn, and a node starts
        # what that names too: refusing the spawn alone would refuse nothing.
        node, published, spawned = runner

        await _desired_from(node, [(node_signing.VERSION_PROPERTY, _another_series())], False)

        assert spawned == []
        ((_topic, said),) = published
        assert said["type"] == "error"
        assert "collector" in said["message"]

    async def test_the_agents_a_node_had_come_back_after_a_reboot_regardless(
        self, runner: tuple[NodeRunner, list[tuple[str, Any]], list[Any]]
    ) -> None:
        # The retained copy, read when the node subscribes. A server upgraded
        # since may have written it; the agents in it ran here before.
        node, published, spawned = runner

        await _desired_from(node, [(node_signing.VERSION_PROPERTY, _another_series())], True)

        assert spawned == [{"name": "collector"}]
        assert published == []

    async def test_a_stop_is_obeyed_whoever_sends_it(
        self, runner: tuple[NodeRunner, list[tuple[str, Any]], list[Any]]
    ) -> None:
        # Stopping is how a node is brought level, so it is not held to the check.
        node, _published, _spawned = runner
        stopped: list[Any] = []

        async def _stop(name: str, delete: bool = False) -> None:
            stopped.append(name)

        node.stop_agent = _stop  # type: ignore[method-assign]
        pairs = [(node_signing.VERSION_PROPERTY, _another_series())]

        await node._on_stop(
            "nodes/rpi/stop", {"name": "collector"}, _Command("nodes/rpi/stop", b"{}", pairs)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert stopped == ["collector"]

    @pytest.mark.parametrize("server", [__version__, _same_series()])
    async def test_a_spawn_from_a_server_in_the_same_series_is_started(
        self, runner: tuple[NodeRunner, list[tuple[str, Any]], list[Any]], server: str
    ) -> None:
        node, published, spawned = runner

        await _spawn_from(node, [(node_signing.VERSION_PROPERTY, server)])

        assert spawned == [{"name": "collector"}]
        assert published == []

    async def test_a_spawn_that_names_no_version_is_started(
        self, runner: tuple[NodeRunner, list[tuple[str, Any]], list[Any]]
    ) -> None:
        # A server from before commands carried one. It judges this node by its
        # heartbeat, and the node has nothing to judge it by.
        node, _published, spawned = runner

        await _spawn_from(node, [])

        assert spawned == [{"name": "collector"}]

    def test_only_a_stated_version_this_node_cannot_work_with_is_a_mismatch(self) -> None:
        other = _another_series()

        assert node_side.server_mismatch({node_signing.VERSION_PROPERTY: other}, __version__) == (
            other
        )
        assert node_side.server_mismatch({}, __version__) is None
        assert node_side.server_mismatch({node_signing.VERSION_PROPERTY: ""}, __version__) is None
