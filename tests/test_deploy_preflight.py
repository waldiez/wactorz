"""A deploy proves the node can talk to the broker before it reports success.

"Runner started" used to be the last thing a deploy said. It meant one thing: the
unit's restart returned zero. A node whose broker address was wrong, whose port a
firewall dropped, or whose password was a comment the template lured into the
value, was reported as deployed while it retried every few seconds against
nothing, with the reason in a journal nobody was reading.

Three checks close that, each from the only place its answer is true. The node
opens the broker's port itself. The server, which can reach the broker, presents
the account the node is about to be given. And after the node starts, the deploy
waits for its first heartbeat rather than promising one.
"""

import asyncio
import inspect
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.agents import installer_agent
from wactorz.agents.installer_agent import (
    FIRST_HEARTBEAT_TIMEOUT_S,
    BrokerRefusedNodeAccountError,
    BrokerUnreachableFromNodeError,
    InstallerAgent,
    reach_check_command,
)
from wactorz.agents.main.nodes import NodeManager
from wactorz.config import CONFIG, DeployTarget


class FakeConn:
    """Answers the node's checks as a test says, recording what was run."""

    def __init__(self, ok: bool = True, output: str = "") -> None:
        self.ok = ok
        self.output = output
        self.commands: list[str] = []

    async def run(self, command: str, check: bool = False) -> Any:
        self.commands.append(command)
        return SimpleNamespace(exit_status=0 if self.ok else 1, stdout=self.output, stderr="")


class _Logs:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        self.lines.append(message)


def _agent() -> Any:
    agent = InstallerAgent.__new__(InstallerAgent)
    agent.name = "installer"
    agent._registry = None
    agent._deploy_started_at = 0.0
    logs = _Logs()
    agent._log_remote = logs  # pyright: ignore[reportAttributeAccessIssue]  # records instead of publishing
    agent.logs = logs  # pyright: ignore[reportAttributeAccessIssue]  # for the assertions below
    return agent


def _target(**over: Any) -> DeployTarget:
    return DeployTarget(name="rpi", host="10.0.0.5", **over)


# ── The port, from the node ────────────────────────────────────────────────────


class TestReachabilityFromTheNode:
    async def test_a_port_that_answers_lets_the_deploy_continue(self) -> None:
        conn = FakeConn(ok=True)

        await _agent()._check_broker_reachable(conn, "rpi", "10.0.0.1", 1883)

        assert len(conn.commands) == 1

    async def test_a_port_that_does_not_answer_fails_the_deploy_naming_the_address(
        self,
    ) -> None:
        conn = FakeConn(ok=False, output="timed out")

        with pytest.raises(BrokerUnreachableFromNodeError) as caught:
            await _agent()._check_broker_reachable(conn, "rpi", "10.0.0.1", 1883)

        message = str(caught.value)
        assert "10.0.0.1:1883" in message
        assert "timed out" in message
        assert "DEPLOY_RPI_BROKER" in message
        assert "firewall" in message

    def test_the_command_quotes_every_value(self) -> None:
        command = reach_check_command("10.0.0.1; rm -rf /", 1883)

        argv = shlex.split(command)
        assert argv[0] == "python3"
        assert "10.0.0.1; rm -rf /" in argv
        assert "1883" in argv

    def test_the_check_run_as_the_node_would_passes_against_a_listener(self) -> None:
        with socket.create_server(("127.0.0.1", 0)) as listener:
            port = listener.getsockname()[1]
            result = _run_reach_check(port)
        assert result.returncode == 0, result.stdout

    def test_the_check_run_as_the_node_would_fails_when_nothing_listens(self) -> None:
        with socket.create_server(("127.0.0.1", 0)) as probe:
            closed = probe.getsockname()[1]
        result = _run_reach_check(closed)
        assert result.returncode == 1
        assert result.stdout.strip()


def _run_reach_check(port: int) -> subprocess.CompletedProcess[str]:
    """Run the check as the node would, with this interpreter standing in for python3."""
    argv = shlex.split(reach_check_command("127.0.0.1", port))
    argv[0] = sys.executable
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)


# ── The account, from the server ───────────────────────────────────────────────


class _Attempts:
    """Stands in for the one connection the check opens, recording the login it used."""

    def __init__(self, refuse: str | None = None, hang: bool = False) -> None:
        self.refuse = refuse
        self.hang = hang
        self.logins: list[tuple[str | None, str | None]] = []

    async def __call__(self, username: str, password: str, node_name: str) -> None:
        self.logins.append((username, password))
        if self.hang:
            await asyncio.Event().wait()
        if self.refuse:
            raise ConnectionError(self.refuse)


