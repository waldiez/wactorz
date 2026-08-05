"""The in-memory log buffer: bounded, redacted, and inert toward other handlers.

Three regressions these pin:

* an unbounded buffer is a memory leak an unauthenticated publisher can drive —
  the same defect already recorded against ``state["agents"]``;
* a handler that mutates shared records makes what the *file* contains depend on
  handler registration order;
* a handler that raises or logs from inside ``emit`` breaks or recurses the one
  subsystem that has to keep working while everything else fails.
"""

import logging
import sys
from collections.abc import Generator
from typing import Any

import pytest

from wactorz.monitoring.log_buffer import (
    LogRingBuffer,
    get_buffer,
    install,
    uninstall,
)
from wactorz.monitoring.log_redaction import REDACTED


@pytest.fixture(name="buf")
def buf_fixture() -> LogRingBuffer:
    """A detached buffer — not attached to the root logger."""
    return LogRingBuffer(capacity=5)


def record(
    msg: str,
    *args: object,
    level: int = logging.INFO,
    name: str = "wactorz.test",
    exc_info: object = None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args or None,
        exc_info=exc_info,  # type: ignore[arg-type]
    )


class TestBounded:
    def test_evicts_oldest_at_capacity(self, buf: LogRingBuffer) -> None:
        for i in range(8):
            buf.emit(record(f"line {i}"))
        texts = [e["text"] for e in buf.snapshot()]
        assert texts == ["line 3", "line 4", "line 5", "line 6", "line 7"]

    def test_flood_does_not_grow(self, buf: LogRingBuffer) -> None:
        for i in range(10_000):
            buf.emit(record("line %d", i))
        assert len(buf.snapshot()) == buf.capacity == 5

    def test_capacity_reported(self) -> None:
        assert LogRingBuffer(capacity=42).capacity == 42


class TestSnapshot:
    def test_oldest_first(self, buf: LogRingBuffer) -> None:
        for i in range(3):
            buf.emit(record(f"line {i}"))
        assert [e["text"] for e in buf.snapshot()] == ["line 0", "line 1", "line 2"]

    def test_limit_returns_most_recent(self, buf: LogRingBuffer) -> None:
        for i in range(5):
            buf.emit(record(f"line {i}"))
        assert [e["text"] for e in buf.snapshot(limit=2)] == ["line 3", "line 4"]

    def test_limit_zero(self, buf: LogRingBuffer) -> None:
        buf.emit(record("line"))
        assert buf.snapshot(limit=0) == []

    def test_limit_larger_than_buffer(self, buf: LogRingBuffer) -> None:
        buf.emit(record("line"))
        assert len(buf.snapshot(limit=999)) == 1

    def test_snapshot_is_a_copy(self, buf: LogRingBuffer) -> None:
        buf.emit(record("line"))
        buf.snapshot().clear()
        assert len(buf.snapshot()) == 1


class TestEnvelope:
    def test_shape(self, buf: LogRingBuffer) -> None:
        buf.emit(record("hello", level=logging.WARNING, name="wactorz.agents"))
        entry = buf.snapshot()[0]
        assert entry["source"] == "app"
        assert entry["level"] == "WARNING"
        assert entry["origin"] == "wactorz.agents"
        assert entry["text"] == "hello"
        assert isinstance(entry["ts"], float)

    def test_args_are_merged(self, buf: LogRingBuffer) -> None:
        buf.emit(record("actor %s on %s", "temp-monitor", "rpi"))
        assert buf.snapshot()[0]["text"] == "actor temp-monitor on rpi"

    def test_traceback_is_kept(self, buf: LogRingBuffer) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            buf.emit(record("failed", exc_info=sys.exc_info()))
        text = buf.snapshot()[0]["text"]
        assert "failed" in text
        assert "ValueError: boom" in text
        assert "Traceback" in text


class TestRedaction:
    def test_secret_in_message(self, buf: LogRingBuffer) -> None:
        buf.emit(record("connecting with password=hunter2"))
        text = buf.snapshot()[0]["text"]
        assert "hunter2" not in text
        assert REDACTED in text

    def test_secret_in_args(self, buf: LogRingBuffer) -> None:
        buf.emit(record("broker %s", "mqtt://u:s3cr3t@host"))
        assert "s3cr3t" not in buf.snapshot()[0]["text"]

    def test_secret_in_exception_message(self, buf: LogRingBuffer) -> None:
        """Frames carry no values, but an exception's own message can."""
        try:
            raise RuntimeError("auth failed for token=sk-live-9f3a")
        except RuntimeError:
            buf.emit(record("upstream error", exc_info=sys.exc_info()))
        assert "sk-live-9f3a" not in buf.snapshot()[0]["text"]


class TestLeavesRecordsAlone:
    """What the file handler writes must not depend on handler ordering."""

    def test_record_not_mutated(self, buf: LogRingBuffer) -> None:
        rec = record("password=hunter2")
        buf.emit(rec)
        assert rec.getMessage() == "password=hunter2"

    def test_args_not_cleared(self, buf: LogRingBuffer) -> None:
        rec = record("connecting with %s", "password=hunter2")
        buf.emit(rec)
        assert rec.args == ("password=hunter2",)


class TestNeverRaises:
    def test_malformed_format_string(self, buf: LogRingBuffer) -> None:
        buf.emit(record("%d items", "not-a-number"))
        entry = buf.snapshot()[0]
        assert "unformattable" in entry["text"]
        assert entry["origin"] == "wactorz.test", "level and origin are still true"

    def test_non_string_msg(self, buf: LogRingBuffer) -> None:
        buf.emit(record(12345))  # type: ignore[arg-type]
        assert buf.snapshot()[0]["text"] == "12345"

    def test_emit_does_not_log(self, buf: LogRingBuffer, caplog) -> None:
        """A handler that logs from inside emit recurses."""
        with caplog.at_level(logging.DEBUG):
            buf.emit(record("%d items", "not-a-number"))
        assert caplog.records == []


class TestInstall:
    @pytest.fixture(autouse=True)
    def _cleanup(self) -> Generator[None, Any, None]:
        yield
        uninstall()

    def test_attaches_to_root(self) -> None:
        buffer = install()
        assert buffer in logging.getLogger().handlers

    def test_idempotent(self) -> None:
        """A repeated startup path must not record every line twice."""
        first = install()
        second = install()
        assert first is second
        assert logging.getLogger().handlers.count(first) == 1

    def test_get_buffer_before_install(self) -> None:
        uninstall()
        assert get_buffer() is None

    def test_get_buffer_after_install(self) -> None:
        buffer = install()
        assert get_buffer() is buffer

    def test_captures_real_logging(self) -> None:
        buffer = install()
        logging.getLogger("wactorz.test.install").warning("token=sk-live-9f3a")
        entries = [e for e in buffer.snapshot() if e["origin"] == "wactorz.test.install"]
        assert len(entries) == 1
        assert "sk-live-9f3a" not in entries[0]["text"]
        assert entries[0]["level"] == "WARNING"

    def test_uninstall_detaches(self) -> None:
        buffer = install()
        uninstall()
        assert buffer not in logging.getLogger().handlers
        assert get_buffer() is None
