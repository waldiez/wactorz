"""A second process standing in for a machine at the edge.

`remote_runner.py` is not a module of the application - it is a single file that
gets copied to a Raspberry Pi on its own and run there, with no `wactorz` package
anywhere near it. That property is what makes the deployment story work, and it
is invisible to every test that imports the runner rather than running it.

So the node here is started the way a deployed one is, and under two conditions
that make the property fail loudly rather than silently:

*Copied out of the repository.* The runner runs from a directory that contains
nothing else, so a sibling module it accidentally relies on is simply absent.

*With `wactorz` made unimportable.* The copy alone proves nothing on a developer
machine, where the package is pip-installed and importable from anywhere - the
runner would import it happily and the check would pass while the property was
broken. A `sitecustomize` on the path refuses the import instead, so the runner
that reaches for it dies with a message saying exactly that.

Both apply to every node this suite starts, not just the scenario that asserts
the property. A rule enforced only where it is tested is a rule that decays.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import backend, broker, waiting
from .probe import Rest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_SOURCE = REPO_ROOT / "wactorz" / "remote_runner.py"

#: Installed on the runner's path, refusing the one import that must not happen.
#: `sitecustomize` because it runs before the script does, so the refusal is in
#: place no matter how early the import is attempted.
_GUARD = '''\
"""Refuses `import wactorz` in this process. Installed by the e2e harness.

The runner is deployed as a single file to machines that have no wactorz package.
On a developer machine it is pip-installed and importable, which would hide a
regression in that property behind an import that happens to succeed.
"""

import sys


class _NoWactorz:
    def find_module(self, name, path=None):
        return self.find_spec(name, path)

    def find_spec(self, name, path=None, target=None):
        if name == "wactorz" or name.startswith("wactorz."):
            raise ImportError(
                f"remote_runner imported {name!r}. It is deployed to edge nodes as a "
                f"single file with no wactorz package alongside it, so this import "
                f"cannot work there. Keep the runner self-contained."
            )
        return None


sys.meta_path.insert(0, _NoWactorz())
'''


@dataclass
class Node:
    """A running edge-node runner."""

    process: subprocess.Popen[str]
    name: str
    workdir: Path
    console_log: Path

    def console(self) -> str:
        return self.console_log.read_text(encoding="utf-8", errors="replace")

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def is_listed_by(self, rest: Rest) -> bool:
        """Whether the backend currently reports this node as online.

        Reads the dashboard's node list, which is the only place nodes appear.
        """
        for entry in rest.nodes():
            if entry.get("node") == self.name or entry.get("name") == self.name:
                return bool(entry.get("online", True))
        return False

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)


def start(*, name: str, workdir: Path, console_log: Path) -> Node:
    """Copy the runner somewhere on its own and run it there."""
    workdir.mkdir(parents=True, exist_ok=True)
    console_log.parent.mkdir(parents=True, exist_ok=True)

    runner = workdir / "remote_runner.py"
    shutil.copy2(RUNNER_SOURCE, runner)
    (workdir / "sitecustomize.py").write_text(_GUARD, encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "MQTT_USERNAME": broker.USERNAME,
            "MQTT_PASSWORD": broker.PASSWORD,
            # The guard has to be found before anything else, and the working
            # directory has to be the copy - not the repository, whose `wactorz/`
            # would otherwise be one relative import away.
            "PYTHONPATH": str(workdir),
            "PYTHONUNBUFFERED": "1",
        }
    )

    handle = console_log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            str(runner),
            "--broker",
            broker.HOST,
            "--port",
            str(broker.PORT),
            "--name",
            name,
        ],
        cwd=workdir,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        # Same reasoning as the backend's: a session killed outright would
        # otherwise leave a runner connected to the broker indefinitely.
        preexec_fn=backend.die_with_parent(),
    )
    return Node(process=process, name=name, workdir=workdir, console_log=console_log)


def wait_until_listed(node: Node, rest: Rest, timeout: float = 90.0) -> None:
    """Wait for the backend to report the node online, or say why it will not.

    A runner that died - because it reached for the package it must not need, or
    because the broker refused it - would otherwise be reported as a node that is
    merely slow, with the actual message sitting in a file nobody opened.
    """

    def listed() -> bool:
        if node.process.poll() is not None:
            raise AssertionError(
                f"the node runner exited with code {node.process.returncode} before "
                f"appearing:\n{node.console()}"
            )
        return node.is_listed_by(rest)

    waiting.until(listed, what=f"node {node.name!r} to come online", timeout=timeout, interval=0.5)


@contextlib.contextmanager
def running(*, name: str, workdir: Path, console_log: Path) -> Iterator[Node]:
    instance = start(name=name, workdir=workdir, console_log=console_log)
    try:
        yield instance
    finally:
        instance.stop()
