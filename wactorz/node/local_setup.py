"""``sudo wactorz-node setup-local``: a node on this machine, for code main should not run.

Main holds every credential the deployment has. Code a model writes, possibly
from an email or a web page it was reading, is safer run somewhere that holds
none of them: a node. This sets one up on the same machine, once, as root:

- a system user, ``wactorz-node``, whose home is under ``/var/lib``;
- a key main deploys with, kept in main's state directory and accepted only
  from this machine (``from="127.0.0.1,::1"``), with no forwarding and no
  terminal;
- a sudoers rule that lets that user restart its own unit and do nothing else;
- the unit itself, sandboxed (see
  :func:`wactorz.agents.node_service.sandboxed_unit_file`), enabled but not
  started: nothing is installed in it until main deploys.

The node is called ``sandbox``: ``local`` and ``main`` already mean main itself
(``/migrate <agent> local`` moves an agent to main). From then on it is like
any other node: ``/deploy sandbox`` installs wactorz
into a venv there over SSH, writes its settings, and restarts the unit, and
packages reach it the way they reach every node. A deploy restarts this unit and
never rewrites it, so the sandbox stays root's to change.

**Main's secrets must not be readable by the node's user.** Inside the unit
they are hidden whatever their mode, but a package install runs outside it, as
that user. So setup refuses to finish while main's ``.env`` or state directory
is readable to it, and ``--fix-permissions`` makes them main's alone.

Commands run through an injected callable and every path is a field of
`Layout`, so the whole sequence is testable without root.
"""

import argparse
import logging
import os
import platform
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from ..agents.node_service import (
    MANAGED_MARKER,
    SYSTEM_UNIT_PATH,
    UNIT_NAME,
    sandboxed_unit_file,
)

logger = logging.getLogger(__name__)

#: The node's name, its system user's, and the deploy target main knows it by.
NODE = "sandbox"
NODE_USER = "wactorz-node"

#: The key main deploys with, under its state directory.
KEY_NAME = "sandbox_node_ed25519"

#: How the key is limited in the node user's ``authorized_keys``: from this
#: machine only, and no forwarding or terminal. Exec and SFTP, which is what a
#: deploy uses, still work.
KEY_OPTIONS = 'from="127.0.0.1,::1",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty'

#: The comment the key carries, by which an earlier one is found and replaced.
#: Kept as it was when the node had another name, so those keys are replaced too.
KEY_COMMENT = "wactorz-local-node"

#: Characters a path may not hold to be written into a unit or sudoers file:
#: each would split the value or end it.
UNSAFE_IN_UNIT = frozenset(" \t\n\r\"'\\")

Run = Callable[[Sequence[str]], tuple[int, str]]


@dataclass(frozen=True)
class Layout:
    """Where setup writes. The defaults are the real ones; a test points them elsewhere."""

    home: Path = Path("/var/lib") / NODE_USER
    unit: Path = Path(SYSTEM_UNIT_PATH)
    sudoers: Path = Path("/etc/sudoers.d") / NODE_USER
    systemd_running: Path = Path("/run/systemd/system")


@dataclass(frozen=True)
class Main:
    """The main this node is set up for: who runs it, and where its secrets are."""

    user: str
    env_file: Path
    state_dir: Path
    secrets: list[Path] = field(default_factory=list)


class SetupError(Exception):
    """Why setup stopped, said to whoever ran it."""

    @classmethod
    def not_root(cls) -> "SetupError":
        return cls("Run it as root: sudo wactorz-node setup-local")

    @classmethod
    def not_linux(cls) -> "SetupError":
        return cls(
            "A sandbox node needs Linux with systemd. Elsewhere, model-written code stays on main."
        )

    @classmethod
    def no_systemd(cls) -> "SetupError":
        return cls("systemd is not running here, and the sandbox node is a systemd unit.")

    @classmethod
    def no_main_user(cls) -> "SetupError":
        return cls(
            "Say which user runs main: --main-user NAME (sudo sets it for you as SUDO_USER)."
        )

    @classmethod
    def main_is_root(cls) -> "SetupError":
        return cls("Main should not run as root; give the user that runs it with --main-user.")

    @classmethod
    def unknown_user(cls, name: str) -> "SetupError":
        return cls(f"There is no user {name!r} on this machine.")

    @classmethod
    def no_env_file(cls, path: Path) -> "SetupError":
        return cls(f"Main's settings are not at {path}; give their path with --env-file.")

    @classmethod
    def unsafe_path(cls, path: Path) -> "SetupError":
        return cls(
            f"{path} has a space, quote or backslash in it, which a unit file cannot hold. "
            "Move main's state somewhere without one."
        )

    @classmethod
    def foreign_unit(cls, path: Path) -> "SetupError":
        return cls(
            f"{path} is already here and was not written by setup-local: this machine is "
            "a deployed node. Remove that node first."
        )

    @classmethod
    def readable(cls, paths: Sequence[Path]) -> "SetupError":
        listed = "\n".join(f"  {path}" for path in paths)
        return cls(
            f"{NODE_USER} could read main's secrets:\n{listed}\n"
            "Run again with --fix-permissions to make them main's alone."
        )

    @classmethod
    def failed(cls, command: Sequence[str], output: str) -> "SetupError":
        return cls(f"`{shlex.join(command)}` failed: {output.strip()}")


