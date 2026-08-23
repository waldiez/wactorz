"""The application, as a process.

Not the app imported and driven in-process - that is what the unit suite does,
and it cannot see the seams this suite exists for: the bind, the banner, the
signal handling, the log file, the fact that a second copy of the app is a
different process with its own state.

Every backend gets its own port and its own state directory, so scenarios that
want a private one can have it without disturbing the shared session backend.
Its whole console stream - stdout and stderr into one file, in order - is kept,
because `a01` asserts on the order of two lines in it.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import broker, waiting
from .probe import Rest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The line `_print_ready_banner` puts on stdout once everything is actually up.
READY_BANNER = "Dashboard   http://localhost:"


def free_ports(count: int) -> list[int]:
    """`count` ports nothing is listening on, all different from each other.

    Every socket is held open until the last one has been assigned, and that is
    the whole point. Allocating them one at a time - bind, read the number, close
    - lets the kernel hand back the port it just took back, so two calls in a row
    return the same number surprisingly often. A backend given one port for its
    API and its dashboard binds one, fails to bind the other, and stays up
    without a dashboard: the process is alive, so nothing looks crashed, and the
    run times out waiting for a health check that is never going to come.

    Still racy against the rest of the machine - something else can take a port
    between this returning and the child binding it - and that is accepted rather
    than papered over, because the alternative is fixed ports that collide with
    the developer's own running instance every time. That race is rare and says
    what happened; this one was neither.
    """
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [int(sock.getsockname()[1]) for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def die_with_parent() -> Callable[[], None] | None:
    """Popen arguments that make a child not outlive this process, where possible.

    Every backend is torn down by the fixture that started it, and that covers
    every ordinary ending including a failure. What it does not cover is the
    session being killed outright - a `timeout`, a Ctrl-C that lands badly, a
    crashed runner - after which the backend keeps running, holding its port and
    writing to a state directory nobody is watching any more.

    Linux can be told to send the child a signal when its parent dies. Elsewhere
    this is empty and the fixtures remain the only guarantee, which is the
    situation everywhere today.
    """
    if not sys.platform.startswith("linux"):
        return None

    def _set_pdeathsig() -> None:
        # 1 is PR_SET_PDEATHSIG. Failure here is not worth aborting a launch
        # over: the fixtures still tear the process down on every ordinary path.
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)

    return _set_pdeathsig


@dataclass
class Backend:
    """A running application process, and everything a scenario may ask of it."""

    process: subprocess.Popen[str]
    port: int
    api_port: int
    state_dir: Path
    console_log: Path
    rest: Rest

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def app_log(self) -> Path:
        """The application's own log file, which lives in the state directory."""
        return self.state_dir / "wactorz.log"

    def console(self) -> str:
        """Everything the process has written to stdout and stderr, in order."""
        return self.console_log.read_text(encoding="utf-8", errors="replace")

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def interrupt(self) -> float:
        """Send one SIGINT and return how long the process took to exit.

        One signal, and the number returned rather than asserted on here: `a09`
        owns the claim about how fast that has to be, and a harness that baked in
        a threshold would make the scenario's assertion a lie about where the
        requirement lives.
        """
        started = time.monotonic()
        self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
            raise AssertionError(
                "the backend did not exit within 30s of a single interrupt; it was killed"
            ) from None
        return time.monotonic() - started

    def kill(self) -> None:
        """Stop the process, whatever state it is in. For teardown, not scenarios."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)


def environment(
    *,
    state_dir: Path,
    port: int,
    api_port: int,
    llm: str,
    script: str = "",
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment a backend is started under.

    Built from a copy of the current one, then overridden. Copied so a machine's
    own settings (a proxy, a locale, a CA bundle) still apply; overridden so
    nothing the developer exported decides what a scenario tests. The overrides
    are the whole configuration surface this suite uses - anything a scenario
    needs to vary goes through `extra` and is visible in the scenario.
    """
    env = dict(os.environ)
    env.update(
        {
            "WACTORZ_STATE_DIR": str(state_dir),
            # Both, and this is not belt-and-braces. `--monitor-port` defaults to
            # MONITOR_PORT and falls back to WS_PORT, so setting only the latter
            # leaves MONITOR_PORT free for `.env` to answer - which is how a run
            # ends up on the developer's own 8888 instead of its allocated port.
            "WS_PORT": str(port),
            "MONITOR_PORT": str(port),
            "PORT": str(api_port),
            "INTERFACE": "rest",
            "WACTORZ_BIND_HOST": "127.0.0.1",
            "LLM_PROVIDER": llm,
            "MQTT_HOST": broker.HOST,
            "MQTT_PORT": str(broker.PORT),
            "MQTT_USERNAME": broker.USERNAME,
            "MQTT_PASSWORD": broker.PASSWORD,
            # Ordering in the console capture is the assertion in `a01`, and a
            # block-buffered child would deliver it in whatever order the flushes
            # happened to land.
            "PYTHONUNBUFFERED": "1",
        }
    )
    # Emptied, not deleted, and that distinction is the whole point. The app
    # loads the repository's `.env`, and `load_dotenv` fills in any variable that
    # is *absent* from the environment - so a deleted API_KEY comes straight back
    # from the developer's file and puts every scenario behind a sign-in page it
    # never agreed to. A variable that is present and empty is left alone.
    #
    # Everything here is a setting a developer plausibly has in `.env` and that
    # would change what a scenario tests. A scenario that wants one of them sets
    # it through `extra`, where it is visible in the scenario.
    for leaky in ("API_KEY", "LLM_FAKE_SCRIPT", "LLM_FAKE_INTENT", "WACTORZ_EXPOSED_OK"):
        env.setdefault(leaky, "")
        env[leaky] = ""
    if script:
        env["LLM_FAKE_SCRIPT"] = script
    if extra:
        env.update(extra)
    return env


