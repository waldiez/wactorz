"""Setting up a node on main's own machine, for the code main should not run.

`sudo wactorz-node setup-local` runs once, as root, and leaves a user, a key, a
sudoers rule and a sandboxed unit; `/deploy local` does the rest as it does for
any node. What matters here is what root leaves behind: the node's user can
restart its own unit and nothing more, main's key works only from this machine,
and main's secrets are out of the node's reach -- or setup says so and stops.

Every command goes through a fake that records it and acts out the few whose
effects later steps read (a directory made, a key generated), and every path
is under a temporary directory, so nothing here needs root.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from wactorz.agents.node_service import MANAGED_MARKER, UNIT_NAME
from wactorz.config import RESERVED_NODE_NAMES
from wactorz.node import cli as node_cli
from wactorz.node import local_setup
from wactorz.node.local_setup import (
    KEY_COMMENT,
    KEY_NAME,
    NODE_USER,
    Layout,
    Main,
    SetupError,
    main_from,
    setup,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a local node is Linux only")


class FakeMachine:
    """Answers setup's commands, recording them, and acts out those whose results it reads."""

    def __init__(self, *, user_exists: bool = False, readable: Sequence[Path] = ()) -> None:
        self.user_exists = user_exists
        self.readable = {str(path) for path in readable}
        self.failing: set[str] = set()
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> tuple[int, str]:
        command = list(command)
        self.commands.append(command)
        if command[0] in self.failing:
            return 1, f"{command[0]}: refused"
        if command[:2] == ["id", "-u"]:
            known = command[2] != NODE_USER or self.user_exists
            return (0, "1000\n") if known else (1, "no such user")
        if command[0] == "install" and "-d" in command:
            Path(command[-1]).mkdir(parents=True, exist_ok=True)
        if "ssh-keygen" in command:
            key = Path(command[command.index("-f") + 1])
            key.write_text("PRIVATE\n", encoding="utf-8")
            Path(f"{key}.pub").write_text(f"ssh-ed25519 AAAAkey {KEY_COMMENT}\n", encoding="utf-8")
        if command[:2] == ["sh", "-c"]:
            return 0, "/usr/bin/systemctl\n"
        if command[-3:-1] == ["test", "-r"]:
            return (0, "") if command[-1] in self.readable else (1, "")
        return 0, ""

    def ran(self, *words: str) -> bool:
        return any(all(word in command for word in words) for command in self.commands)


@pytest.fixture(name="layout")
def layout_fixture(tmp_path: Path) -> Layout:
    etc = tmp_path / "etc"
    (etc / "sudoers.d").mkdir(parents=True)
    (etc / "systemd").mkdir()
    return Layout(
        home=tmp_path / "var" / "lib" / NODE_USER,
        unit=etc / "systemd" / UNIT_NAME,
        sudoers=etc / "sudoers.d" / NODE_USER,
        systemd_running=tmp_path / "run" / "systemd" / "system",
    )


@pytest.fixture(name="main")
def main_fixture(tmp_path: Path) -> Main:
    env_file = tmp_path / "main" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")
    state = tmp_path / "main" / "state"
    state.mkdir()
    return Main(user="tam", env_file=env_file, state_dir=state, secrets=[env_file, state])


