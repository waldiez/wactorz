"""Following the log live, without the stream feeding itself.

An open page keeps up with the log instead of showing whatever it fetched when
it loaded.

The hazard is a loop rather than a leak: broadcasting can log — a failing send
certainly does — and a record written by the push path is one the next tick
would push, which fails again, at the poll rate forever. Records from the
loggers doing the sending are therefore never pushed. They stay in the buffer
and `/api/logs` still serves them; they simply do not feed the thing that made
them.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import pytest

from wactorz.monitoring import log_buffer
from wactorz.web import log_stream, runtime


@pytest.fixture(name="buffer")
def buffer_fixture() -> Any:
    """A real ring buffer attached to the root logger, removed afterwards."""
    log_buffer.uninstall()
    buffer = log_buffer.install(capacity=50)
    yield buffer
    log_buffer.uninstall()


@pytest.fixture(name="sent")
def sent_fixture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Records what the pump broadcasts, with a client connected."""
    frames: list[dict[str, Any]] = []

    async def _broadcast(msg: dict[str, Any]) -> None:
        frames.append(msg)

    monkeypatch.setattr(log_stream.ws, "broadcast", _broadcast)
    monkeypatch.setattr(runtime, "ws_clients", {object()})
    return frames


def _interval() -> float:
    """A tick short enough to keep the suite quick, long enough to happen.

    asyncio schedules on the monotonic clock, which Windows resolves to ~15.6ms
    against Linux's ~1ms — a sleep shorter than that resolution returns when the
    clock next moves rather than when asked, so a fixed 10ms window turned the
    pump's first tick into a race it sometimes lost. Scaling off the clock keeps
    the Linux timing as it was and stretches it only where it has to stretch.
    """
    return max(0.01, time.get_clock_info("monotonic").resolution * 3)


async def _started(interval: float) -> asyncio.Task[None]:
    """Start the pump and return once it has taken its starting position.

    The loop reads the buffer's length before its first sleep, so a record
    written after this point is one it considers new. Yielding to the event loop
    rather than sleeping a fixed time makes that ordering exact instead of
    merely likely — which is what the old fixed delay was buying.
    """
    task = asyncio.create_task(log_stream.log_push_loop(interval=interval))
    await asyncio.sleep(0)
    return task


async def _stop(task: asyncio.Task[None]) -> None:
    """Cancel the pump and wait for it to finish unwinding."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _pump_until(
    frames: list[dict[str, Any]],
    write: Callable[[], None],
    count: int = 1,
    timeout: float = 5.0,
) -> None:
    """Run the pump, write, and return as soon as `count` frames have arrived.

    Driven to an outcome rather than for a duration: waiting a fixed number of
    milliseconds turns any slowdown — coverage tracing, a loaded machine — into
    a failure that says the pump stopped when it had only been slow. The timeout
    is the backstop, so a pump that has genuinely stopped still fails its
    assertion rather than hanging.
    """
    interval = _interval()
    task = await _started(interval)
    try:
        write()
        deadline = time.monotonic() + timeout
        while len(frames) < count and time.monotonic() < deadline:
            await asyncio.sleep(interval / 2)
    finally:
        await _stop(task)


async def _pump_briefly(write: Callable[[], None] | None = None, times: float = 4.0) -> None:
    """Run the pump for roughly `times` ticks, then stop it.

    For the cases that assert nothing was pushed: there is no arrival to wait
    for, so the only option is to run it a while and look. A slow machine makes
    these wait longer than they need to, never fail.
    """
    interval = _interval()
    task = await _started(interval)
    try:
        if write is not None:
            write()
        await asyncio.sleep(interval * times)
    finally:
        await _stop(task)


class TestWhatIsPushed:
    async def test_a_record_written_while_connected_is_broadcast(
        self, buffer: Any, sent: list[dict[str, Any]]
    ) -> None:
        await _pump_until(
            sent, lambda: logging.getLogger("wactorz.demo").warning("something happened")
        )

        texts = [e["text"] for frame in sent for e in frame["entries"]]
        assert "something happened" in texts

    async def test_the_frame_says_what_it_is(self, buffer: Any, sent: list[dict[str, Any]]) -> None:
        await _pump_until(sent, lambda: logging.getLogger("wactorz.demo").error("boom"))

        assert sent and sent[0]["type"] == "app_log"

    async def test_history_is_not_replayed(self, buffer: Any, sent: list[dict[str, Any]]) -> None:
        # The page fetches its own history from /api/logs on load. Replaying it
        # here would double every row on every reconnect.
        logging.getLogger("wactorz.demo").warning("logged before the pump started")

        await _pump_briefly()

        assert not sent

    async def test_nothing_is_sent_when_nobody_is_watching(
        self, buffer: Any, sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "ws_clients", set())

        await _pump_briefly(lambda: logging.getLogger("wactorz.demo").warning("into the void"))

        assert not sent


class TestTheLoopItMustNotEnter:
    async def test_records_from_the_push_path_are_never_pushed(
        self, buffer: Any, sent: list[dict[str, Any]]
    ) -> None:
        # This is the feedback loop: a failing send logs, that record is pushed,
        # the send fails again. Excluding these breaks it at the source.
        def _write() -> None:
            logging.getLogger(log_stream.__name__).error("send failed")
            logging.getLogger("aiohttp.server").error("connection reset")

        await _pump_briefly(_write)

        pushed = [e["text"] for frame in sent for e in frame["entries"]]
        assert "send failed" not in pushed
        assert "connection reset" not in pushed

    async def test_but_they_are_still_in_the_buffer(self, buffer: Any) -> None:
        # Excluded from the live stream only — `/api/logs` still serves them, or
        # the failure would be invisible exactly when it matters.
        logging.getLogger(log_stream.__name__).error("send failed")

        assert any(e["text"] == "send failed" for e in buffer.snapshot())


class TestBursts:
    async def test_one_frame_is_capped(self, buffer: Any, sent: list[dict[str, Any]]) -> None:
        # A traceback storm should not become a single huge message that stalls
        # every connected client.
        def _write() -> None:
            for i in range(log_stream.MAX_PER_FRAME + 40):
                logging.getLogger("wactorz.demo").warning("line %d", i)

        await _pump_until(sent, _write)

        assert all(len(frame["entries"]) <= log_stream.MAX_PER_FRAME for frame in sent)


class TestWithoutABuffer:
    async def test_the_pump_returns_rather_than_failing(self, sent: list[dict[str, Any]]) -> None:
        # An embedding app may never have called install(). Nothing records, so
        # nothing streams — not an error.
        log_buffer.uninstall()

        await _pump_briefly()

        assert not sent
