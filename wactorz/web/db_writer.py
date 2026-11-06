# SPDX-License-Identifier: Apache-2.0
"""What a broker message or a chat turn writes, written in order on one background task.

A write inside the dispatch of a message holds the event loop, and with it every
actor in the process and the broker's keepalive, for as long as the disk takes.
So the writers here hand their rows over and return at once; one task writes
them on a thread, in the order they were handed over.

Two lanes, drained by that one task:

* **Chat rows** (`chat`), bounded by a count and by bytes. The page has already
  shown a turn by the time it is queued, so when the lane is full a row is
  dropped, counted on `/metrics`, and said once in a while in the log, rather
  than held without limit on a small machine. A batch goes in one transaction.
* **Key-value writes** (`kv_set`, `kv_delete`), never dropped and never counted
  against the chat lane, so a delete is always accepted. A set replaces a set
  of the same key still waiting behind nothing else, since only the latest
  value matters; a delete is a barrier, so a set queued after a reset is never
  merged into one queued before it.

Chat rows keep one order among themselves and key-value writes keep theirs; the
two lanes write different tables, so nothing orders one against the other.

`settled` waits until everything queued before it is written, for a caller that
reads the database next. `close` drains for a while at shutdown, then waits for
the write already running on its thread, so the database is not closed under it.
"""

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.persistence.db import ChatRow
from ..monitoring import chat_metrics
from . import runtime

logger = logging.getLogger(__name__)

#: Chat rows waiting at most, and their bytes at most, before new ones are dropped.
MAX_ROWS = 2000
MAX_BYTES = 4 * 1024 * 1024
#: Seconds between warnings about dropped rows, however many are dropped meanwhile.
WARN_EVERY_S = 60.0
#: Seconds shutdown waits for what is queued, before giving the rest up.
DRAIN_TIMEOUT_S = 10.0
#: Seconds between looks at the queue while waiting for it to empty.
_POLL_S = 0.005


@dataclass
class _KvWrite:
    seq: int
    agent: str
    key: str
    #: A copy taken when queued; None deletes the key.
    value: Any = None
    delete: bool = False


@dataclass
class _Queued:
    seq: int
    row: ChatRow
    size: int


@dataclass
class _Busy:
    """The write running on a thread, and the newest item it carries."""

    future: "asyncio.Future[None]"
    seqs: list[int] = field(default_factory=list)


def _size_of(row: ChatRow) -> int:
    return len(row.content) + sum(len(str(a)) for a in row.attachments or ())


def _write_rows(rows: list[ChatRow]) -> None:
    """Write ``rows`` in one transaction, or one by one if that fails. Run on a thread."""
    db = runtime.db
    if db is None:
        return
    try:
        db.write_chat_logs(rows)
    except Exception:
        # One row the database refuses must not cost the rest of the batch.
        for row in rows:
            try:
                db.write_chat_logs([row])
            except Exception as exc:
                logger.warning("[db-writer] a chat row was not kept: %s", exc)


def _write_kv(write: _KvWrite) -> None:
    """Apply one key-value write. Run on a thread."""
    db = runtime.db
    if db is None:
        return
    if write.delete:
        db.kv_delete(write.agent, write.key)
    else:
        db.kv_set(write.agent, write.key, write.value)


