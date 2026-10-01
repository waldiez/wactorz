"""What stops running is seen, and said, by something that is still running.

Three ways a process looks alive while doing nothing, each invisible to what
already watched it:

- An actor waiting for ever on one message. Its heartbeat is a task of its own
  and carries on, so the supervisor and the dashboard see a healthy actor.
- Code that blocks the event loop. Every actor stops at once, and nothing on
  the loop can report it, the loop being what stopped.
- A node frozen that way. Its service manager sees a process that exists.

So an actor says how long it has been on its current message, a thread times
the loop and names the code holding it, and a node tells systemd its loop is
running, to be restarted when it stops saying so.
"""

import asyncio
import contextlib
import logging
import os
import re
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.waiting import PATIENCE_S, until
from wactorz.agents import node_service
from wactorz.core import sd_notify
from wactorz.core.actor import Actor, Message, MessageType
from wactorz.monitoring.loop_lag import LAG, LoopLagMonitor
from wactorz.monitoring.prometheus import PrometheusMonitor

# ── An actor stuck on one message ──────────────────────────────────────────────


class _Slow(Actor):
    """Handles a message for as long as the test holds it there."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_message(self, msg: Message) -> None:
        self.started.set()
        await self.release.wait()


class TestAnActorOnOneMessage:
    def test_an_idle_actor_reports_nothing(self) -> None:
        assert _Slow(name="idle").handling_seconds == 0.0

    async def test_it_counts_for_as_long_as_the_message_takes(self) -> None:
        actor = _Slow(name="busy")
        loop = asyncio.create_task(actor._message_loop())
        try:
            await actor.receive(Message(type=MessageType.TASK, sender_id="s", payload={}))
            await actor.started.wait()

            await until(lambda: actor.handling_seconds >= 0.2, "the time on the message growing")

            actor.release.set()
            await until(lambda: actor.handling_seconds == 0.0, "the actor going idle again")
        finally:
            loop.cancel()

    async def test_a_handler_that_fails_does_not_leave_it_counting(self) -> None:
        class _Failing(Actor):
            async def handle_message(self, msg: Message) -> None:
                raise RuntimeError("no")

        actor = _Failing(name="failing")
        loop = asyncio.create_task(actor._message_loop())
        try:
            await actor.receive(Message(type=MessageType.TASK, sender_id="s", payload={}))
            await until(lambda: actor.metrics.errors >= 1, "the handler having failed")

            assert actor.handling_seconds == 0.0
        finally:
            loop.cancel()

    async def test_metrics_carry_it(self) -> None:
        actor = _Slow(name="busy")
        actor._handling_since = time.monotonic() - 42
        registry = type("Registry", (), {"all_actors": lambda self: [actor]})()

        rendered = PrometheusMonitor(lambda: registry).render().decode()

        (line,) = [
            line
            for line in rendered.splitlines()
            if line.startswith('wactorz_actor_handling_seconds{actor_name="busy"}')
        ]
        assert 42 <= float(line.rsplit(" ", 1)[1]) < 60


# ── A blocked event loop ───────────────────────────────────────────────────────


def _observed() -> float:
    """How many times the loop's lag has been recorded."""
    return next(
        sample.value
        for metric in LAG.collect()
        for sample in metric.samples
        if sample.name.endswith("_count")
    )


def _holds_the_loop(until_true: Callable[[], bool], limit: float = PATIENCE_S) -> None:
    """Blocking work on the event loop, as a careless handler would do.

    Held until ``until_true`` says so rather than for a length of time, so the
    test does not depend on how soon the watching thread gets to run.
    """
    deadline = time.monotonic() + limit
    while not until_true() and time.monotonic() < deadline:
        time.sleep(0.01)


def _reported(caplog: pytest.LogCaptureFixture) -> bool:
    return "The event loop has not run for" in caplog.text


