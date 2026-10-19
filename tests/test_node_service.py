"""The deploy supervises the runner, or says plainly that it could not.

`nohup` does not survive a reboot, so a node that quietly fell back to it looks
identical to a supervised one until the power blinks. Every test here is about
which rung was reached and whether that answer is honest.
"""

import logging

import pytest

from wactorz.agents import node_service
from wactorz.agents.node_service import MANAGED, NOHUP, ROOT, SUDO, USER


class FakeNode:
    """A node that answers probes from a script, recording what it was asked."""

    def __init__(self, **answers: tuple[bool, str]) -> None:
        #: Matched as substrings of the command, longest first, so a specific
        #: answer wins over a general one.
        self.answers = answers
        self.commands: list[str] = []

    async def __call__(self, command: str) -> tuple[bool, str]:
        self.commands.append(command)
        for key in sorted(self.answers, key=len, reverse=True):
            if key.replace("_", " ") in command or key in command:
                return self.answers[key]
        return (True, "")

    def ran(self, needle: str) -> bool:
        return any(needle in c for c in self.commands)


def _root() -> FakeNode:
    return FakeNode(**{"id -u": (True, "0\n")})


def _unprivileged(**over: tuple[bool, str]) -> FakeNode:
    answers: dict[str, tuple[bool, str]] = {
        "id -u": (True, "1000\n"),
        "systemctl --user show": (True, "Version=252\n"),
        "show-user": (True, "Linger=yes\n"),
        "sudo -n true": (False, ""),
        # No unit set up beforehand by setup-local: the usual remote node.
        "grep -qxF": (False, ""),
    }
    answers.update(over)
    return FakeNode(**answers)


def _set_up_locally(**over: tuple[bool, str]) -> FakeNode:
    """The local node's user: unprivileged, with root's unit and leave to restart it."""
    return _unprivileged(**{"grep -qxF": (True, ""), **over})


class TestTheLadder:
    async def test_root_takes_the_system_unit(self) -> None:
        node = _root()

        assert await node_service.install(node, user="pi", home="/root") is ROOT

        assert node.ran(f"cat > {node_service.SYSTEM_UNIT_PATH}")
        assert not node.ran("sudo")

    async def test_a_lingering_user_beats_sudo(self) -> None:
        # Least privilege first: a box with both should never reach for sudo.
        node = _unprivileged(**{"sudo -n true": (True, "")})

        assert await node_service.install(node, user="pi", home="/home/pi") is USER

        assert node.ran("cat > /home/pi/.config/systemd/user/wactorz-node.service")
        assert not node.ran("sudo -n systemctl enable")

    async def test_without_linger_it_falls_to_sudo(self) -> None:
        # A user unit dies with the last session, so it fails the one thing
        # this module exists for. Refusing the rung is the point.
        node = _unprivileged(**{"show-user": (True, "Linger=no\n"), "sudo -n true": (True, "")})

        assert await node_service.install(node, user="pi", home="/home/pi") is SUDO

        assert node.ran("enable-linger --no-ask-password")

    async def test_no_privilege_at_all_returns_nohup(self) -> None:
        node = _unprivileged(**{"show-user": (True, "Linger=no\n")})

        assert await node_service.install(node, user="pi", home="/home/pi") is NOHUP

        assert not node.ran("daemon-reload")

    async def test_linger_is_enabled_when_it_can_be(self) -> None:
        # Read, try, read again — whether an SSH exec channel counts as an
        # active session for polkit varies, so the answer is measured.
        replies = iter([(True, "Linger=no\n"), (True, "Linger=yes\n")])
        node = _unprivileged()

        async def answer(command: str) -> tuple[bool, str]:
            node.commands.append(command)
            if "show-user" in command:
                return next(replies)
            if "id -u" in command:
                return (True, "1000\n")
            if "systemctl --user show" in command:
                return (True, "Version=252\n")
            if "grep -qxF" in command:
                return (False, "")
            return (True, "")

        assert await node_service.install(answer, user="pi", home="/home/pi") is USER


