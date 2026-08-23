"""Reaching the broker, and - when it is ours to touch - taking it away.

Two different things live here and the difference is the whole point.

*Reaching* it is a precondition: every scenario needs a broker, and a run without
one checks nothing. That is enforced at the start of a run as an error.

*Stopping* it is a capability, and only for the broker this repository starts.
`a08` needs the broker gone to prove a local command still lands, and a suite
that stops whatever happens to be on port 1883 would take out the broker running
someone's house. So the controls below act on the named development container and
nothing else; when the reachable broker is not that container, the scenario that
needs it says so and skips - loudly, the way an absent node does.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

from . import waiting

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Compose file and service. The restart has to reuse the same config, and the
#: credentials have to be read the same way Compose reads them.
_COMPOSE_FILE = REPO_ROOT / "compose.dev.yaml"
_SERVICE = "mosquitto"

#: The container `make dev` starts. Named, not discovered: this is the one
#: broker the suite is allowed to stop.
CONTAINER = "wactorz-dev-mosquitto"


def _from_env_file(name: str) -> str:
    """The value `.env` gives this variable, or "" - a very small reader.

    Not a general dotenv parser and not trying to be. `docker compose` reads
    `.env` when it substitutes `${MQTT_PASSWORD}` into the broker's command, so
    the broker's actual password is whatever is in that file - and a harness that
    used the Compose *defaults* instead connects to a broker that refuses it,
    reporting a working system as a broken one. This reads the same file for the
    same two variables so the two agree.
    """
    path = REPO_ROOT / ".env"
    if not path.exists():
        return ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != name:
            continue
        # Trailing `# comment` and surrounding quotes, both of which .env uses.
        return value.split(" #")[0].strip().strip("\"'")
    return ""


def _setting(name: str, default: str) -> str:
    """Environment, then `.env`, then the Compose default - Compose's own order."""
    return os.getenv(name) or _from_env_file(name) or default


HOST = _setting("MQTT_HOST", "localhost")
PORT = int(_setting("MQTT_PORT", "1883"))
USERNAME = _setting("MQTT_USERNAME", "wactorz")
PASSWORD = _setting("MQTT_PASSWORD", "wactorz-dev")


def reachable(host: str = HOST, port: int = PORT, timeout: float = 0.5) -> bool:
    """Whether a TCP connection to the broker port completes.

    Deliberately not an MQTT connect: this answers "is the port answering", which
    is what both the precondition and the restart wait actually need. A broker
    that accepts TCP and refuses the credentials is a configuration failure, and
    it should surface as the backend failing to connect - with the broker's own
    message - rather than as a connectivity check that quietly returns False.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_until_reachable(timeout: float = 30.0) -> None:
    waiting.until(reachable, what=f"the broker on {HOST}:{PORT}", timeout=timeout, interval=0.2)


def wait_until_gone(timeout: float = 30.0) -> None:
    waiting.until(
        lambda: not reachable(),
        what=f"the broker on {HOST}:{PORT} to stop answering",
        timeout=timeout,
        interval=0.2,
    )


def controllable() -> bool:
    """Whether the reachable broker is the development container we may stop.

    False for a broker somebody else runs - a system mosquitto, one on another
    machine, one belonging to a home. `a08` skips on this rather than failing,
    because "we are not allowed to unplug this" is not a defect in the product.
    """
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return CONTAINER in result.stdout


def stop() -> None:
    """Stop the development broker and wait until the port stops answering.

    Waits rather than returning on the command's exit: `docker stop` returns when
    the container is stopped, but a scenario asserting "with the broker down"
    needs the socket to actually be refusing, and those are not the same instant.
    """
    _compose("stop", _SERVICE)
    wait_until_gone()


def start() -> None:
    """Bring the development broker back and wait until it answers again."""
    _compose("start", _SERVICE)
    wait_until_reachable()


def restart() -> None:
    stop()
    start()


def _compose(*args: str) -> None:
    result = subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`docker compose {' '.join(args)}` failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
