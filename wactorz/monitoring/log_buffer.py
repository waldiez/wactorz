"""A bounded in-memory ring of recent log records.

Holds what the dashboard's log view will read, in the envelope the view renders,
so agent activity and application logs share one shape::

    {"source": "app", "ts": float, "level": str, "origin": str, "text": str}

Two properties to keep:

* **Nothing here logs.** A handler that logs from inside its own ``emit``
  recurses; failures go to ``handleError``, which writes to stderr.
* **The buffer does not mutate records.** It redacts a *copy*, so what the file
  and stream handlers write does not depend on which handler the root logger
  happens to call first. Mutating shared records would make output
  order-dependent, which is the kind of bug nobody reproduces.
"""

import logging
from collections import deque
from typing import Any

from .log_redaction import EXC_FORMATTER, redact, redacted_message

DEFAULT_CAPACITY = 1000


class LogRingBuffer(logging.Handler):
    """Keep the most recent records in memory, oldest evicted first.

    Bounded by construction: a ``deque`` with ``maxlen`` cannot grow past its
    capacity, so no flood of records — from an agent, a library, or a retry loop
    — can consume memory without limit.

    Entries are the envelope the dashboard renders, so the view has one shape to
    handle across both sources::

        {"source": "app", "ts": float, "level": str, "origin": str, "text": str}
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        super().__init__()
        self._entries: deque[dict[str, Any]] = deque(maxlen=capacity)
        #: Records ever appended, not records held. A reader remembers a
        #: position in this count and asks what has arrived since, which the
        #: deque itself cannot answer — it forgets what it evicts.
        self._total = 0

    @property
    def capacity(self) -> int:
        return self._entries.maxlen or 0

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer one record. Never raises, never logs."""
        try:
            self._entries.append(self._entry(record))
            # After the append, so a record that could not be built does not
            # advance the count and silently become "missed" for every reader.
            self._total += 1
        except Exception:
            # handleError writes to stderr (and honours logging.raiseExceptions)
            # rather than logging, which is what keeps this from recursing.
            self.handleError(record)

    def _entry(self, record: logging.LogRecord) -> dict[str, Any]:
        text = redacted_message(record)
        if text is None:
            # Keep the record rather than dropping it: that a line could not be
            # formatted is itself worth seeing, and the level and origin are
            # still true.
            text = f"<unformattable log record from {record.name}>"
        if record.exc_info:
            # Redacted too — an exception's *message* can carry a credential
            # even though the frames themselves do not.
            text = f"{text}\n{redact(EXC_FORMATTER.formatException(record.exc_info))}"
        return {
            "source": "app",
            "ts": record.created,
            "level": record.levelname,
            "origin": record.name,
            "text": text,
        }

    def snapshot(self, limit: int | None = None) -> list[dict[str, Any]]:
        """The buffered entries, oldest first; the most recent ``limit`` of them."""
        entries = list(self._entries)
        if limit is not None and limit >= 0:
            entries = entries[-limit:] if limit else []
        return entries

    @property
    def total(self) -> int:
        """Records ever appended. A reader's starting position."""
        return self._total

    def since(self, position: int) -> tuple[list[dict[str, Any]], int]:
        """Entries appended after `position`, and the position to remember next.

        Returns what is *still held*, which is not always what was missed: a
        reader that falls further behind than the buffer is deep gets the oldest
        surviving records rather than the ones evicted while it was away. The
        alternative — pretending nothing was lost — is worse for a log.
        """
        missed = self._total - position
        if missed <= 0:
            return [], self._total
        # Copied without a lock. `emit` can append from any thread while this
        # iterates, which CPython tolerates for a deque in practice and which
        # would at worst raise here — a caller that skips one tick, not one that
        # loses records: the position is only advanced on the way out. A lock on
        # the logging path would cost every record to protect a reader that can
        # simply try again.
        entries = list(self._entries)
        return entries[-missed:] if missed < len(entries) else entries, self._total


_buffer: LogRingBuffer | None = None


def install(capacity: int = DEFAULT_CAPACITY) -> LogRingBuffer:
    """Attach the buffer to the root logger, once.

    Idempotent: a second call returns the buffer already attached rather than
    stacking another handler, so a reload or a repeated startup path cannot end
    up recording every line twice.
    """
    global _buffer
    if _buffer is None:
        _buffer = LogRingBuffer(capacity)
        logging.getLogger().addHandler(_buffer)
    return _buffer


def get_buffer() -> LogRingBuffer | None:
    """The installed buffer, or ``None`` before ``install()`` has run."""
    return _buffer


def uninstall() -> None:
    """Detach and forget the buffer. For tests; the process never needs it."""
    global _buffer
    if _buffer is not None:
        logging.getLogger().removeHandler(_buffer)
        _buffer = None
