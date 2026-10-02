"""One run of the suite: where it writes, the ports it uses, the secrets it made up.

Everything a run needs that must not collide with anything else on the machine
is decided here, once, before any process starts: a directory of its own under
``e2e/out``, ports nothing is listening on, and an API key and a broker password
generated for this run and written nowhere but its processes' environments.

A developer's own instance, its ``.env`` and its state are never read or
touched. That is what lets the suite run on a machine where Wactorz is already
running.
"""

from __future__ import annotations

import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

E2E_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = E2E_ROOT.parent
OUT = E2E_ROOT / "out"

#: The name a deployed node and its account go by.
NODE_NAME = "edge"


def free_ports(count: int) -> list[int]:
    """``count`` ports nothing is listening on, all different from each other.

    Every socket is held open until the last has been assigned: taken one at a
    time, the kernel hands back the port it was just given back, and two calls
    in a row return the same number.
    """
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [int(sock.getsockname()[1]) for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


@dataclass(frozen=True)
class Ports:
    dashboard: int
    api: int
    broker: int
    broker_tls: int
    node_ssh: int


@dataclass(frozen=True)
class Run:
    """The directories, ports and secrets of one run."""

    root: Path
    ports: Ports
    api_key: str = field(repr=False)
    broker_password: str = field(repr=False)

    @property
    def state(self) -> Path:
        """The backend's state directory."""
        return self.root / "state"

    @property
    def broker_files(self) -> Path:
        """What the backend generates for the broker: accounts, access list, certificate."""
        return self.root / "broker"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def traces(self) -> Path:
        return self.root / "traces"

    @property
    def ssh(self) -> Path:
        """The key pair the backend deploys with, and the hosts it has seen."""
        return self.root / "ssh"

    @property
    def secrets(self) -> dict[str, str]:
        """What this run made up, by what to call each in a failure message."""
        return {
            "the API key": self.api_key,
            "the broker password": self.broker_password,
        }


def new() -> Run:
    """A fresh run: a directory named for now, free ports, new secrets."""
    root = OUT / time.strftime("%Y%m%d-%H%M%S")
    root.mkdir(parents=True, exist_ok=False)
    dashboard, api, broker, broker_tls, node_ssh = free_ports(5)
    run = Run(
        root=root,
        ports=Ports(dashboard, api, broker, broker_tls, node_ssh),
        api_key=secrets.token_urlsafe(24),
        broker_password=secrets.token_urlsafe(18),
    )
    for folder in (run.state, run.broker_files, run.logs, run.traces, run.ssh):
        folder.mkdir()
    return run