def _server_broker(monkeypatch: pytest.MonkeyPatch, username: str, password: str) -> None:
    monkeypatch.setattr(
        installer_agent,
        "CONFIG",
        replace(CONFIG, mqtt_host="10.0.0.1", mqtt_username=username, mqtt_password=password),
    )


class TestTheAccountFromTheServer:
    async def test_the_account_the_node_will_use_is_the_one_presented(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _server_broker(monkeypatch, "wactorz", "shared")
        agent = _agent()
        attempts = _Attempts()
        agent._connect_once = attempts  # pyright: ignore[reportAttributeAccessIssue]  # the broker, faked

        await agent._check_node_account(_target(), "rpi")

        assert attempts.logins == [("wactorz", "shared")]

    async def test_a_refused_account_fails_the_deploy_before_anything_is_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The exact failure the template used to invite: a comment as a password.
        _server_broker(monkeypatch, "wactorz", "shared")
        agent = _agent()
        agent._connect_once = _Attempts(refuse="Not authorized")  # pyright: ignore[reportAttributeAccessIssue]
        target = _target(broker_password="# default: this server's MQTT_PASSWORD")

        with pytest.raises(BrokerRefusedNodeAccountError) as caught:
            await agent._check_node_account(target, "rpi")

        message = str(caught.value)
        assert "Not authorized" in message
        assert "wactorz" in message
        assert "DEPLOY_RPI_BROKER_USER" in message
        assert "# default" not in message

    async def test_a_broker_that_never_answers_is_a_refusal_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _server_broker(monkeypatch, "wactorz", "shared")
        monkeypatch.setattr(installer_agent, "CREDENTIAL_CHECK_TIMEOUT_S", 0.05)
        agent = _agent()
        agent._connect_once = _Attempts(hang=True)  # pyright: ignore[reportAttributeAccessIssue]

        with pytest.raises(BrokerRefusedNodeAccountError, match="no answer in time"):
            await agent._check_node_account(_target(), "rpi")

    async def test_an_anonymous_broker_has_nothing_to_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _server_broker(monkeypatch, "", "")
        agent = _agent()
        attempts = _Attempts(refuse="should not be called")
        agent._connect_once = attempts  # pyright: ignore[reportAttributeAccessIssue]

        await agent._check_node_account(_target(), "rpi")

        assert attempts.logins == []

    async def test_a_node_on_another_broker_is_not_checked_against_this_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The server cannot vouch for a broker it does not use itself.
        _server_broker(monkeypatch, "wactorz", "shared")
        agent = _agent()
        attempts = _Attempts(refuse="should not be called")
        agent._connect_once = attempts  # pyright: ignore[reportAttributeAccessIssue]

        await agent._check_node_account(_target(broker="10.0.0.99"), "rpi")

        assert attempts.logins == []


class _Client:
    """Stands in for `aiomqtt.Client`, recording how the check connected."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.entered = False

    def __call__(self, hostname: str, port: int, **kwargs: Any) -> "_Client":
        self.kwargs = {"hostname": hostname, "port": port, **kwargs}
        return self

    async def __aenter__(self) -> "_Client":
        self.entered = True
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.mark.real_mqtt_client
class TestTheOneConnectionTheCheckOpens:
    async def test_it_presents_the_nodes_login_to_the_servers_broker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Through the real factory, so the broker's TLS rule applies to this
        # connection exactly as it does to every other the server opens.
        monkeypatch.setattr(
            "wactorz.config.CONFIG",
            replace(
                CONFIG, mqtt_host="10.0.0.1", mqtt_port=1884, mqtt_username="", mqtt_password=""
            ),
        )
        monkeypatch.setattr(
            installer_agent,
            "CONFIG",
            replace(
                CONFIG, mqtt_host="10.0.0.1", mqtt_port=1884, mqtt_username="", mqtt_password=""
            ),
        )
        client = _Client()
        monkeypatch.setattr("aiomqtt.Client", client)

        await InstallerAgent._connect_once("rpi", "per-node", "rpi")

        assert client.entered
        assert client.kwargs["hostname"] == "10.0.0.1"
        assert client.kwargs["port"] == 1884
        assert client.kwargs["username"] == "rpi"
        assert client.kwargs["password"] == "per-node"
        assert client.kwargs["identifier"].endswith("deploy-rpi")

    async def test_an_empty_password_is_sent_as_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            installer_agent,
            "CONFIG",
            replace(CONFIG, mqtt_host="10.0.0.1", mqtt_username="", mqtt_password=""),
        )
        client = _Client()
        monkeypatch.setattr("aiomqtt.Client", client)

        await InstallerAgent._connect_once("rpi", "", "rpi")

        assert client.kwargs["password"] is None


# ── The heartbeat, after the start ─────────────────────────────────────────────


class _Main:
    def __init__(self) -> None:
        self.nodes = NodeManager()

    @property
    def _known_nodes(self) -> dict[str, dict[str, Any]]:
        return self.nodes.known


def _with_main(monkeypatch: pytest.MonkeyPatch) -> _Main:
    main = _Main()
    monkeypatch.setattr(installer_agent, "find_main_actor", lambda _registry: main)
    return main


class TestWaitingForTheFirstHeartbeat:
    async def test_a_heartbeat_after_the_start_ends_the_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent()
        main = _with_main(monkeypatch)
        agent._deploy_started_at = time.time()

        async def arrive() -> None:
            await asyncio.sleep(0.05)
            await main.nodes.receive_heartbeat("rpi", {"agents": []})

        asyncio.create_task(arrive())
        assert await agent._await_first_heartbeat("rpi") is None

    async def test_a_heartbeat_from_before_the_deploy_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The previous runner's, still in the table: a redeploy that broke the
        # node would otherwise be reported as a success on its predecessor's word.
        monkeypatch.setattr(installer_agent, "FIRST_HEARTBEAT_TIMEOUT_S", 0.1)
        agent = _agent()
        main = _with_main(monkeypatch)
        await main.nodes.receive_heartbeat("rpi", {"agents": []})
        agent._deploy_started_at = time.time() + 1.0

        assert await agent._await_first_heartbeat("rpi") is not None

    async def test_no_heartbeat_in_time_says_so_and_where_to_look(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installer_agent, "FIRST_HEARTBEAT_TIMEOUT_S", 0.1)
        agent = _agent()
        _with_main(monkeypatch)
        agent._deploy_started_at = time.time()

        problem = await agent._await_first_heartbeat("rpi")

        assert problem is not None
        assert "no heartbeat" in problem
        assert "log" in problem

    async def test_without_a_main_there_is_nobody_to_ask(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(installer_agent, "find_main_actor", lambda _registry: None)
        agent = _agent()

        assert await agent._await_first_heartbeat("rpi") is None

    def test_the_wait_is_generous(self) -> None:
        # A node installing its packages on first start takes a while to connect;
        # the wait exists to catch a node that never will, not a slow one.
        assert FIRST_HEARTBEAT_TIMEOUT_S >= 30


class TestTheLogTail:
    async def test_a_unit_is_read_from_its_journal(self) -> None:
        conn = FakeConn(ok=True, output="Publisher error: Not authorized")
        agent = _agent()

        tail = await agent._node_log_tail(conn, installer_agent.node_service.USER, "rpi")

        assert tail == "Publisher error: Not authorized"
        assert "journalctl --user" in conn.commands[0]
        assert installer_agent.node_service.UNIT_NAME in conn.commands[0]

    async def test_a_system_unit_is_read_from_the_system_journal(self) -> None:
        conn = FakeConn(ok=True, output="")
        agent = _agent()

        await agent._node_log_tail(conn, installer_agent.node_service.ROOT, "rpi")

        assert conn.commands[0].startswith("journalctl -u")

    async def test_an_unsupervised_node_is_read_from_its_file(self) -> None:
        conn = FakeConn(ok=True, output="")
        agent = _agent()

        await agent._node_log_tail(conn, installer_agent.node_service.NOHUP, "rpi")

        assert "tail" in conn.commands[0]
        assert "rpi.log" in conn.commands[0]


def test_the_checks_run_before_the_node_environment_is_written() -> None:
    # Both answers are wanted before the node has anything to act on.
    source = inspect.getsource(InstallerAgent._node_deploy)
    reach = source.index("_check_broker_reachable")
    account = source.index("_check_node_account")
    env = source.index("_put_node_env(")
    assert reach < env
    assert account < env


def test_the_heartbeat_wait_runs_after_the_node_starts() -> None:
    source = inspect.getsource(InstallerAgent._node_deploy)
    assert source.index("node_service.install") < source.index("_await_first_heartbeat")