class TestOnAFreshMachine:
    def test_the_node_gets_a_system_user_with_a_shell_for_ssh(
        self, main: Main, layout: Layout
    ) -> None:
        machine = FakeMachine()

        setup(main, machine, layout, fix_permissions=False)

        assert machine.ran("useradd", "--system", "--shell", "/bin/sh", NODE_USER)

    def test_main_s_key_is_made_as_main_s_user_in_its_state_directory(
        self, main: Main, layout: Layout
    ) -> None:
        machine = FakeMachine()

        key = setup(main, machine, layout, fix_permissions=False)

        assert key == main.state_dir / KEY_NAME
        assert machine.ran("runuser", "-u", "tam", "ssh-keygen")

    def test_the_key_works_from_this_machine_only(self, main: Main, layout: Layout) -> None:
        setup(main, FakeMachine(), layout, fix_permissions=False)

        (line,) = (layout.home / ".ssh" / "authorized_keys").read_text().splitlines()
        assert line.startswith('from="127.0.0.1,::1",')
        for option in ("no-port-forwarding", "no-agent-forwarding", "no-pty"):
            assert option in line

    def test_the_node_may_restart_its_own_unit_and_nothing_else(
        self, main: Main, layout: Layout
    ) -> None:
        setup(main, FakeMachine(), layout, fix_permissions=False)

        assert layout.sudoers.read_text() == (
            f"{NODE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart {UNIT_NAME}\n"
        )

    def test_the_rule_is_checked_before_it_is_put_in_place(
        self, main: Main, layout: Layout
    ) -> None:
        # A sudoers file with an error in it can stop sudo for everyone.
        machine = FakeMachine()
        machine.failing.add("visudo")

        with pytest.raises(SetupError, match="visudo"):
            setup(main, machine, layout, fix_permissions=False)

        assert not layout.sudoers.exists()
        assert list(layout.sudoers.parent.iterdir()) == []

    def test_the_unit_is_sandboxed_marked_and_hides_main_s_secrets(
        self, main: Main, layout: Layout
    ) -> None:
        setup(main, FakeMachine(), layout, fix_permissions=False)

        unit = layout.unit.read_text().splitlines()
        assert unit[0] == MANAGED_MARKER
        assert f"User={NODE_USER}" in unit
        assert f"InaccessiblePaths=-{main.env_file}" in unit
        assert f"InaccessiblePaths=-{main.state_dir}" in unit

    def test_the_unit_is_enabled_but_left_for_the_deploy_to_start(
        self, main: Main, layout: Layout
    ) -> None:
        # Nothing is installed in it yet; started now, it would only fail.
        machine = FakeMachine()

        setup(main, machine, layout, fix_permissions=False)

        assert machine.ran("systemctl", "enable", UNIT_NAME)
        assert not machine.ran("systemctl", "start")
        assert not machine.ran("systemctl", "restart")


class TestRunAgain:
    def test_what_is_there_is_kept(self, main: Main, layout: Layout) -> None:
        setup(main, FakeMachine(), layout, fix_permissions=False)
        machine = FakeMachine(user_exists=True)

        setup(main, machine, layout, fix_permissions=False)

        assert not machine.ran("useradd")
        assert not machine.ran("ssh-keygen")

    def test_main_s_key_is_replaced_and_other_keys_kept(self, main: Main, layout: Layout) -> None:
        setup(main, FakeMachine(), layout, fix_permissions=False)
        keys = layout.home / ".ssh" / "authorized_keys"
        keys.write_text(keys.read_text() + "ssh-ed25519 AAAAother someone-else\n")

        setup(main, FakeMachine(user_exists=True), layout, fix_permissions=False)

        lines = keys.read_text().splitlines()
        assert len([line for line in lines if line.endswith(KEY_COMMENT)]) == 1
        assert "ssh-ed25519 AAAAother someone-else" in lines

    def test_a_unit_it_did_not_write_is_left_alone(self, main: Main, layout: Layout) -> None:
        # This machine is itself a deployed node: its unit is not ours to replace.
        layout.unit.write_text("[Unit]\nDescription=Wactorz remote node\n")

        with pytest.raises(SetupError, match="deployed node"):
            setup(main, FakeMachine(), layout, fix_permissions=False)

        assert layout.unit.read_text().startswith("[Unit]")


class TestAStateDirectoryAlreadyThere:
    def test_keeps_its_mode_unless_asked_to_fix_it(self, main: Main, layout: Layout) -> None:
        # Making main's files private is --fix-permissions's to do, and said.
        machine = FakeMachine()

        setup(main, machine, layout, fix_permissions=False)

        assert not machine.ran("install", "-d", str(main.state_dir))


class TestMainsSecrets:
    def test_setup_stops_while_the_node_could_read_them(self, main: Main, layout: Layout) -> None:
        machine = FakeMachine(readable=[main.env_file])

        with pytest.raises(SetupError, match="--fix-permissions") as raised:
            setup(main, machine, layout, fix_permissions=False)

        assert str(main.env_file) in str(raised.value)
        assert not machine.ran("chmod", "0600", str(main.env_file))

    def test_fixing_makes_them_main_s_alone(self, main: Main, layout: Layout) -> None:
        machine = FakeMachine(readable=[main.env_file, main.state_dir])

        setup(main, machine, layout, fix_permissions=True)

        assert machine.ran("chmod", "0600", str(main.env_file))
        assert machine.ran("chmod", "0700", str(main.state_dir))

    def test_they_are_asked_about_as_the_node_s_user(self, main: Main, layout: Layout) -> None:
        machine = FakeMachine()

        setup(main, machine, layout, fix_permissions=False)

        assert machine.ran("runuser", "-u", NODE_USER, "test", "-r", str(main.env_file))


