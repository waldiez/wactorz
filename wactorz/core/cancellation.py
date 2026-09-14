"""Stopping a task that may not notice it was asked to."""

import asyncio
from collections.abc import Iterable
from typing import Any

#: How long a cancelled task gets to unwind before it is asked again. Long enough
#: for ordinary cleanup, such as closing a connection, to finish undisturbed,
#: since a repeated request lands in the middle of it; short next to a shutdown's
#: budget, because a task whose request was lost waits out the whole interval.
RECANCEL_AFTER_S = 0.25


async def cancel_until_done(
    task: asyncio.Task[object], *, timeout: float, recancel_after: float | None = None
) -> bool:
    """Cancel `task`, and cancel it again for as long as it keeps running.

    On Python 3.10 and 3.11 a cancellation can be lost: ``asyncio.wait_for``
    returns its result from inside its own ``CancelledError`` handler when the
    future it guards completes in the same instant, and the task carries on as
    though nothing had asked it to stop. A single ``cancel()`` followed by an
    unbounded wait then waits for ever. Asking again after a pause gets through,
    because the second request does not arrive in that same instant.

    Waits with ``asyncio.wait``, never ``wait_for``, for the same reason, and lets
    ``CancelledError`` through: a caller cancelled while waiting here still
    learns that it was.

    Returns whether the task finished within ``timeout``. One that did not is left
    cancelled but running, and the caller decides what to say about it.
    """
    interval = RECANCEL_AFTER_S if recancel_after is None else recancel_after
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        task.cancel()
        await asyncio.wait({task}, timeout=min(interval, remaining))
    if not task.cancelled():
        task.exception()  # retrieved, so the loop does not warn about it at exit
    return True


async def cancel_all_until_done(
    tasks: Iterable[asyncio.Task[Any]], *, timeout: float
) -> list[asyncio.Task[Any]]:
    """Stop `tasks` together with :func:`cancel_until_done`; return any still running.

    Each gets the whole of ``timeout``, side by side rather than one after another.
    A task that had already finished is left alone, but its exception is retrieved,
    so the loop does not warn about it at exit.
    """
    running: list[asyncio.Task[Any]] = []
    for task in tasks:
        if not task.done():
            running.append(task)
        elif not task.cancelled():
            task.exception()
    if not running:
        return []
    stopped = await asyncio.gather(*(cancel_until_done(task, timeout=timeout) for task in running))
    return [task for task, done in zip(running, stopped, strict=True) if not done]
