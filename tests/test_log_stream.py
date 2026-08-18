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


async def _pump_once(interval: float = 0.01) -> None:
    """Run the loop long enough for one tick, then stop it."""
    task = asyncio.create_task(log_stream.log_push_loop(interval=interval))
    await asyncio.sleep(interval * 4)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class TestWhatIsPushed:
    async def test_a_record_written_while_connected_is_broadcast(
        self, buffer: Any, sent: list[dict[str, Any]]
    ) -> None:
        async def _log_soon() -> None:
            await asyncio.sleep(0.015)
            logging.getLogger("wactorz.demo").warning("something happened")

        logger_task = asyncio.create_task(_log_soon())
        await _pump_once()
        await logger_task

        texts = [e["text"] for frame in sent for e in frame["entries"]]
        assert "something happened" in texts

    async def test_the_frame_says_what_it_is(self, buffer: Any, sent: list[dict[str, Any]]) -> None:
        async def _log_soon() -> None:
            await asyncio.sleep(0.015)
            logging.getLogger("wactorz.demo").error("boom")

        task = asyncio.create_task(_log_soon())
        await _pump_once()
        await task

        assert sent and sent[0]["type"] == "app_log"

    async def test_history_is_not_replayed(self, buffer: Any, sent: list[dict[str, Any]]) -> None:
        # The page fetches its own history from /api/logs on load. Replaying it
        # here would double every row on every reconnect.
        logging.getLogger("wactorz.demo").warning("logged before the pump started")

        await _pump_once()

        assert not sent

    async def test_nothing_is_sent_when_nobody_is_watching(
        self, buffer: Any, sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "ws_clients", set())

        async def _log_soon() -> None:
            await asyncio.sleep(0.015)
            logging.getLogger("wactorz.demo").warning("into the void")

        task = asyncio.create_task(_log_soon())
        await _pump_once()
        await task

        assert not sent


class TestTheLoopItMustNotEnter:
    async def test_records_from_the_push_path_are_never_pushed(
        self, buffer: Any, sent: list[dict[str, Any]]
    ) -> None:
        # This is the feedback loop: a failing send logs, that record is pushed,
        # the send fails again. Excluding these breaks it at the source.
        async def _log_soon() -> None:
            await asyncio.sleep(0.015)
            logging.getLogger(log_stream.__name__).error("send failed")
            logging.getLogger("aiohttp.server").error("connection reset")

        task = asyncio.create_task(_log_soon())
        await _pump_once()
        await task

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
        async def _log_soon() -> None:
            await asyncio.sleep(0.015)
            for i in range(log_stream.MAX_PER_FRAME + 40):
                logging.getLogger("wactorz.demo").warning("line %d", i)

        task = asyncio.create_task(_log_soon())
        await _pump_once()
        await task

        assert all(len(frame["entries"]) <= log_stream.MAX_PER_FRAME for frame in sent)


class TestWithoutABuffer:
    async def test_the_pump_returns_rather_than_failing(self, sent: list[dict[str, Any]]) -> None:
        # An embedding app may never have called install(). Nothing records, so
        # nothing streams — not an error.
        log_buffer.uninstall()

        await _pump_once()

        assert not sent
