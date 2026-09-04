"""The deploy supervises the runner, or says plainly that it could not.

`nohup` does not survive a reboot, so a node that quietly fell back to it looks
identical to a supervised one until the power blinks. Every test here is about
which rung was reached and whether that answer is honest.
"""

import pytest

from wactorz.agents import node_service
from wactorz.agents.node_service import NOHUP, ROOT, SUDO, USER


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
    }
    answers.update(over)
    return FakeNode(**answers)


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
            return (True, "")

        assert await node_service.install(answer, user="pi", home="/home/pi") is USER


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

    async def test_a_failed_enable_falls_back_too(self) -> None:
        node = _unprivileged(**{"enable --now": (False, "Failed to enable unit")})

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
        node = _unprivileged(**{"enable --now": (False, "Failed to enable unit")})

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
        assert "--broker ${WACTORZ_BROKER}" in unit
        assert "--port ${WACTORZ_PORT}" in unit
        assert "--name ${WACTORZ_NODE}" in unit

    async def test_the_unit_is_written_through_a_quoted_heredoc(self) -> None:
        # An unquoted delimiter would let the deploying shell expand
        # ${WACTORZ_NODE} to nothing, and the node would start with no name.
        node = _root()

        await node_service.install(node, user="pi", home="/root")

        write = next(c for c in node.commands if "WZUNIT" in c)
        assert "<<'WZUNIT'" in write
        assert "${WACTORZ_NODE}" in write
