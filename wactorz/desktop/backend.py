"""The Wactorz backend, run as a child process of the desktop shell.

Owns the child's lifetime and the probes that decide whether it can start at
all: the broker has to be reachable first (the backend exits without it), then
the shell waits for the HTTP server to answer before pointing the window at it.

The process handle lives here rather than in the shell, so "is it running?" has
one answer and nobody can leave a stale handle behind. Navigation decisions —
which page to show when a probe fails — stay with the shell; this module only
reports.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request

from wactorz.desktop import backend_config
from wactorz.desktop.config import BACKEND_LOG, DATA_DIR, FROZEN, PORT, URL

_process: subprocess.Popen | None = None


def is_running() -> bool:
    """True while the child is alive (`poll()` is None until it exits)."""
    return _process is not None and _process.poll() is None


def start() -> None:
    r"""Spawn the backend child, replacing any previous handle.

    Runs from a per-user writable dir: the backend writes wactorz.log,
    monitor.log and ./state relative to its cwd, which fails under an all-users
    install (e.g. C:\\Program Files). INTERFACE=rest makes it serve the web/REST
    UI on MONITOR_PORT — its default "cli" mode never binds one.
    """
    global _process
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # User config from the Configure view wins over the inherited shell/.env, so
    # a stale shell var can't shadow it. The shell-owned vars below win over both.
    env.update(backend_config.env_for_backend())
    env.update(
        MONITOR_PORT=str(PORT),
        INTERFACE="rest",
        WACTORZ_STATE_DIR=str(DATA_DIR / "state"),
    )
    cmd = [sys.executable, "--run-backend"] if FROZEN else [sys.executable, "-m", "wactorz"]
    try:
        # Both the log handle and the child outlive this call: a `with` block
        # would close the pipe and terminate the backend on return.
        # pylint: disable=consider-using-with
        log = open(BACKEND_LOG, "w", encoding="utf-8")  # noqa: SIM115
        _process = subprocess.Popen(
            cmd, env=env, cwd=str(DATA_DIR), stdout=log, stderr=subprocess.STDOUT
        )
    except Exception:  # pylint: disable=broad-exception-caught
        # Could not open the log file — still run, just without captured output.
        _process = subprocess.Popen(cmd, env=env, cwd=str(DATA_DIR))


def stop() -> None:
    """Terminate the child, escalating to kill if it ignores the signal."""
    global _process
    if _process and _process.poll() is None:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except Exception:  # pylint: disable=broad-exception-caught
            _process.kill()
    _process = None


def wait_until_serving(timeout: float = 30.0) -> bool:
    """Poll until the backend answers, or give up.

    Probes /health rather than "/" because "/" depends on the static assets and
    could 404, which would read as not-ready. Returns early if the child exits
    first — there is nothing left to wait for; see BACKEND_LOG for why.
    """
    health = f"{URL}/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _process is not None and _process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(health, timeout=1):
                return True
        except Exception:  # pylint: disable=broad-exception-caught
            time.sleep(0.15)
    return False


def mqtt_reachable(timeout: float = 3.0) -> bool:
    """TCP-probe the configured MQTT broker.

    The backend cannot start without it, so a quick check lets the shell open
    Configure straight away instead of waiting out the full startup timeout.
    """
    env = backend_config.env_for_backend()
    host = env.get("MQTT_HOST") or os.environ.get("MQTT_HOST") or "localhost"
    try:
        port = int(env.get("MQTT_PORT") or os.environ.get("MQTT_PORT") or 1883)
    except (TypeError, ValueError):
        port = 1883
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_mqtt(timeout: float = 25.0) -> bool:
    """Poll the broker until it answers or the timeout expires.

    Covers a broker (often a local container) still starting up at login: a
    single probe would lose that race and pop Configure even though it comes up
    a moment later.
    """
    deadline = time.time() + timeout
    while True:
        if mqtt_reachable(timeout=2.0):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(1.0)


def run_in_process() -> None:
    """Become the backend — the `<exe> --run-backend` entry used when frozen.

    A frozen build has no separate interpreter to exec, so the executable
    re-invokes itself with this flag instead of `python -m wactorz`.
    """
    import runpy

    sys.argv = ["wactorz"]
    runpy.run_module("wactorz", run_name="__main__")
