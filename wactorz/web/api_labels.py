"""The labels main keeps, for the dashboard.

``GET /api/labels`` lists labels, oldest recorded first, filtered by
``source``, by a window of event time (``since``, ``until``, Unix seconds) and
by ``limit``; superseded ones only with ``all=1``. ``GET /api/labels/effective``
resolves one ``source`` over a window into what held when: the newest label at
each moment (`wactorz.core.labels.Labels.effective`).

Labels are written by agents, rules and the operator's answers, never here;
see docs/design/labels-and-provenance.md. The database blocks, so every call
goes through a thread rather than holding the event loop.
"""

import asyncio
import math
from typing import Any

from aiohttp import web

from ..core.labels import Labels
from ..core.persistence.stores import get_db
from . import runtime

#: The most labels one listing returns.
MAX_LIMIT = 5000

#: How many a listing returns when it does not say.
DEFAULT_LIMIT = 1000


class _BadQueryError(ValueError):
    """A query parameter that is not what the route takes."""

    @classmethod
    def not_seconds(cls, name: str) -> "_BadQueryError":
        return cls(f"'{name}' is Unix seconds")

    @classmethod
    def not_a_count(cls) -> "_BadQueryError":
        return cls("'limit' is a whole number")

    @classmethod
    def ends_first(cls) -> "_BadQueryError":
        return cls("'until' comes after 'since'")


def _labels() -> Labels | None:
    db = runtime.db if runtime.db is not None else get_db()
    return None if db is None else Labels(db)


def _seconds(request: web.Request, name: str) -> float | None:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        raise _BadQueryError.not_seconds(name) from None
    if not math.isfinite(value) or value < 0:
        raise _BadQueryError.not_seconds(name)
    return value


def _window(request: web.Request) -> tuple[float | None, float | None]:
    since, until = _seconds(request, "since"), _seconds(request, "until")
    if since is not None and until is not None and until <= since:
        raise _BadQueryError.ends_first()
    return since, until


def _limit(request: web.Request) -> int:
    raw = request.query.get("limit")
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        raise _BadQueryError.not_a_count() from None
    if value < 1:
        raise _BadQueryError.not_a_count()
    return min(value, MAX_LIMIT)


def _unavailable() -> web.Response:
    return web.json_response(
        {"error": "persistence is not running, so there are no labels"}, status=503
    )


async def labels_handler(request: web.Request) -> web.Response:
    """``GET /api/labels``: labels by source and event-time window."""
    labels = _labels()
    if labels is None:
        return _unavailable()
    try:
        since, until = _window(request)
        limit = _limit(request)
    except _BadQueryError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    source = request.query.get("source") or None
    include_superseded = request.query.get("all") in ("1", "true", "yes")
    found = await asyncio.to_thread(
        labels.query,
        source,
        since=since,
        until=until,
        include_superseded=include_superseded,
        limit=limit,
    )
    return web.json_response({"labels": [label.to_dict() for label in found]})


async def effective_handler(request: web.Request) -> web.Response:
    """``GET /api/labels/effective``: what one source was doing, moment by moment."""
    labels = _labels()
    if labels is None:
        return _unavailable()
    source = request.query.get("source")
    if not source:
        return web.json_response({"error": "name the 'source' to resolve"}, status=400)
    try:
        since, until = _window(request)
    except _BadQueryError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    intervals = await asyncio.to_thread(labels.effective, source, since=since, until=until)
    body: dict[str, Any] = {
        "source": source,
        "since": since,
        "until": until,
        "intervals": [i.to_dict() for i in intervals],
    }
    return web.json_response(body)