def _args(**over: str) -> argparse.Namespace:
    return local_setup.get_args(
        [f"--{key.replace('_', '-')}={value}" for key, value in over.items()]
    )


class TestWhichMain:
    def test_the_user_comes_from_sudo(self, main: Main, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUDO_USER", "tam")

        found = main_from(_args(env_file=str(main.env_file)), FakeMachine())

        assert found.user == "tam"

    def test_root_is_refused_as_main_s_user(self, main: Main) -> None:
        with pytest.raises(SetupError, match="not run as root"):
            main_from(_args(main_user="root", env_file=str(main.env_file)), FakeMachine())

    def test_an_unknown_user_is_refused(self, main: Main) -> None:
        machine = FakeMachine()
        machine.failing.add("id")

        with pytest.raises(SetupError, match="no user"):
            main_from(_args(main_user="nobody-here", env_file=str(main.env_file)), machine)

    def test_without_a_user_it_says_how_to_name_one(
        self, main: Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SUDO_USER", raising=False)

        with pytest.raises(SetupError, match="--main-user"):
            main_from(_args(env_file=str(main.env_file)), FakeMachine())

    def test_the_state_directory_is_the_one_main_s_settings_name(self, main: Main) -> None:
        main.env_file.write_text("WACTORZ_STATE_DIR=elsewhere/state\n")

        found = main_from(_args(main_user="tam", env_file=str(main.env_file)), FakeMachine())

        assert found.state_dir == main.env_file.parent / "elsewhere" / "state"
        assert found.secrets == [main.env_file, found.state_dir]

    def test_by_default_it_is_state_beside_the_settings(self, main: Main) -> None:
        found = main_from(_args(main_user="tam", env_file=str(main.env_file)), FakeMachine())

        assert found.state_dir == main.state_dir

    def test_a_path_a_unit_cannot_hold_is_refused(self, main: Main, tmp_path: Path) -> None:
        spaced = tmp_path / "my state"

        with pytest.raises(SetupError, match="unit file cannot hold"):
            main_from(
                _args(main_user="tam", env_file=str(main.env_file), state_dir=str(spaced)),
                FakeMachine(),
            )

    def test_missing_settings_are_named(self, tmp_path: Path) -> None:
        with pytest.raises(SetupError, match="--env-file"):
            main_from(_args(main_user="tam", env_file=str(tmp_path / "nope.env")), FakeMachine())


class TestTheCommand:
    def test_it_must_run_as_root(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(local_setup.platform, "system", lambda: "Linux")
        monkeypatch.setattr(local_setup.os, "geteuid", lambda: 1000)

        assert local_setup.main([]) == 1
        assert "sudo wactorz-node setup-local" in capsys.readouterr().err

    def test_it_is_linux_only(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(local_setup.platform, "system", lambda: "Darwin")

        assert local_setup.main([]) == 1
        assert "stays on main" in capsys.readouterr().err

    def test_it_says_what_main_s_settings_need(self, tmp_path: Path) -> None:
        settings = local_setup.deploy_settings(tmp_path / KEY_NAME).splitlines()

        assert settings == [
            "DEPLOY_SANDBOX_HOST=127.0.0.1",
            f"DEPLOY_SANDBOX_USER={NODE_USER}",
            f"DEPLOY_SANDBOX_KEY={tmp_path / KEY_NAME}",
        ]

    def test_the_node_is_not_given_a_name_that_means_main(self) -> None:
        # `/migrate <agent> local` moves an agent to main, and the dashboard lists
        # main's agents under 'local'.
        assert local_setup.NODE not in RESERVED_NODE_NAMES

    def test_the_node_command_hands_setup_local_its_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        given: list[Sequence[str]] = []

        def _setup(argv: Sequence[str]) -> int:
            given.append(argv)
            return 0

        monkeypatch.setattr(local_setup, "main", _setup)

        with pytest.raises(SystemExit) as exited:
            node_cli.main(["setup-local", "--fix-permissions"])

        assert exited.value.code == 0
        assert given == [["--fix-permissions"]]
