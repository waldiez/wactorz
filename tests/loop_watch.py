# SPDX-License-Identifier: Apache-2.0
"""Whether the event loop could run while a blocking call was in progress.

A test wraps the call so it clears a flag and waits for a coroutine on the loop
to set it again. Made on a thread, that happens at once; made on the loop's own
thread, the coroutine never gets to run and the wait runs out, so the answer
does not depend on how fast or loaded the machine is.
"""

import asyncio
import threading


class LoopWatch:
    """See the module's description. Start `tick` as a task while the call is made."""

    def __init__(self) -> None:
        self._ran = threading.Event()
        self.seen: list[bool] = []

    def during(self) -> None:
        """Call from inside the blocking call: records whether the loop ran meanwhile."""
        self._ran.clear()
        self.seen.append(self._ran.wait(timeout=10))

    async def tick(self) -> None:
        while True:
            self._ran.set()
            await asyncio.sleep(0.001)