class DbWriter:
    """See the module's description. One instance serves the running server."""

    def __init__(self, *, max_rows: int = MAX_ROWS, max_bytes: int = MAX_BYTES) -> None:
        self._max_rows = max_rows
        self._max_bytes = max_bytes
        self._rows: deque[_Queued] = deque()
        self._bytes = 0
        self._kv: list[_KvWrite] = []
        self._seq = 0
        self._busy: _Busy | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._closing = False
        self._dropped_since_warning = 0
        #: When the last warning was said; None before the first. Not 0: the clock
        #: counts from the machine's boot, so on one booted under a minute ago a
        #: start at 0 would hold back the first warning too.
        self._warned_at: float | None = None

    # ── Handing work over (on the event loop) ──────────────────────────────

    def chat(self, row: ChatRow) -> None:
        """Queue a chat turn, or drop it when the chat lane is full."""
        size = _size_of(row)
        if (
            self._closing
            or len(self._rows) >= self._max_rows
            or self._bytes + size > self._max_bytes
        ):
            self._dropped(row)
            return
        self._seq += 1
        self._rows.append(_Queued(self._seq, row, size))
        self._bytes += size
        self._start()

    def kv_set(self, agent: str, key: str, value: Mapping[str, Any] | Any) -> None:
        """Queue setting ``key``; ``value`` is copied now, so later changes to it are not written."""
        copied = dict(value) if isinstance(value, Mapping) else value
        self._seq += 1
        last = self._kv[-1] if self._kv else None
        if last is not None and not last.delete and (last.agent, last.key) == (agent, key):
            # Only the newest value of a key matters, and nothing waits between
            # the two: a delete in between would be the last entry instead. The
            # write keeps its number, so a caller already waiting for it still does.
            last.value = copied
        else:
            self._kv.append(_KvWrite(self._seq, agent, key, copied))
        self._start()

    def kv_delete(self, agent: str, key: str) -> None:
        """Queue deleting ``key``, after every write of it already queued."""
        self._seq += 1
        self._kv.append(_KvWrite(self._seq, agent, key, delete=True))
        self._start()

    # ── Waiting ────────────────────────────────────────────────────────────

    async def settled(self) -> None:
        """Wait until everything queued before this call is written."""
        target = self._seq
        while not self._done_through(target):
            if self._task is None or self._task.done():
                self._start()
            await asyncio.sleep(_POLL_S)

    async def close(self, timeout: float = DRAIN_TIMEOUT_S) -> None:
        """Stop taking chat rows, write what is queued for up to ``timeout``, then stop.

        What is still queued after it is given up, and said. The write already
        running on its thread is waited for either way: the database closes
        after this returns, and must not close under it.
        """
        self._closing = True
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.settled(), timeout)
            left_rows, left_kv = len(self._rows), len(self._kv)
            if left_rows or left_kv:
                logger.warning(
                    "[db-writer] stopping with %s chat row(s) and %s other write(s) not kept",
                    left_rows,
                    left_kv,
                )
            self._rows.clear()
            self._kv.clear()
            self._bytes = 0
            busy = self._busy
            if busy is not None:
                await asyncio.shield(busy.future)
        finally:
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._closing = False

    # ── The task ───────────────────────────────────────────────────────────

    def _start(self) -> None:
        """Make sure a task on the running loop is writing, and tell it there is work."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = self._task
        if task is None or task.done() or task.get_loop() is not loop:
            self._wake = asyncio.Event()
            self._task = loop.create_task(self._run(self._wake), name="db-writer")
        if self._wake is not None:
            self._wake.set()

    async def _run(self, wake: asyncio.Event) -> None:
        while True:
            if not self._rows and not self._kv:
                wake.clear()
                await wake.wait()
                continue
            try:
                await self._write_next()
            except Exception:
                # Whatever failed around a write, the task carries on with the next.
                logger.exception("[db-writer] a write failed")

    async def _write_next(self) -> None:
        if self._rows:
            taken = list(self._rows)
            self._rows.clear()
            self._bytes = 0
            await self._on_thread(_write_rows, [q.row for q in taken], [q.seq for q in taken])
        if self._kv:
            write = self._kv.pop(0)
            try:
                await self._on_thread(_write_kv, write, [write.seq])
            except Exception as exc:
                logger.warning("[db-writer] %s/%s was not written: %s", write.agent, write.key, exc)

    async def _on_thread(self, call: Any, argument: Any, seqs: list[int]) -> None:
        future = asyncio.ensure_future(asyncio.to_thread(call, argument))
        self._busy = _Busy(future, seqs)
        try:
            await future
        finally:
            self._busy = None

    def _done_through(self, target: int) -> bool:
        waiting = [q.seq for q in self._rows] + [w.seq for w in self._kv]
        if self._busy is not None:
            waiting.extend(self._busy.seqs)
        return not waiting or min(waiting) > target

    def _dropped(self, row: ChatRow) -> None:
        chat_metrics.ROWS_DROPPED.inc()
        self._dropped_since_warning += 1
        now = time.monotonic()
        if self._warned_at is None or now - self._warned_at >= WARN_EVERY_S:
            logger.warning(
                "[db-writer] the chat log is falling behind: %s turn(s) not kept "
                "(the page showed them), latest from %r",
                self._dropped_since_warning,
                row.agent_name,
            )
            self._warned_at = now
            self._dropped_since_warning = 0


#: The running server's writer.
writer = DbWriter()