def start(
    *,
    state_dir: Path,
    console_log: Path,
    llm: str = "fake",
    script: str = "",
    extra: Mapping[str, str] | None = None,
    port: int | None = None,
    api_port: int | None = None,
    wait_for_ready: bool = True,
) -> Backend:
    """Launch the application and, by default, wait until it serves `/health`.

    `wait_for_ready=False` is for the scenarios about starting badly - a refusal
    to bind, a broker that is not there - where waiting for a health check that
    is never going to pass would report the refusal as a timeout.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    console_log.parent.mkdir(parents=True, exist_ok=True)

    if port is None or api_port is None:
        allocated = free_ports(2)
        port = port or allocated[0]
        api_port = api_port or allocated[1]
    env = environment(
        state_dir=state_dir, port=port, api_port=api_port, llm=llm, script=script, extra=extra
    )

    handle = console_log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "wactorz"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        preexec_fn=die_with_parent(),
        # Merged into stdout so the capture is one ordered stream. `a01` asserts
        # the ready banner comes after the startup lines, and two files cannot
        # answer that question at all.
        stderr=subprocess.STDOUT,
        text=True,
    )
    backend = Backend(
        process=process,
        port=port,
        api_port=api_port,
        state_dir=state_dir,
        console_log=console_log,
        rest=Rest(f"http://127.0.0.1:{port}"),
    )
    if wait_for_ready:
        try:
            wait_until_ready(backend)
        except BaseException:
            # A backend that never became ready is still a backend that is
            # running. Without this it outlives the run entirely: the failure
            # propagates out of `start`, so the caller never gets the object it
            # would have torn down, and the process sits there holding its port
            # and its state directory until somebody notices it by hand.
            backend.kill()
            raise
    return backend


def wait_until_ready(backend: Backend, timeout: float = 60.0) -> None:
    """Wait for `/health`, and fail with the process's own output if it died.

    A backend that exits during startup would otherwise time out here and be
    reported as slow, with the actual reason - a port in use, a refused bind, a
    missing credential - sitting unread in the capture file.
    """

    def healthy() -> bool:
        if backend.process.poll() is not None:
            raise AssertionError(
                f"the backend exited with code {backend.process.returncode} during startup:\n"
                f"{backend.console()}"
            )
        return backend.rest.ok("/health")

    try:
        waiting.until(
            healthy, what=f"the backend on port {backend.port}", timeout=timeout, interval=0.2
        )
    except waiting.ConditionTimeout as exc:
        # A dashboard that fails to bind logs it and returns, leaving the agents
        # running - so the process is alive, nothing looks crashed, and a bare
        # timeout says nothing about a log that named the problem in its first
        # second. Attach the output rather than making someone go and find it.
        raise AssertionError(
            f"{exc}\nThe process is still running. Its output ends:\n{backend.console()[-3000:]}"
        ) from exc


#: The actors the application always starts. A system that is up but has not yet
#: reported these is a system a scenario would see mid-boot.
CORE_AGENTS = ("main", "catalog")


def wait_until_settled(backend: Backend, timeout: float = 120.0) -> None:
    """Wait until the system reports its own agents as running, not merely alive.

    `/health` answers as soon as the web server binds, which is well before the
    supervision tree has said anything about itself: agent state reaches the
    dashboard over MQTT, and the first status lands seconds after the port does.
    A scenario that started at `/health` would read `unknown` for every agent and
    assert against a system that was still coming up.

    This is a precondition of the shared backend rather than something scenarios
    repeat, so no scenario has to know that the first heartbeat is late.
    """

    def settled() -> bool:
        if backend.process.poll() is not None:
            raise AssertionError(
                f"the backend exited with code {backend.process.returncode} while settling:\n"
                f"{backend.console()}"
            )
        states = {a.get("name"): a.get("state") for a in backend.rest.agents()}
        return all(states.get(name) == "running" for name in CORE_AGENTS)

    waiting.until(
        settled,
        what=f"agents {', '.join(CORE_AGENTS)} to report themselves running",
        timeout=timeout,
        interval=0.5,
    )


@contextlib.contextmanager
def running(**kwargs: object) -> Iterator[Backend]:
    """A backend for the duration of a `with` block, stopped however it ends."""
    instance = start(**kwargs)  # type: ignore[arg-type]
    try:
        yield instance
    finally:
        instance.kill()
