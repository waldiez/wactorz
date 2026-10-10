"""Waiting for something to happen, rather than for an amount of time.

A test that sleeps for as long as the thing under test usually takes fails on
the day it takes longer: a loaded runner, a slow disk. These wait for the
condition itself, for far longer than it should ever need, and say what never
happened when it does not.
"""

import asyncio
from collections.abc import Callable

import pytest

from wactorz.core.deferred_write import DeferredWriter

#: How long to wait for something that should take a moment. Generous on
#: purpose: it is only ever reached when the test is about to fail anyway.
PATIENCE_S = 20.0


async def until(condition: Callable[[], object], what: str, timeout: float = PATIENCE_S) -> None:
    """Wait for ``condition`` to hold, and fail naming ``what`` if it never does."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"within {timeout:g}s, never: {what}")
        await asyncio.sleep(0.01)


async def quiet(writer: DeferredWriter) -> None:
    """Wait until ``writer`` has written everything asked of it and has nothing scheduled.

    Past its delay as well as its writes: a test that checks a file was *not*
    written needs the moment it would have been written in to have gone by.
    """
    await until(
        lambda: writer.idle and writer._timer is None, "the writer finishing what it was asked"
    )