class TestTheEventLoop:
    async def test_a_loop_that_is_running_has_its_lag_recorded(self) -> None:
        before = _observed()
        monitor = LoopLagMonitor(interval=0.05, report_after=PATIENCE_S)
        monitor.start()
        try:
            await until(lambda: _observed() > before, "a lag being recorded")
        finally:
            monitor.stop()

    async def test_a_blocked_loop_is_reported_with_the_code_holding_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="wactorz.monitoring.loop_lag")
        monitor = LoopLagMonitor(interval=0.05, report_after=0.3)
        monitor.start()
        try:
            _holds_the_loop(lambda: _reported(caplog))
            await until(lambda: "running again after" in caplog.text, "the loop being seen again")
        finally:
            monitor.stop()

        assert _reported(caplog)
        # The stack of the loop's own thread, taken while it was stuck.
        assert "_holds_the_loop" in caplog.text
        assert "time.sleep(0.01)" in caplog.text
        # And how long the whole of it lasted, once it was over.
        resumed = re.search(r"running again after (\d+\.\d)s", caplog.text)
        assert resumed is not None
        assert float(resumed.group(1)) >= 0.3

    async def test_one_block_is_reported_once(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.WARNING, logger="wactorz.monitoring.loop_lag")
        monitor = LoopLagMonitor(interval=0.05, report_after=0.2)
        monitor.start()
        try:
            _holds_the_loop(lambda: _reported(caplog))
            # Still held, for several times as long again as it took to report.
            held_until = time.monotonic() + 1.0
            _holds_the_loop(lambda: time.monotonic() >= held_until)
            await until(lambda: "running again after" in caplog.text, "the loop being seen again")
        finally:
            monitor.stop()
        await asyncio.sleep(0.5)

        assert caplog.text.count("The event loop has not run for") == 1

    async def test_stopping_does_not_hold_the_loop(self, caplog: pytest.LogCaptureFixture) -> None:
        # The thread is waiting for the loop to answer it. A stop that waited
        # for the thread, from the loop, would hold the loop until the thread
        # gave up -- and have it report that hold as a block.
        caplog.set_level(logging.WARNING, logger="wactorz.monitoring.loop_lag")
        monitor = LoopLagMonitor(interval=0.05, report_after=3.0)
        monitor.start()
        thread = monitor._thread
        assert thread is not None
        _holds_the_loop(lambda: _reported(caplog))

        started = time.monotonic()
        monitor.stop()
        took = time.monotonic() - started

        assert took < 1.5, "stop returned without waiting out the thread"
        await until(lambda: not thread.is_alive(), "the watching thread ending")
        assert caplog.text.count("The event loop has not run for") == 1

    async def test_stopping_again_and_again_never_reports_a_block(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Whatever the thread is in the middle of when the stop arrives. The
        # window that mattered was a narrow one, between two of its steps, so
        # it is looked for by stopping many monitors rather than by timing one.
        caplog.set_level(logging.WARNING, logger="wactorz.monitoring.loop_lag")

        for _ in range(500):
            monitor = LoopLagMonitor(interval=0.0005, report_after=0.05)
            monitor.start()
            await asyncio.sleep(0.002)
            monitor.stop()
        await asyncio.sleep(0.2)

        assert not _reported(caplog)

    async def test_a_monitor_started_again_watches_with_one_thread(self) -> None:
        monitor = LoopLagMonitor(interval=0.05)
        monitor.start()
        first = monitor._thread
        monitor.stop()

        monitor.start()
        try:
            assert first is not None
            await until(lambda: not first.is_alive(), "the first thread ending")
            assert monitor._thread is not first
        finally:
            monitor.stop()

    async def test_starting_twice_and_stopping_twice_are_harmless(self) -> None:
        monitor = LoopLagMonitor(interval=0.05)
        monitor.start()
        thread = monitor._thread
        monitor.start()

        assert monitor._thread is thread

        monitor.stop()
        monitor.stop()

    def test_stopping_one_that_never_started_is_harmless(self) -> None:
        LoopLagMonitor().stop()


# ── systemd's watchdog ─────────────────────────────────────────────────────────

needs_unix_sockets = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="systemd's notification socket is a Unix socket"
)


