"""Main's stored history, as read from a node.

A node keeps no time series, detections or Home Assistant state changes: those
live in main's SQLite. An agent here calls ``await agent.query_ts(...)`` as it
would on main, and the read goes out over the broker to ``main/history_request``,
signed like a model call, and comes back a page at a time (see
:mod:`wactorz.agents.main.history` for why pages, and how their window stays
fixed). This module asks for the pages until the read is complete and hands the
agent one list.

**All or nothing.** A read main does not finish answering -- a page that never
arrives, or one that says why it was refused -- gives the agent no rows and a
warning in the log, never the pages that did arrive passed off as the whole.
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import NodeAgent

logger = logging.getLogger(__name__)

#: Where history reads are sent.
HISTORY_TOPIC = "main/history_request"

#: How long one page waits for main. Generous: main reads the page from disk
#: off its event loop, and a Pi's SD card under load is slow but not broken.
PAGE_TIMEOUT_S = 60.0

#: What a read that main never answers usually means, said once in the warning.
UNANSWERED_HINT = (
    "main did not answer. A broker whose access list predates history reads "
    "drops them: restart main so it writes the list again, or add "
    "`topic write main/history_request` to this node's account by hand."
)


async def read_history(
    agent: "NodeAgent", query: str, filters: dict[str, Any], *, hours: float, limit: int
) -> list[dict[str, Any]] | None:
    """Up to ``limit`` rows of ``query`` from main, or None if main did not give them all."""
    rows: list[dict[str, Any]] = []
    request: dict[str, Any] = {
        "query": query,
        "filters": filters,
        "hours": hours,
        "offset": 0,
    }
    while len(rows) < limit:
        request["limit"] = limit - len(rows)
        reply = await agent.ask_main(HISTORY_TOPIC, dict(request), timeout=PAGE_TIMEOUT_S)
        page = _page(agent, query, reply)
        if page is None:
            return None
        rows.extend(page)
        if reply.get("skipped"):
            logger.warning(
                "[%s] %d %s row(s) were too large to send from main and were left out",
                agent.name,
                reply["skipped"],
                query,
            )
        following = reply.get("next")
        if not isinstance(following, int) or following <= request["offset"]:
            break
        request["offset"] = following
        request["since"] = reply.get("since")
    return rows[:limit]


async def read_stats(agent: "NodeAgent") -> dict[str, Any] | None:
    """The row counts of main's history tables, or None if main did not give them."""
    reply = await agent.ask_main(HISTORY_TOPIC, {"query": "stats"}, timeout=PAGE_TIMEOUT_S)
    if not isinstance(reply, dict) or not isinstance(reply.get("stats"), dict):
        _warn(agent, "stats", reply)
        return None
    return reply["stats"]


def _page(agent: "NodeAgent", query: str, reply: Any) -> list[dict[str, Any]] | None:
    """The rows in ``reply``, or None -- having said why -- if it carries none."""
    if isinstance(reply, dict) and isinstance(reply.get("rows"), list):
        return [row for row in reply["rows"] if isinstance(row, dict)]
    _warn(agent, query, reply)
    return None


def _warn(agent: "NodeAgent", query: str, reply: Any) -> None:
    if isinstance(reply, dict) and reply.get("error"):
        reason = f"main refused it: {reply['error']}"
    elif reply is None:
        reason = UNANSWERED_HINT
    else:
        reason = "main's reply was not one this node understands"
    logger.warning("[%s] Reading %s history from main failed; %s", agent.name, query, reason)
