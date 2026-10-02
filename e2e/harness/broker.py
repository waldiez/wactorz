"""The broker this suite starts, stops and takes away.

It is the development stack's own mosquitto, taken as it is from
``compose.dev.yaml`` (see ``e2e/stack/compose.yaml``): the image, the start
script that takes what Wactorz generates for it, and the watcher that reloads a
changed account. So a scenario about a node's account or the TLS listener is a
scenario about the broker people run, and not one configured for the tests.

Only ever this run's container. The suite never looks at port 1883 or at a
broker it did not start, so it cannot unplug a developer's, or someone's house.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys

from . import waiting
from .run import E2E_ROOT, REPO_ROOT, Run

COMPOSE_FILE = E2E_ROOT / "stack" / "compose.yaml"

#: The account the backend connects with: the broker's own, as in compose.
USERNAME = "wactorz"

#: The name a node reaches the broker by, on the network the two share.
NAME_FOR_NODES = "mosquitto"


def _compose(run: Run, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "E2E_BROKER_PORT": str(run.ports.broker),
        "E2E_BROKER_TLS_PORT": str(run.ports.broker_tls),
        "E2E_BROKER_DIR": str(run.broker_files),
        "E2E_BROKER_PASSWORD": run.broker_password,
        "E2E_NODE_SSH_PORT": str(run.ports.node_ssh),
        "E2E_NODE_KEYS": str(run.ssh),
    }
    done = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"`docker compose {' '.join(args)}` failed ({done.returncode}):\n"
            f"{done.stderr.strip() or done.stdout.strip()}"
        )
    return done


def issue_files(run: Run, environment: dict[str, str]) -> None:
    """Have Wactorz write what the broker reads, before the broker starts.

    The certificate, the node accounts and the access list, by the command the
    compose stacks run for the same purpose and under the environment the
    backend is about to be started with. Done first so the broker comes up
    once, already configured, and the backend never meets one that is about to
    restart to take a certificate.
    """
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "wactorz.broker_certificates",
            "--name",
            NAME_FOR_NODES,
            "--export",
            str(run.broker_files),
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    (run.logs / "broker-files.log").write_text(done.stdout + done.stderr, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"issuing the broker's files failed:\n{done.stdout}{done.stderr}")


def reachable(port: int, timeout: float = 0.5) -> bool:
    """Whether a TCP connection to this port of the broker completes."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def up(run: Run) -> None:
    """Start the broker and wait until it answers on both listeners."""
    _compose(run, "up", "-d", "--wait", "broker")
    for port in (run.ports.broker, run.ports.broker_tls):
        waiting.until(lambda p=port: reachable(p), what=f"the broker on port {port}")


def stop(run: Run) -> None:
    """Take the broker away, and wait until its ports stop answering."""
    _compose(run, "stop", "broker")
    waiting.until(
        lambda: not reachable(run.ports.broker) and not reachable(run.ports.broker_tls),
        what="the broker to stop answering",
    )


def start(run: Run) -> None:
    """Bring a stopped broker back."""
    _compose(run, "start", "broker")
    waiting.until(lambda: reachable(run.ports.broker_tls), what="the broker to answer again")


def log(run: Run) -> str:
    """What the broker has written to its console."""
    return _compose(run, "logs", "--no-color", "broker").stdout


def down(run: Run) -> None:
    """Remove everything the stack file started, and its network."""
    _compose(run, "down", "--volumes", "--remove-orphans")