@pytest.fixture(name="no_systemd")
def no_systemd_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(name="systemd")
def systemd_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A socket standing in for systemd's, which the process is told about.

    Named by a path relative to the test's own directory. A socket's path has a
    short limit, shorter on macOS than the temporary directory a test is given
    there, so the full path cannot be bound at all.
    """
    monkeypatch.chdir(tmp_path)
    path = "notify"
    listening = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listening.bind(path)
    listening.settimeout(5)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    yield listening
    listening.close()


class TestTellingSystemd:
    def test_with_nobody_to_tell_it_does_nothing(self, no_systemd: None) -> None:
        assert sd_notify.notify("WATCHDOG=1") is False

    @needs_unix_sockets
    def test_the_message_reaches_the_socket_systemd_named(self, systemd: socket.socket) -> None:
        assert sd_notify.notify("WATCHDOG=1") is True
        assert systemd.recv(64) == b"WATCHDOG=1"

    @pytest.mark.skipif(sys.platform != "linux", reason="abstract sockets are Linux's")
    def test_a_socket_in_the_abstract_namespace_is_reached_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        name = f"wactorz-test-{os.getpid()}"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listening:
            listening.bind("\0" + name)
            listening.settimeout(5)
            monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)

            assert sd_notify.notify("WATCHDOG=1") is True
            assert listening.recv(64) == b"WATCHDOG=1"

    def test_a_socket_that_is_not_there_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "gone"))

        assert sd_notify.notify("WATCHDOG=1") is False


class TestHowOftenSystemdExpectsToHear:
    def test_never_when_no_watchdog_is_set(self, no_systemd: None) -> None:
        assert sd_notify.watchdog_interval() is None

    def test_as_often_as_it_says(self, no_systemd: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "300000000")

        assert sd_notify.watchdog_interval() == 300.0

    def test_not_when_the_watchdog_is_for_another_process(
        self, no_systemd: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The variables are inherited: a child must not answer for its parent.
        monkeypatch.setenv("WATCHDOG_USEC", "300000000")
        monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))

        assert sd_notify.watchdog_interval() is None

    @pytest.mark.parametrize("value", ["", "soon", "-5", "0"])
    def test_a_value_that_is_not_an_interval_means_none(
        self, no_systemd: None, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", value)

        assert sd_notify.watchdog_interval() is None


class TestAnsweringTheWatchdog:
    async def test_without_one_the_loop_returns_at_once(self, no_systemd: None) -> None:
        await asyncio.wait_for(sd_notify.watchdog_loop(), timeout=PATIENCE_S)

    @needs_unix_sockets
    async def test_it_answers_at_half_the_interval_for_as_long_as_it_runs(
        self, systemd: socket.socket, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "200000")  # 0.2s, so answered every 0.1s
        systemd.setblocking(False)
        received: list[bytes] = []

        def _heard_three_times() -> bool:
            with contextlib.suppress(BlockingIOError):
                while True:
                    received.append(systemd.recv(64))
            return len(received) >= 3

        answering = asyncio.create_task(sd_notify.watchdog_loop())
        try:
            await until(_heard_three_times, "systemd hearing from the loop three times")
        finally:
            answering.cancel()

        assert set(received) == {b"WATCHDOG=1"}


@pytest.mark.parametrize("system", [True, False])
class TestTheNodesUnit:
    def test_it_asks_systemd_to_watch_it(self, system: bool) -> None:
        unit = node_service.unit_file("/home/pi", "pi", system=system)
        service = unit.split("[Service]", 1)[1].split("[Install]", 1)[0]

        assert f"WatchdogSec={node_service.WATCHDOG_S}" in service.splitlines()
        # Only the node's own process may answer, not something it started.
        assert "NotifyAccess=main" in service.splitlines()

    def test_a_frozen_node_is_killed_without_a_core_dump(self, system: bool) -> None:
        # systemd aborts by default, and writes the process out to disk.
        unit = node_service.unit_file("/home/pi", "pi", system=system)

        assert "WatchdogSignal=SIGKILL" in unit.splitlines()

    def test_a_missed_watchdog_restarts_it(self, system: bool) -> None:
        # systemd counts a watchdog timeout as a failure, which is the one
        # restart policy the unit has.
        unit = node_service.unit_file("/home/pi", "pi", system=system)

        assert "Restart=on-failure" in unit

    def test_it_allows_minutes_not_seconds(self, system: bool) -> None:
        # A slow board under load must not be taken for a frozen one.
        assert node_service.WATCHDOG_S >= 120
