"""A machine for a node to be deployed to, and what a scenario may ask of it.

The suite does not start a node. It starts a machine -- a container with Python
and an SSH server and nothing of Wactorz -- and the application deploys a node
to it with `/deploy`, from the chat, as it would to a Raspberry Pi: it builds a
wheel of itself, uploads it, makes a virtualenv, writes the node's settings and
starts it. So the node under test is one the product installed.

This module makes the key the application deploys with, starts and stops the
machine, and reads the node's side of things from inside it.
"""

from __future__ import annotations

import subprocess

import asyncssh

from . import broker, waiting
from .run import Run

CONTAINER = "wactorz-e2e-node"

#: The user the application deploys as, and where it puts the node.
USER = "node"
HOME = f"/home/{USER}"


def make_key(run: Run) -> None:
    """Write the key pair this run deploys with.

    Made here and not with ``ssh-keygen``, so the suite needs no SSH tools on
    the machine it runs on: the application brings ``asyncssh`` with it.
    """
    key = asyncssh.generate_private_key("ssh-ed25519", comment="wactorz-e2e")
    private = run.ssh / "id_ed25519"
    private.write_bytes(key.export_private_key())
    private.chmod(0o600)
    (run.ssh / "id_ed25519.pub").write_bytes(key.export_public_key())


def up(run: Run) -> None:
    """Build and start the machine, and wait for its SSH server to answer."""
    broker._compose(run, "up", "-d", "--build", "node")
    waiting.until(
        lambda: broker.reachable(run.ports.node_ssh),
        what=f"the machine's SSH server on port {run.ports.node_ssh}",
    )


def run_on(command: str) -> subprocess.CompletedProcess[str]:
    """Run a shell command on the machine, as the user the node runs as."""
    return subprocess.run(
        ["docker", "exec", "-u", USER, CONTAINER, "sh", "-c", command],
        capture_output=True,
        text=True,
        check=False,
    )


def running() -> bool:
    """Whether a node process is running on the machine."""
    return run_on("pgrep -f 'wactorz-node'").returncode == 0


def settings() -> dict[str, str]:
    """The node's environment file as the deploy wrote it, by name."""
    lines = run_on(f"cat {HOME}/wactorz/.env").stdout.splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def log() -> str:
    """What the node has logged, wherever the deploy pointed it."""
    return run_on(f"cat {HOME}/wactorz/*.log 2>/dev/null").stdout


def machine_log() -> str:
    """The machine's own console: its SSH server."""
    done = subprocess.run(
        ["docker", "logs", CONTAINER], capture_output=True, text=True, check=False
    )
    return done.stdout + done.stderr
