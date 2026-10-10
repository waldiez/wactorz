"""Writing a file a moment later, from a worker thread.

Replacing a file safely means writing a temporary, forcing it to the disk and
renaming it, and the forcing is what costs: on an SD card it takes tens of
milliseconds however little was written. Done on the event loop, every actor in
the process waits for it, and an agent that saves a value on each tick makes
them wait on each tick.

So a write asked for here happens shortly after, on a thread, and what is
written is whatever the file should hold by then: several changes inside the
delay become one write. The price is the delay itself. A process that is killed
loses what was asked for in the last moment before it; one that shuts down
calls :meth:`DeferredWriter.flush` and loses nothing.

With no event loop running there is nothing to keep waiting, and nothing to
write it later, so the write happens at once.
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from .atomic_io import write_bytes

logger = logging.getLogger(__name__)

#: How long a change waits to be written, so that the ones made just after it
#: go out in the same write. Also the most that a killed process can lose.
WRITE_DELAY_S = 1.0

#: When to say an agent's state has grown expensive to write. Every save
#: rewrites the whole state, and encoding it happens on the event loop. Chosen
#: from measurement on a Raspberry Pi 5 SD card, where a save crosses ~15ms
#: around here and climbs steeply after it.
LARGE_STATE_BYTES = 512 * 1024

#: What a path should hold, asked for when the write is about to happen. None
#: when it turns out to hold that already, and there is nothing to write.
Content = Callable[[], bytes | None]

#: Told the bytes a path now holds, once they are on the disk. Called from the
#: thread that wrote them.
Written = Callable[[bytes], None]


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class DeferredWriter:
    """Writes files shortly after they are asked for, off the event loop.

    A path is given with a function that returns its content. The function is
    called when the write is about to happen, on the event loop, so it sees the
    latest state and cannot run while the loop is changing that state; the
    bytes it returns are then written by a thread, and None means the file is
    already right. Asking again for a path before it is written replaces the
    earlier request.

    Safe to ask from any thread. Writes to one path never overtake each other:
    each is numbered, and one that arrives after a later one, or after the
    path was withdrawn, is dropped.
    """

    def __init__(self, delay: float = WRITE_DELAY_S) -> None:
        self._delay = delay
        #: Guards the fields below, and is never held across a write.
        self._lock = threading.Lock()
        #: Held for the length of one write, so two never interleave and a
        #: withdrawal waits for the write it would otherwise race.
        self._io_lock = threading.Lock()
        self._pending: dict[Path, tuple[Content, Written | None]] = {}
        self._sequence = 0
        #: The number of the last write, or withdrawal, each path received,
        #: kept only while a batch taken earlier could still arrive after it.
        self._settled: dict[Path, int] = {}
        #: The lowest number in each batch that has been taken and not written.
        self._in_hand: set[int] = set()
        #: The loop whose timer and thread pool carry the writes.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._in_flight: asyncio.Future[None] | None = None

    def submit(self, path: Path, content: Content, written: Written | None = None) -> None:
        """Have ``path`` hold what ``content()`` returns, shortly.

        ``written`` is called with those bytes once they are on the disk, and
        not at all for a write that failed or was overtaken: whoever keeps
        track of what a file holds learns it from the write, not the request.
        """
        with self._lock:
            self._pending[path] = (content, written)
        loop = _running_loop()
        if loop is not None:
            self._adopt(loop)
            self._arm()
            return
        carrier = self._loop
        if carrier is not None and carrier.is_running():
            # Asked from a thread while the loop runs elsewhere: the loop does
            # the scheduling, since its timers are not this thread's to touch.
            try:
                carrier.call_soon_threadsafe(self._arm)
            except RuntimeError:
                # The loop closed between the check and the call.
                self.flush()
            return
        self.flush()

    def discard(self, path: Path) -> None:
        """Withdraw ``path``: nothing asked for before now will be written to it.

        Returns once any write to it that had already started has finished, so
        the caller can remove the file without it coming back.
        """
        with self._io_lock, self._lock:
            self._pending.pop(path, None)
            self._sequence += 1
            self._settled[path] = self._sequence

    def flush(self) -> None:
        """Write everything that is waiting, now, on the calling thread."""
        self._write(self._take())

    @property
    def idle(self) -> bool:
        """Whether nothing is waiting to be written and nothing is being written.

        For something that needs to know the files are as they were asked to
        be, without forcing them out itself: once this is true they are.
        """
        with self._lock:
            waiting = bool(self._pending)
        return not waiting and self._in_flight is None

    # ── On the event loop ─────────────────────────────────────────────────────

    def _adopt(self, loop: asyncio.AbstractEventLoop) -> None:
        """Carry the writes on ``loop``, forgetting what belonged to an earlier one."""
        if loop is not self._loop:
            self._loop = loop
            self._timer = None
            self._in_flight = None

    def _arm(self) -> None:
        """Start the delay, unless it is running or a write is still on its way out."""
        if self._loop is None or self._timer is not None or self._in_flight is not None:
            return
        self._timer = self._loop.call_later(self._delay, self._start_write)

    def _start_write(self) -> None:
        self._timer = None
        if self._loop is None:
            return
        batch = self._take()
        if not batch:
            return
        self._in_flight = self._loop.run_in_executor(None, self._write, batch)
        self._in_flight.add_done_callback(self._finished)

    def _finished(self, done: "asyncio.Future[None]") -> None:
        if done is not self._in_flight:
            return
        self._in_flight = None
        if not done.cancelled() and done.exception() is not None:
            logger.error("[deferred-write] A write failed", exc_info=done.exception())
        # One write at a time: whatever was asked for meanwhile goes out next.
        if self._pending:
            self._arm()

    # ── The write itself ──────────────────────────────────────────────────────

    def _take(self) -> list[tuple[Path, int, bytes, Written | None]]:
        """Everything waiting, with its content and the number that orders it."""
        with self._lock:
            waiting = list(self._pending.items())
            self._pending.clear()
            first = self._sequence + 1
            self._sequence += len(waiting)
        batch = []
        for number, (path, (content, written)) in enumerate(waiting, start=first):
            try:
                data = content()
            except Exception as exc:
                logger.warning("[deferred-write] %s was not written: %s", path, exc)
                continue
            if data is not None:
                batch.append((path, number, data, written))
        if batch:
            with self._lock:
                self._in_hand.add(batch[0][1])
        return batch

    def _write(self, batch: list[tuple[Path, int, bytes, Written | None]]) -> None:
        try:
            self._write_each(batch)
        finally:
            if batch:
                self._forget_what_is_settled(batch[0][1])

    def _forget_what_is_settled(self, first: int) -> None:
        """Drop the numbers no batch still in hand could be compared with.

        A path's number only matters to a write taken before it. Once every
        batch in hand starts later, nothing can arrive that it would turn away,
        and keeping it would keep one entry for every file ever written or
        withdrawn -- one for each agent ever deleted.
        """
        with self._lock:
            self._in_hand.discard(first)
            floor = min(self._in_hand, default=self._sequence + 1)
            for path in [path for path, number in self._settled.items() if number < floor]:
                del self._settled[path]

    def _write_each(self, batch: list[tuple[Path, int, bytes, Written | None]]) -> None:
        for path, number, data, written in batch:
            with self._io_lock:
                with self._lock:
                    if self._settled.get(path, 0) > number:
                        continue
                try:
                    # Its directory is made here, off the event loop, the first
                    # time anything is written into it.
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write_bytes(path, data)
                except OSError as exc:
                    logger.warning("[deferred-write] %s was not written: %s", path, exc)
                    continue
                with self._lock:
                    self._settled[path] = number
                if written is not None:
                    written(data)
