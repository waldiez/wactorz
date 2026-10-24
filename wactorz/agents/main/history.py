"""Main answering reads of its stored history for agents running on nodes.

The time series, detections and Home Assistant state changes live in main's
SQLite, and a node has none of it. An agent there calls ``agent.query_ts(...)``
as it would on main; the call goes out over the broker to
``main/history_request`` (see :mod:`wactorz.node.history`), and main answers
from its database here. The request is admitted by the same rules as a model
call -- a signature made with the node's key, a reply topic in that node's own
space -- which is why :class:`~wactorz.agents.main.llm_bridge.LLMBridge` serves
both topics and this module only turns an admitted request into its reply.

**Read-only, and only what a query names.** A request picks one of `QUERIES`
and fills in that query's filters; nothing else reaches the database, and every
value is a bound parameter there.

**A page at a time.** A reply is one MQTT message, and a broker bounds those
(the one Wactorz ships allows 256 KiB), so main answers with the rows that fit
in `PAGE_BYTES` and the offset to ask for next. The window is fixed by the
first page: main answers with the ``since`` it used, the node sends it back, and
the database orders rows by time and then by insertion, so rows written while a
reader pages arrive at the end rather than shifting the pages already read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any

from ...core.persistence import get_db

logger = logging.getLogger(__name__)

#: The topic agents on nodes publish their history reads to.
HISTORY_TOPIC = "main/history_request"

#: What one reply's rows may take, encoded. Under the broker's packet limit with
#: room for the reply's own fields and the topic.
PAGE_BYTES = 192 * 1024

#: The most rows read from the database for one page, before trimming to
#: `PAGE_BYTES`. Bounds the work one request costs main.
PAGE_ROWS = 2_000

#: How far back a request may reach, in hours. Retention prunes long before this;
#: the bound only keeps the arithmetic finite.
MAX_HOURS = 24 * 366 * 10

#: Longest text a filter may carry. Entity ids and topics are far shorter.
MAX_FILTER_CHARS = 512

#: Each query an agent may ask: the database method that answers it, and the
#: filters it takes with the type each must have.
QUERIES: dict[str, tuple[str, dict[str, type]]] = {
    "ts": ("query_sensor", {"topic": str, "entity_id": str, "field": str}),
    "detections": (
        "query_detections",
        {"agent": str, "class_name": str, "min_confidence": float},
    ),
    "ha_states": ("query_ha_states", {"entity_id": str, "domain": str}),
}

#: The query answered with the tables' row counts rather than rows.
STATS = "stats"


class BadRequest(ValueError):
    """A history request main will not run, with the reason the caller is told."""

    @classmethod
    def unknown_query(cls, query: Any) -> BadRequest:
        return cls(f"unknown query {query!r}; one of {sorted([*QUERIES, STATS])}")

    @classmethod
    def unknown_filter(cls, query: str, name: str) -> BadRequest:
        return cls(f"{query} takes no filter {name!r}")

    @classmethod
    def wrong_type(cls, name: str, wanted: type) -> BadRequest:
        return cls(f"{name} must be a {wanted.__name__}")

    @classmethod
    def out_of_range(cls, name: str) -> BadRequest:
        return cls(f"{name} is out of range")


async def answer_history(data: dict[str, Any]) -> dict[str, Any]:
    """The reply to one admitted history request: a page of rows, the counts, or an error."""
    try:
        query = data.get("query")
        if query == STATS:
            return {"stats": await _stats()}
        if not isinstance(query, str) or query not in QUERIES:
            raise BadRequest.unknown_query(query)
        method, filters = _filters(query, data.get("filters"))
        hours = _number(data.get("hours", 24), "hours", low=0.0, high=MAX_HOURS)
        since = data.get("since")
        since = time.time() - hours * 3600 if since is None else _number(since, "since")
        offset = _count(data.get("offset", 0), "offset", low=0)
        wanted = _count(data.get("limit", PAGE_ROWS), "limit", low=1)
    except BadRequest as exc:
        return {"error": str(exc)}

    db = get_db()
    if db is None:
        # As on main before persistence is up: nothing recorded, not a failure.
        return {"rows": [], "since": since, "next": None}
    fetch = min(wanted, PAGE_ROWS)
    rows = await asyncio.to_thread(
        getattr(db, method), since=since, offset=offset, limit=fetch, **filters
    )
    page, used, skipped = fit_page(rows)
    more = used < len(rows) or len(rows) == fetch
    reply: dict[str, Any] = {
        "rows": page,
        "since": since,
        "next": offset + used if more and used < wanted else None,
    }
    if skipped:
        reply["skipped"] = skipped
    return reply


def fit_page(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """The leading ``rows`` that fit in `PAGE_BYTES`, how many were used, and how many skipped.

    A row larger than a whole page on its own can never be sent, so it is
    skipped and counted rather than stopping the read there for good. Used
    counts it too, so the next page starts after it.
    """
    page: list[dict[str, Any]] = []
    size = 2  # the list's brackets
    skipped = 0
    for row in rows:
        cost = len(json.dumps(row, default=str)) + 1
        if cost + 2 > PAGE_BYTES:
            if page:
                break
            skipped += 1
            continue
        if size + cost > PAGE_BYTES:
            break
        page.append(row)
        size += cost
    return page, len(page) + skipped, skipped


async def _stats() -> dict[str, Any]:
    db = get_db()
    return {} if db is None else await asyncio.to_thread(db.stats)


def _filters(query: str, raw: Any) -> tuple[str, dict[str, Any]]:
    """The database method for ``query``, and ``raw`` checked as its filters."""
    method, allowed = QUERIES[query]
    if raw is None:
        return method, {}
    if not isinstance(raw, dict):
        raise BadRequest.wrong_type("filters", dict)
    filters: dict[str, Any] = {}
    for name, value in raw.items():
        wanted = allowed.get(name)
        if wanted is None:
            raise BadRequest.unknown_filter(query, str(name))
        if value is None:
            continue
        if wanted is float:
            filters[name] = _number(value, name)
        elif not isinstance(value, str):
            raise BadRequest.wrong_type(name, wanted)
        elif len(value) > MAX_FILTER_CHARS:
            raise BadRequest.out_of_range(name)
        else:
            filters[name] = value
    return method, filters


def _number(value: Any, name: str, *, low: float | None = None, high: float | None = None) -> float:
    """``value`` as a finite float within ``low``..``high``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadRequest.wrong_type(name, float)
    number = float(value)
    if not math.isfinite(number):
        raise BadRequest.out_of_range(name)
    if (low is not None and number < low) or (high is not None and number > high):
        raise BadRequest.out_of_range(name)
    return number


def _count(value: Any, name: str, *, low: int) -> int:
    """``value`` as an int no smaller than ``low``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest.wrong_type(name, int)
    if value < low:
        raise BadRequest.out_of_range(name)
    return value
