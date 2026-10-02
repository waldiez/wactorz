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

import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import broker, waiting
from .probe import Rest
from .run import NODE_NAME, REPO_ROOT, Run

#: Settings a developer plausibly has in `.env` that would change what a
#: scenario tests, and that this suite does not set itself.
LEFT_EMPTY = (
    "LLM_FAKE_INTENT",
    "LLM_API_KEY",
    "LLM_MODEL",
    "WACTORZ_EXPOSED_OK",
    "HA_URL",
    "HA_TOKEN",
    "HOME_ASSISTANT_URL",
    "HOME_ASSISTANT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "MQTT_TLS_CA",
    "MQTT_TLS_CHECK_HOSTNAME",
    "WACTORZ_LOG_FORMAT",
)

#: The line `_print_ready_banner` puts on stdout once everything is actually up.
READY_BANNER = "Dashboard   http://localhost:"


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
    #: What it was started with, so it can be started again the same.
    run: Run | None = None
    script: str = ""

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

    def restart(self) -> None:
        """Stop the process and start it again, on the same state, ports and settings.

        This object goes on standing for the application: what holds it does
        not have to be told there is a new process behind it.
        """
        assert self.run is not None
        self.kill()
        again = start(self.run, script=self.script)
        self.process = again.process

    def kill(self) -> None:
        """Stop the process, whatever state it is in: an interrupt, and a kill if that is not enough."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)


def environment(
    run: Run, *, script: str = "", extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment the backend of ``run`` is started under.

    Built from a copy of the current one, so a machine's own settings (a proxy,
    a locale, a CA bundle) still apply, then overridden with the whole
    configuration this suite uses. It is the configuration of a real install
    that deploys nodes: an API key, TLS to the broker, an account per node, and
    one deploy target.

    The application loads the repository's ``.env``, which fills in any variable
    that is *absent*. So what a developer may have set there and this suite does
    not use is set to nothing here, not removed: a variable that is present and
    empty is left alone.
    """
    env = dict(os.environ)
    for theirs in LEFT_EMPTY:
        env[theirs] = ""
    prefix = f"DEPLOY_{NODE_NAME.upper()}"
    env.update(
        {
            "WACTORZ_STATE_DIR": str(run.state),
            # Both: `--monitor-port` reads MONITOR_PORT and falls back to WS_PORT.
            "WS_PORT": str(run.ports.dashboard),
            "MONITOR_PORT": str(run.ports.dashboard),
            "PORT": str(run.ports.api),
            "INTERFACE": "rest",
            "WACTORZ_BIND_HOST": "127.0.0.1",
            "API_KEY": run.api_key,
            "LLM_PROVIDER": "fake",
            "LLM_FAKE_SCRIPT": script,
            "MQTT_HOST": "127.0.0.1",
            "MQTT_PORT": str(run.ports.broker),
            "MQTT_USERNAME": broker.USERNAME,
            "MQTT_PASSWORD": run.broker_password,
            "MQTT_TLS": "1",
            "MQTT_TLS_PORT": str(run.ports.broker_tls),
            "MQTT_BROKER_DIR": str(run.broker_files),
            "WACTORZ_NODE_ACCOUNTS": "1",
            "WACTORZ_NODE_SIGNING": "enforce",
            "DEPLOY_TARGETS": NODE_NAME,
            f"{prefix}_HOST": "127.0.0.1",
            f"{prefix}_SSH_PORT": str(run.ports.node_ssh),
            f"{prefix}_USER": "node",
            f"{prefix}_KEY": str(run.ssh / "id_ed25519"),
            # As the node sees the broker: by name, on the network they share.
            f"{prefix}_BROKER": broker.NAME_FOR_NODES,
            f"{prefix}_BROKER_PORT": "1883",
            f"{prefix}_BROKER_TLS_PORT": "8883",
            "DEPLOY_KNOWN_HOSTS": str(run.ssh / "known_hosts"),
            "DEPLOY_STRICT_HOST_KEYS": "0",
            # The console capture is read while the process runs.
            "PYTHONUNBUFFERED": "1",
        }
    )
    if extra:
        env.update(extra)
    return env


def start(
    run: Run,
    *,
    script: str = "",
    extra: Mapping[str, str] | None = None,
    wait_for_ready: bool = True,
) -> Backend:
    """Launch the application and, by default, wait until it serves ``/health``.

    The console capture is appended to, so a backend started again in the same
    run continues the file the first one wrote.
    """
    console_log = run.logs / "backend.log"
    handle = console_log.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "wactorz"],
        cwd=REPO_ROOT,
        env=environment(run, script=script, extra=extra),
        stdin=subprocess.DEVNULL,
        stdout=handle,
        preexec_fn=die_with_parent(),
        # Merged into stdout so the capture is one ordered stream.
        stderr=subprocess.STDOUT,
        text=True,
    )
    backend = Backend(
        process=process,
        port=run.ports.dashboard,
        api_port=run.ports.api,
        state_dir=run.state,
        console_log=console_log,
        rest=Rest(f"http://127.0.0.1:{run.ports.dashboard}", api_key=run.api_key),
        run=run,
        script=script,
    )
    if wait_for_ready:
        try:
            wait_until_ready(backend)
            wait_until_settled(backend)
        except BaseException:
            # A backend that never became ready is still running, and the
            # caller never gets the object it would have stopped it with.
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