class TestAUnitSetUpBeforehand:
    """The local node's unit is root's, written once by `wactorz-node setup-local`."""

    async def test_it_is_taken_ahead_of_a_user_unit(self) -> None:
        node = _set_up_locally(**{"sudo -n true": (True, "")})

        assert await node_service.install(node, user="wactorz-node", home="/var/lib/x") is MANAGED

    async def test_it_is_restarted_and_never_rewritten(self) -> None:
        # The sandbox is root's to change: a deploy that wrote its own unit here
        # would replace it with the unsandboxed one.
        node = _set_up_locally()

        await node_service.install(node, user="wactorz-node", home="/var/lib/x")

        assert node.ran(f"sudo -n systemctl restart {node_service.UNIT_NAME}")
        assert not node.ran("cat >")
        assert not node.ran("tee")
        assert not node.ran("daemon-reload")

    async def test_it_is_not_removed_as_stale(self) -> None:
        node = _set_up_locally()

        await node_service.install(node, user="wactorz-node", home="/var/lib/x")

        assert not node.ran(f"rm -f {node_service.SYSTEM_UNIT_PATH}")
        assert node.ran("rm -f /var/lib/x/.config/systemd/user/wactorz-node.service")

    async def test_both_the_marker_and_the_leave_to_restart_are_asked(self) -> None:
        node = _set_up_locally()

        await node_service.install(node, user="wactorz-node", home="/var/lib/x")

        (probe,) = [c for c in node.commands if "grep -qxF" in c]
        assert node_service.SYSTEM_UNIT_PATH in probe
        assert node.ran(f"sudo -n -l systemctl restart {node_service.UNIT_NAME}")

    async def test_a_marked_unit_sudo_will_not_restart_is_said(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The rule names one systemctl and sudo finds another: the deploy takes a
        # weaker rung, and says why rather than looking as if setup were undone.
        node = _set_up_locally(**{"sudo -n -l": (False, ""), "show-user": (True, "Linger=no\n")})

        with caplog.at_level(logging.WARNING, logger=node_service.__name__):
            rung = await node_service.install(node, user="wactorz-node", home="/var/lib/x")

        assert rung is NOHUP
        assert "sudo will not let this user restart" in caplog.text

    async def test_an_unmarked_unit_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=node_service.__name__):
            await node_service.install(_unprivileged(), user="pi", home="/home/pi")

        assert "setup-local" not in caplog.text

    async def test_a_failed_restart_falls_back_and_says_so(self) -> None:
        node = _set_up_locally(**{"sudo -n systemctl restart": (False, "")})

        assert await node_service.install(node, user="wactorz-node", home="/var/lib/x") is NOHUP


class TestTheSandboxedUnit:
    def _unit(self, hidden: list[str] | None = None) -> str:
        return node_service.sandboxed_unit_file(
            "/var/lib/wactorz-node", "wactorz-node", hidden or []
        )

    def test_it_says_who_wrote_it_on_its_first_line(self) -> None:
        assert self._unit().splitlines()[0] == node_service.MANAGED_MARKER

    def test_only_the_state_directory_is_writable(self) -> None:
        lines = self._unit().splitlines()

        assert "ProtectSystem=strict" in lines
        assert [line for line in lines if line.startswith("ReadWritePaths=")] == [
            "ReadWritePaths=/var/lib/wactorz-node/wactorz/state"
        ]

    @pytest.mark.parametrize(
        "line",
        [
            "NoNewPrivileges=yes",
            "CapabilityBoundingSet=",
            "ProtectHome=yes",
            "PrivateTmp=yes",
            "PrivateDevices=yes",
            "ProtectProc=invisible",
            "RestrictSUIDSGID=yes",
        ],
    )
    def test_it_holds_the_code_in(self, line: str) -> None:
        assert line in self._unit().splitlines()

    def test_main_s_secrets_are_hidden_from_it(self) -> None:
        unit = self._unit(["/srv/main/.env", "/srv/main/state"])

        assert "InaccessiblePaths=-/srv/main/.env" in unit.splitlines()
        assert "InaccessiblePaths=-/srv/main/state" in unit.splitlines()

    def test_a_percent_is_not_read_as_a_specifier(self) -> None:
        assert "InaccessiblePaths=-/srv/100%%/state" in self._unit(["/srv/100%/state"]).splitlines()

    def test_the_sandbox_is_in_the_service_section(self) -> None:
        unit = self._unit()

        assert unit.index("[Service]") < unit.index("NoNewPrivileges=yes") < unit.index("[Install]")
        assert "User=wactorz-node" in unit.splitlines()


class TestWhenSystemdIsThereButUnusable:
    async def test_a_failed_unit_write_falls_back_rather_than_lying(self) -> None:
        # `sudo -n true` proves only that some passwordless sudo exists —
        # NOPASSWD can be scoped to commands that exclude tee and systemctl.
        node = _unprivileged(
            **{
                "show-user": (True, "Linger=no\n"),
                "sudo -n true": (True, ""),
                "sudo -n tee": (False, "sudo: a password is required"),
            }
        )

        assert await node_service.install(node, user="pi", home="/home/pi") is NOHUP

    async def test_the_unit_is_restarted_not_merely_enabled(self) -> None:
        # Measured on hardware: the caller's pkill makes the runner exit 0, which
        # Restart=on-failure rightly declines to bring back, and `enable --now`
        # does not start a unit that is still deactivating -- so a redeploy left
        # the node dead. `restart` also adopts newly uploaded code, which
        # `--now` skips on an already-active unit.
        node = _unprivileged()

        assert await node_service.install(node, user="pi", home="/home/pi") is USER

        assert node.ran("systemctl --user restart wactorz-node.service")
        assert not node.ran("enable --now")

    async def test_a_failed_restart_falls_back(self) -> None:
        node = _unprivileged(**{"restart wactorz-node": (False, "Job failed")})

        assert await node_service.install(node, user="pi", home="/home/pi") is NOHUP

    async def test_a_failed_enable_falls_back_too(self) -> None:
        node = _unprivileged(**{"enable wactorz-node": (False, "Failed to enable unit")})

        assert await node_service.install(node, user="pi", home="/home/pi") is NOHUP