def unit_safe(path: Path) -> bool:
    """Whether ``path`` can be written into a unit or sudoers file as it is."""
    return not (set(str(path)) & UNSAFE_IN_UNIT)


def main_from(args: argparse.Namespace, run: Run) -> Main:
    """Who runs main, and where its settings and state are, from the arguments."""
    user = args.main_user or os.environ.get("SUDO_USER", "")
    if not user:
        raise SetupError.no_main_user()
    if user == "root":
        raise SetupError.main_is_root()
    if run(["id", "-u", user])[0] != 0:
        raise SetupError.unknown_user(user)
    env_file = Path(args.env_file).expanduser().resolve()
    if not env_file.is_file():
        raise SetupError.no_env_file(env_file)
    configured = str(dotenv_values(env_file).get("WACTORZ_STATE_DIR") or "").strip()
    state_dir = Path(args.state_dir or configured or env_file.parent / "state")
    if not state_dir.is_absolute():
        state_dir = env_file.parent / state_dir
    state_dir = state_dir.resolve()
    for path in (env_file, state_dir):
        if not unit_safe(path):
            raise SetupError.unsafe_path(path)
    return Main(user=user, env_file=env_file, state_dir=state_dir, secrets=[env_file, state_dir])


def setup(main: Main, run: Run, layout: Layout, *, fix_permissions: bool) -> Path:
    """Set the sandbox node up for ``main``, returning the key main deploys with."""
    if layout.unit.exists() and not _ours(layout.unit):
        raise SetupError.foreign_unit(layout.unit)
    _ensure_user(run, layout)
    _ensure_state_dir(run, layout)
    key = main.state_dir / KEY_NAME
    _ensure_key(run, main, key)
    _authorize(run, layout, key)
    _allow_restart(run, layout)
    _install_unit(run, layout, main)
    _check_secrets(run, main, fix_permissions=fix_permissions)
    return key


def _ours(unit: Path) -> bool:
    """Whether ``unit`` is one setup-local wrote, by its first line."""
    return unit.read_text(encoding="utf-8").splitlines()[:1] == [MANAGED_MARKER]


def _must(run: Run, command: Sequence[str]) -> str:
    status, output = run(command)
    if status != 0:
        raise SetupError.failed(command, output)
    return output


def _ensure_user(run: Run, layout: Layout) -> None:
    """The node's system user, made if it is not there. A shell, because SSH runs commands in one."""
    if run(["id", "-u", NODE_USER])[0] == 0:
        return
    _must(
        run,
        [
            "useradd",
            "--system",
            "--create-home",
            "--home-dir",
            str(layout.home),
            "--shell",
            "/bin/sh",
            NODE_USER,
        ],
    )
    logger.info("Created the system user %s, home %s", NODE_USER, layout.home)


def _ensure_state_dir(run: Run, layout: Layout) -> None:
    """The one directory the unit may write, which must exist before it starts."""
    state = layout.home / "wactorz" / "state"
    _must(run, ["install", "-d", "-m", "0700", "-o", NODE_USER, "-g", NODE_USER, str(layout.home)])
    for directory in (layout.home / "wactorz", state):
        _must(
            run, ["install", "-d", "-m", "0700", "-o", NODE_USER, "-g", NODE_USER, str(directory)]
        )


def _ensure_key(run: Run, main: Main, key: Path) -> None:
    """The key main deploys with, made as main's user so only main can read it."""
    if key.exists():
        return
    if not main.state_dir.exists():
        # Only when missing: on an existing directory `install -d` would also
        # change its mode, which is `--fix-permissions`'s to do, and said.
        _must(run, ["install", "-d", "-m", "0700", "-o", main.user, str(main.state_dir)])
    _must(
        run,
        [
            "runuser",
            "-u",
            main.user,
            "--",
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            KEY_COMMENT,
            "-f",
            str(key),
        ],
    )
    logger.info("Made the key main deploys the sandbox node with: %s", key)


