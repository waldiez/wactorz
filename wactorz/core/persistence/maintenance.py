"""Housekeeping the storage owes itself, on a timer and off the event loop.

Nothing on the server ran periodic work before this, which is why the WAL
checkpoint was left to SQLite — and SQLite does it inline, on whichever write
crosses its page threshold. That write is on the event loop, and on a Raspberry
Pi's SD card the fold costs ~69ms with every actor in the process stopped for it.

So the shape matters more than the schedule: each job runs through
``asyncio.to_thread``, and the jobs themselves are ordinary synchronous methods.
Nothing here may block the loop, because avoiding exactly that is the reason it
exists.

A job is registered rather than hard-coded so the next one — the retention
pruner, which already exists and nothing calls — is a line rather than a
redesign.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .stores import get_db

logger = logging.getLogger(__name__)

#: How often the jobs run. Long, because none of this is urgent: the point is
#: that it happens away from the loop and while the system is usually quiet, not
#: that it happens promptly.
INTERVAL_S = 300.0

#: name -> the work. Synchronous by design; the loop is what puts it on a thread.
_JOBS: dict[str, Callable[[], object]] = {}

#: How long :func:`stop` waits for a job already on a thread. Generous: the jobs
#: are bounded (a checkpoint, a prune), and the alternative to waiting is letting
#: shutdown race a thread that holds the connection lock.
STOP_TIMEOUT_S = 30.0

_task: asyncio.Task | None = None
_stopping: asyncio.Event | None = None


def register(name: str, job: Callable[[], object]) -> None:
    """Add a job to the rotation, replacing any registered under the same name."""
    _JOBS[name] = job


def _checkpoint_db() -> object:
    """Fold the main database's WAL back in, if there is a database yet."""
    db = get_db()
    if db is None:
        return None
    return db.checkpoint()


async def _run_once() -> None:
    """Run every job on a worker thread, one at a time.

    Sequential rather than gathered: these contend for the same connection lock,
    so running them together would only move the waiting into the threads. A job
    that raises is logged and the rest still run — housekeeping failing is not a
    reason to stop housekeeping.
    """
    for name, job in list(_JOBS.items()):
        started = time.perf_counter()
        try:
            result = await asyncio.to_thread(job)
        except Exception:
            logger.exception("[maintenance] %s failed", name)
            continue
        elapsed = (time.perf_counter() - started) * 1000
        # Logged at info with the duration because an unhealthy checkpointer is
        # otherwise invisible until the disk complains.
        logger.info("[maintenance] %s took %.0fms (%s)", name, elapsed, result)


async def _loop(stopping: asyncio.Event) -> None:
    """Wait out the interval, work, repeat — until asked to stop.

    The wait is on the stop signal rather than a plain sleep, so shutdown does
    not have to cancel this task. That matters more than it looks: a job runs
    through ``asyncio.to_thread``, and cancelling *that* await abandons the
    coroutine while the thread carries on. The job would still be holding the
    connection lock when shutdown reached for it to write actor state out —
    which is the one thing stopping the rotation is supposed to prevent.

    ``asyncio.wait``, not ``wait_for``, for the reason given in
    ``retry._bounded``: on Python 3.10 a ``wait_for`` whose guarded future
    completes in the same instant the caller is cancelled returns from inside
    its own ``CancelledError`` handler, and the caller never learns it was
    cancelled. Here that would mean starting a job *after* being cancelled —
    putting a thread on the connection lock during shutdown, which is the whole
    thing this ordering exists to avoid.
    """
    while not stopping.is_set():
        waiter = asyncio.ensure_future(stopping.wait())
        try:
            done, _pending = await asyncio.wait({waiter}, timeout=INTERVAL_S)
        except BaseException:
            # Cancelled while waiting: the waiter is ours, and asyncio.wait
            # leaves it running.
            waiter.cancel()
            raise
        waiter.cancel()
        if done:
            return
        await _run_once()


def start() -> None:
    """Begin the rotation. Idempotent; needs a running event loop."""
    global _task, _stopping
    if _task is not None and not _task.done():
        return
    register("wal-checkpoint", _checkpoint_db)
    _stopping = asyncio.Event()
    _task = asyncio.create_task(_loop(_stopping))
    logger.info("[maintenance] every %.0fs: %s", INTERVAL_S, ", ".join(_JOBS))


async def stop() -> None:
    """Ask the rotation to stop, and wait for any job in flight. Idempotent."""
    global _task, _stopping
    if _task is None:
        return
    if _stopping is not None:
        _stopping.set()
    done, _pending = await asyncio.wait({_task}, timeout=STOP_TIMEOUT_S)
    if not done:
        # The thread cannot be cancelled, so saying so is all that is left.
        # Whoever wants the lock next will wait for it rather than race.
        logger.warning("[maintenance] a job is still running after %.0fs", STOP_TIMEOUT_S)
    else:
        outcome = _task.exception()
        if outcome is not None:
            logger.warning("[maintenance] ended in error: %s", outcome)
    _task = None
    _stopping = None
