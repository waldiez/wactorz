"""Pushing application-log records to open dashboards as they are written.

A pump watches the ring buffer and broadcasts what is new, so a page left open
follows the log rather than showing whatever it fetched when it loaded.
`GET /api/logs` still serves history — a page asks for that once, then this
keeps it current.

**A pump that polls, rather than a send from inside the handler.**
`logging.Handler.emit` is synchronous and can be called from any thread, so it
cannot await a broadcast, and handing work to an `asyncio.Queue` from a foreign
thread is not safe either. Polling a `deque` that only ever grows at one end
needs neither: `deque.append` is atomic, and the pump reads a position on the
event loop like any other coroutine. The cost is latency of one interval, which
for a log view is nothing.

**The feedback loop this has to avoid.** Broadcasting can log — a failing send
certainly does — and a record written by the push path is a record the next tick
would push, which fails again, and so on at the poll rate. Records from the
loggers involved in sending are therefore never pushed. They are still buffered
and still served by `GET /api/logs`, so nothing is hidden; they simply do not
feed the thing that produced them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..monitoring.log_buffer import get_buffer
from . import runtime, ws

logger = logging.getLogger(__name__)

#: How often the pump looks for new records. Fast enough to read as live, slow
#: enough that an idle dashboard costs nothing measurable.
POLL_SECONDS = 0.5

#: Most records in one frame. A burst — a traceback storm, a library at DEBUG —
#: should not become a single multi-megabyte message that stalls every client.
#: What is dropped is the oldest of the burst, and the page can still fetch the
#: full history from `/api/logs`.
MAX_PER_FRAME = 100

#: Loggers whose records are never pushed, because pushing is what produces
#: them. Buffered and served over `/api/logs` as normal — excluded only from the
#: live stream, and only to stop a failing send from feeding itself.
_NEVER_PUSHED = ("aiohttp.", __name__)


def _pushable(entry: dict[str, Any]) -> bool:
    origin = str(entry.get("origin", ""))
    return not origin.startswith(_NEVER_PUSHED)


async def log_push_loop(interval: float = POLL_SECONDS) -> None:
    """Broadcast new log records to connected dashboards, forever.

    Starts from *now*, not from the beginning of the buffer: a page fetches its
    own history from `/api/logs` on load, and replaying it here would double
    every row on every reconnect.
    """
    buffer = get_buffer()
    if buffer is None:
        # No buffer installed — nothing records, so nothing streams. Not an
        # error: an embedding app may never have called `install()`.
        return

    position = buffer.total
    while True:
        await asyncio.sleep(interval)
        try:
            entries, position = buffer.since(position)
            if not entries or not runtime.ws_clients:
                continue
            pushable = [entry for entry in entries if _pushable(entry)][-MAX_PER_FRAME:]
            if pushable:
                await ws.broadcast({"type": "app_log", "entries": pushable})
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never logged: this is the one place where reporting the failure is
            # what causes the next one. The pump keeps running, because a
            # dashboard that silently stops following is the failure this whole
            # feature exists to remove.
            continue