def _authorize(run: Run, layout: Layout, key: Path) -> None:
    """Let main's key in, from this machine only, replacing any earlier one of ours."""
    public = Path(f"{key}.pub").read_text(encoding="utf-8").strip()
    ssh = layout.home / ".ssh"
    keys = ssh / "authorized_keys"
    kept = [
        line
        for line in (keys.read_text(encoding="utf-8").splitlines() if keys.exists() else [])
        if not line.rstrip().endswith(KEY_COMMENT)
    ]
    _must(run, ["install", "-d", "-m", "0700", "-o", NODE_USER, "-g", NODE_USER, str(ssh)])
    keys.write_text("\n".join([*kept, f"{KEY_OPTIONS} {public}"]) + "\n", encoding="utf-8")
    _must(run, ["chown", f"{NODE_USER}:{NODE_USER}", str(keys)])
    _must(run, ["chmod", "0600", str(keys)])


def _allow_restart(run: Run, layout: Layout) -> None:
    """A sudoers rule for one command: restarting the node's own unit.

    Checked with ``visudo`` before it is put in place, since a sudoers file with
    an error in it can stop sudo working for everyone.
    """
    systemctl = _must(run, ["sh", "-c", "command -v systemctl"]).strip() or "/usr/bin/systemctl"
    rule = f"{NODE_USER} ALL=(root) NOPASSWD: {systemctl} restart {UNIT_NAME}\n"
    pending = layout.sudoers.with_name(f".{layout.sudoers.name}.new")
    pending.write_text(rule, encoding="utf-8")
    try:
        _must(run, ["visudo", "-cf", str(pending)])
        _must(run, ["chmod", "0440", str(pending)])
        pending.replace(layout.sudoers)
    finally:
        pending.unlink(missing_ok=True)


def _install_unit(run: Run, layout: Layout, main: Main) -> None:
    """The sandboxed unit, enabled; started only by the first deploy, once there is a venv."""
    text = sandboxed_unit_file(str(layout.home), NODE_USER, [str(path) for path in main.secrets])
    layout.unit.write_text(text, encoding="utf-8")
    _must(run, ["chmod", "0644", str(layout.unit)])
    _must(run, ["systemctl", "daemon-reload"])
    _must(run, ["systemctl", "enable", UNIT_NAME])


def _check_secrets(run: Run, main: Main, *, fix_permissions: bool) -> None:
    """Main's settings and state, readable by main alone -- or setup refuses to finish."""
    readable = [
        path
        for path in main.secrets
        if run(["runuser", "-u", NODE_USER, "--", "test", "-r", str(path)])[0] == 0
    ]
    if not readable:
        return
    if not fix_permissions:
        raise SetupError.readable(readable)
    for path in readable:
        _must(run, ["chmod", "0700" if path.is_dir() else "0600", str(path)])
    logger.info("Made these main's alone: %s", ", ".join(str(path) for path in readable))


def deploy_settings(key: Path) -> str:
    """What main's ``.env`` needs for ``/deploy sandbox``, besides the node in ``DEPLOY_TARGETS``."""
    return "\n".join(
        [
            f"DEPLOY_{NODE.upper()}_HOST=127.0.0.1",
            f"DEPLOY_{NODE.upper()}_USER={NODE_USER}",
            f"DEPLOY_{NODE.upper()}_KEY={key}",
        ]
    )


def _run(command: Sequence[str]) -> tuple[int, str]:
    """Run ``command`` here, returning its status and what it printed."""
    done = subprocess.run(list(command), capture_output=True, text=True, check=False)
    return done.returncode, done.stdout + done.stderr


def get_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wactorz-node setup-local",
        description=(
            "Set up a sandboxed node on this machine, under its own user, for model-written "
            "code. Run once, as root; then add the lines it prints to main's .env and run "
            f"/deploy {NODE}."
        ),
    )
    parser.add_argument(
        "--main-user", default="", help="The user that runs main. Default: SUDO_USER."
    )
    parser.add_argument("--env-file", default=".env", help="Main's .env. Default: ./.env.")
    parser.add_argument(
        "--state-dir",
        default="",
        help="Main's state directory. Default: WACTORZ_STATE_DIR from main's .env, else state/ beside it.",
    )
    parser.add_argument(
        "--fix-permissions",
        action="store_true",
        help="Make main's .env and state directory readable by main's user alone, where they are not.",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str]) -> int:
    """Entry point for ``wactorz-node setup-local``; returns the exit status."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = get_args(argv)
    try:
        if platform.system() != "Linux":
            raise SetupError.not_linux()
        if os.geteuid() != 0:
            raise SetupError.not_root()
        layout = Layout()
        if not layout.systemd_running.is_dir():
            raise SetupError.no_systemd()
        key = setup(main_from(args, _run), _run, layout, fix_permissions=args.fix_permissions)
    except SetupError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"The {NODE} node is set up. In main's .env, add {NODE} to DEPLOY_TARGETS and add "
        f"these lines; then restart main and run /deploy {NODE}.\n\n{deploy_settings(key)}"
    )
    return 0