class TestStaleUnits:
    async def test_choosing_a_user_unit_clears_the_system_one(self) -> None:
        # The ladder is re-probed every deploy. A node that gains linger would
        # otherwise keep both units, and after a reboot both start a runner.
        node = _unprivileged()

        await node_service.install(node, user="pi", home="/home/pi")

        assert node.ran(f"sudo -n rm -f {node_service.SYSTEM_UNIT_PATH}")

    async def test_choosing_a_system_unit_clears_the_user_one(self) -> None:
        node = _root()

        await node_service.install(node, user="pi", home="/root")

        assert node.ran("rm -f /root/.config/systemd/user/wactorz-node.service")

    async def test_falling_back_to_nohup_clears_both(self) -> None:
        # The regression path, and the worst one. A node that had a unit and now
        # probes to nohup would otherwise keep it enabled: pkill stops the
        # runner, the unit restarts it, and the nohup launch adds a second.
        node = _unprivileged(**{"show-user": (True, "Linger=no\n")})

        assert await node_service.install(node, user="pi", home="/home/pi") is NOHUP

        assert node.ran("rm -f /home/pi/.config/systemd/user/wactorz-node.service")
        assert node.ran(f"rm -f {node_service.SYSTEM_UNIT_PATH}")

    async def test_a_unit_that_fails_to_enable_is_cleared_too(self) -> None:
        # Same class: privileges enough to probe, not enough to install, and an
        # older unit still enabled from a deploy that had them.
        node = _unprivileged(**{"enable wactorz-node": (False, "Failed to enable unit")})

        assert await node_service.install(node, user="pi", home="/home/pi") is NOHUP

        assert node.ran(f"rm -f {node_service.SYSTEM_UNIT_PATH}")


class TestTheUserManagerProbe:
    async def test_every_user_command_carries_xdg_runtime_dir(self) -> None:
        # Without it `systemctl --user` reports "Failed to connect to bus" on a
        # non-interactive exec channel, which reads as "no user systemd" and
        # escalates to sudo on a node where the unprivileged path was fine.
        node = _unprivileged()

        await node_service.install(node, user="pi", home="/home/pi")

        for command in node.commands:
            if "systemctl --user" in command:
                assert "XDG_RUNTIME_DIR=/run/user/$(id -u)" in command


class TestTheUnitItself:
    @pytest.mark.parametrize("system", [True, False])
    def test_it_restarts_on_failure_only(self, system: bool) -> None:
        # `/nodes shutdown` exits 0 deliberately; Restart=always would turn that
        # command into a restart.
        unit = node_service.unit_file("/home/pi", "pi", system=system)

        assert "Restart=on-failure" in unit
        assert "Restart=always" not in unit

    @pytest.mark.parametrize("system", [True, False])
    def test_exit_two_never_loops(self, system: bool) -> None:
        # Exit 2 is a node name holding an MQTT wildcard, which can never
        # succeed on retry, and argparse's error code for a malformed ExecStart.
        unit = node_service.unit_file("/home/pi", "pi", system=system)

        assert "RestartPreventExitStatus=2" in unit

    @pytest.mark.parametrize("system", [True, False])
    def test_start_limits_live_in_the_unit_section(self, system: bool) -> None:
        # They moved out of [Service] in systemd 229 and are ignored there.
        unit = node_service.unit_file("/home/pi", "pi", system=system)
        head = unit.split("[Service]")[0]

        assert "StartLimitBurst=5" in head
        assert "StartLimitIntervalSec=120" in head

    def test_a_system_unit_drops_privilege_to_the_deploy_user(self) -> None:
        unit = node_service.unit_file("/home/pi", "pi", system=True)

        assert "User=pi" in unit
        assert "WantedBy=multi-user.target" in unit

    def test_a_user_unit_names_no_user(self) -> None:
        unit = node_service.unit_file("/home/pi", "pi", system=False)

        assert "User=" not in unit
        assert "WantedBy=default.target" in unit

    @pytest.mark.parametrize("system", [True, False])
    def test_every_argument_comes_from_the_environment_file(self, system: bool) -> None:
        # The unit carries no node name, so it needs no shell escaping for one:
        # systemd substitutes ${VAR} as a single argument without word
        # splitting, and a node name may legally hold a space or a `;`.
        unit = node_service.unit_file("/home/pi", "pi", system=system)

        assert "EnvironmentFile=/home/pi/wactorz/.env" in unit
        assert "--mqtt-broker ${WACTORZ_BROKER}" in unit
        assert "--mqtt-port ${WACTORZ_PORT}" in unit
        assert "--node ${WACTORZ_NODE}" in unit

    async def test_the_unit_is_written_through_a_quoted_heredoc(self) -> None:
        # An unquoted delimiter would let the deploying shell expand
        # ${WACTORZ_NODE} to nothing, and the node would start with no name.
        node = _root()

        await node_service.install(node, user="pi", home="/root")

        write = next(c for c in node.commands if "WZUNIT" in c)
        assert "<<'WZUNIT'" in write
        assert "${WACTORZ_NODE}" in write
