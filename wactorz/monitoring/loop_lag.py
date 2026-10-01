"""Noticing when the event loop stops running, and saying where it is.

Every actor in a process shares one event loop. Code that blocks it -- a
synchronous call inside a handler, generated agent code that sleeps or computes
without yielding -- stops all of them at once: heartbeats, the supervisor, the
broker's keepalive. Nothing on the loop can report that, because nothing on the
loop is running.

So the watching is done from a thread. It asks the loop to run a callback and
times how long the loop takes to get to it. That delay is the lag, recorded for
``/metrics``; and when the loop has not answered for a while, the thread logs
the stack the loop's own thread is in, which is the line of code holding it.
"""

import asyncio
import logging
import sys
import threading
import time
import traceback

from prometheus_client import Histogram

logger = logging.getLogger(__name__)

#: How often the loop is asked to answer.
CHECK_INTERVAL_S = 1.0

#: How long the loop may go without answering before the log says where it is.
#: Long enough that a burst of ordinary work does not count, short enough to
#: catch a block before the broker's keepalive gives up on the connection.
REPORT_AFTER_S = 5.0

#: Upper bounds in seconds, from a healthy loop to one blocked for a minute.
_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)

LAG = Histogram(
    "wactorz_event_loop_lag_seconds",
    "How long the event loop took to run a callback it was asked to run at once.",
    buckets=_BUCKETS,
    registry=None,
)

#: What a registry serving `/metrics` registers.
COLLECTORS = (LAG,)


class LoopLagMonitor:
    """Times the event loop's answers from a thread of its own."""

    def __init__(
        self, interval: float = CHECK_INTERVAL_S, report_after: float = REPORT_AFTER_S
    ) -> None:
        self._interval = interval
        self._report_after = report_after
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._answered = threading.Event()
        #: The lag last measured, for a test or a log line; `/metrics` has them all.
        self.last = 0.0

    def start(self) -> None:
        """Watch the running loop. Call from the loop's own thread; a second call does nothing."""
        if self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._stopping.clear()
        self._thread = threading.Thread(target=self._watch, name="loop-lag", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop watching. Safe to call when not started."""
        self._stopping.set()
        self._answered.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self._interval + self._report_after)

    def _answer(self, asked_at: float) -> None:
        """On the loop: record how long it took to get here."""
        self.last = time.monotonic() - asked_at
        LAG.observe(self.last)
        self._answered.set()

    def _watch(self) -> None:
        loop = self._loop
        if loop is None:
            return
        # One question at a time: the next is not asked until this one has been
        # answered, however long that takes. So an answer always belongs to the
        # question being waited on, and never arrives during a later one.
        while not self._stopping.wait(self._interval):
            self._answered.clear()
            asked_at = time.monotonic()
            try:
                loop.call_soon_threadsafe(self._answer, asked_at)
            except RuntimeError:
                # The loop has closed; there is nothing left to watch.
                return
            if self._answered.wait(self._report_after):
                continue
            self._report(time.monotonic() - asked_at)
            # Said once per block. The answer, when it comes, records how long
            # the whole of it lasted.
            self._answered.wait()
            if not self._stopping.is_set():
                logger.warning("[loop] The event loop is running again after %.1fs.", self.last)

    def _report(self, waited: float) -> None:
        # The interpreter's own table of where each thread is: the one way to
        # read the stack of a thread that is not running this code.
        frame = sys._current_frames().get(self._loop_thread or 0)
        where = "".join(traceback.format_stack(frame)) if frame is not None else "(unknown)\n"
        logger.warning(
            "[loop] The event loop has not run for %.1fs: every agent in this process is "
            "waiting, and so is the broker connection. It is here:\n%s",
            waited,
            where.rstrip(),
        )
