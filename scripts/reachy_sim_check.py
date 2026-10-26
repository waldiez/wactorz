"""Run the Reachy Mini agent against a simulated robot, with no hardware.

Starts the robot SDK's own daemon in mock-up simulation (no MuJoCo, no camera,
no audio), loads the `reachy-mini` catalogue program exactly as Wactorz does,
and drives it through connection, motion, stop, sleep and reconnect, printing
how long each step took. `--recovery` also stops the daemon mid-session and
checks that the agent reconnects by itself once it is back.

What it proves: the agent's own code and the pinned SDK work together end to
end. What it does not: anything about real motors, the speaker, the microphone,
the camera, the network to a Wireless robot, or speech services. Those still
need the robot (see docs/reachy/internal/test_report.md).

Needs the `reachy` extra:

    uv run --no-sync python scripts/reachy_sim_check.py [--recovery]

The daemon listens on 127.0.0.1:8000 for the run, so nothing else may use that
port, and it is stopped when the run ends.
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Any

import psutil

# Before the agent program is loaded, so its setup() reads them: local mode
# reaches the daemon on localhost, and the simulated daemon has no media.
os.environ["REACHY_CONNECTION_MODE"] = "local"
os.environ["REACHY_MEDIA_BACKEND"] = "no_media"
# The gesture library is a dataset download; the run works without it.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE

PORT = 8000
STATUS_URL = f"http://127.0.0.1:{PORT}/api/daemon/status"

#: (label, command, payload) in the order a person would try them.
STEPS: list[tuple[str, str, dict[str, Any]]] = [
    ("health", "health", {}),
    ("wake", "wake", {}),
    ("pose yaw 20", "pose", {"yaw": 20, "duration": 0.5}),
    ("antennas", "antennas", {"left": 30, "right": -30, "duration": 0.3}),
    ("nod", "gesture", {"name": "nod"}),
    ("look_at", "look_at", {"x": 0.5, "y": 0.1, "z": 0.1, "duration": 0.5}),
    ("turn left 30", "turn", {"angle": 30}),
    ("face forward", "face_forward", {}),
    ("stop", "stop", {}),
    ("sleep", "sleep", {}),
    ("reconnect force", "reconnect", {"force": True, "quiet": True}),
    ("wake after reconnect", "wake", {}),
]


class SimAgent:
    """The part of the Wactorz agent API the Reachy program uses, kept in memory."""

    name = "reachy-mini"
    llm = None

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.store: dict[str, Any] = {}
        self.logs: list[tuple[str, str]] = []
        self.tasks: list[asyncio.Task[Any]] = []

    def recall(self, key: str, default: Any = None) -> Any:
        return self.store.get(key, default)

    def persist(self, key: str, value: Any) -> None:
        self.store[key] = value

    def subscribe(self, topic: str, callback: Any) -> None:
        del topic, callback

    async def publish(self, topic: str, payload: Any) -> None:
        del topic, payload

    async def log(self, text: str, level: str = "info") -> None:
        self.logs.append((level, text))

    async def alert(self, text: str, severity: str = "info") -> None:
        self.logs.append((severity, text))

    async def notify_user(self, text: str, **extra: Any) -> None:
        del extra
        self.logs.append(("chat", text))

    def run_in_background(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


def _daemon_command() -> list[str]:
    exe = shutil.which("reachy-mini-daemon")
    if exe is None:
        sys.exit("reachy-mini-daemon is not on PATH; install the reachy extra and use its venv")
    return [
        exe,
        "--mockup-sim",
        "--headless",
        "--no-media",
        "--no-preload-datasets",
        "--fastapi-host",
        "127.0.0.1",
        "--fastapi-port",
        str(PORT),
        "--log-level",
        "WARNING",
    ]


def _daemon_ready() -> bool:
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def start_daemon(timeout_s: float = 60.0) -> subprocess.Popen[bytes]:
    if _daemon_ready():
        sys.exit(f"something already answers on port {PORT}; stop it and run again")
    daemon = subprocess.Popen(
        _daemon_command(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _daemon_ready():
            return daemon
        if daemon.poll() is not None:
            sys.exit(f"the simulated daemon exited with code {daemon.returncode}")
        time.sleep(0.5)
    stop_daemon(daemon)
    sys.exit(f"the simulated daemon did not answer within {timeout_s:.0f}s")


def stop_daemon(daemon: subprocess.Popen[bytes]) -> None:
    """Stop the daemon and the interpreter its launcher started."""
    try:
        parent = psutil.Process(daemon.pid)
        processes = [*parent.children(recursive=True), parent]
    except psutil.NoSuchProcess:
        return
    for process in processes:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(processes, timeout=10)


async def step(label: str, coro: Any) -> bool:
    started = time.perf_counter()
    try:
        result = await coro
        ok = result.get("ok", True) if isinstance(result, dict) else True
        detail = (result.get("result") or result.get("error")) if isinstance(result, dict) else ""
    except Exception as exc:  # reported, and the run goes on
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    took_ms = (time.perf_counter() - started) * 1000
    first_line = str(detail or "").splitlines()[0] if detail else ""
    print(f"{'PASS' if ok else 'FAIL'}  {label:<28} {took_ms:7.0f} ms  {first_line[:90]}")
    return ok


async def check_recovery(ns: dict[str, Any], agent: SimAgent, daemon: Any) -> tuple[bool, Any]:
    """Stop the daemon, then bring it back and wait for the agent to recover alone."""
    stop_daemon(daemon)
    await asyncio.sleep(2)
    ok = not (await ns["_dispatch"](agent, "wake", {}, True)).get("ok")
    print(
        f"{'PASS' if ok else 'FAIL'}  {'command while daemon is down':<28} refused, recovery started"
    )
    daemon = start_daemon()
    started = time.monotonic()
    while time.monotonic() - started < 90:
        await asyncio.sleep(1)
        if not agent.state.get("motion_link_error") and agent.state.get("awake"):
            print(f"PASS  {'recovered without a command':<28} {time.monotonic() - started:7.0f} s")
            return ok, daemon
    print(f"FAIL  {'recovered without a command':<28} not within 90 s")
    return False, daemon


async def run(recovery: bool) -> int:
    ns: dict[str, Any] = {}
    exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), ns)  # noqa: S102  # as CatalogAgent does
    daemon = start_daemon()
    agent = SimAgent()
    results: list[bool] = []
    try:
        results.append(await step("connect and bring up", ns["setup"](agent)))
        for label, cmd, payload in STEPS:
            results.append(await step(label, ns["_dispatch"](agent, cmd, payload, True)))
        if recovery:
            ok, daemon = await check_recovery(ns, agent, daemon)
            results.append(ok)
        results.append(await step("cleanup", ns["cleanup"](agent)))
    finally:
        for task in agent.tasks:
            task.cancel()
        await asyncio.gather(*agent.tasks, return_exceptions=True)
        stop_daemon(daemon)
    print(f"\n{sum(results)}/{len(results)} steps passed (simulated robot; no hardware tested)")
    return 0 if all(results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Reachy Mini agent against a simulated robot, with no hardware."
    )
    parser.add_argument(
        "--recovery", action="store_true", help="also stop and restart the daemon mid-session"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.recovery)))


if __name__ == "__main__":
    main()
