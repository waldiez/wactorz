"""The application-log endpoint.

`GET /api/logs` serves the in-memory ring buffer the root logger writes to, in
the same envelope the activity feed already renders for agent events. A pull,
not a push: the caller must ask for the records, and nothing is streamed to a
connected page as it is logged.

**Registered under `/api/` deliberately.** Whatever authentication lands will
be middleware over that prefix, so this route is covered by it with no further
edit here — and until then it is open, like the rest of the API.

**What this exposes is different in kind from the rest.** Chat disclosure is
bounded by what was said; a log carries what nobody chose to write there —
resolved config, file paths, and third-party output from libraries that log
freely. The buffer redacts the shapes it knows on the way in (see
`monitoring.log_redaction`), which is a floor rather than a guarantee: no
redactor is complete against machine-authored text.
"""

import logging
from typing import Any

from aiohttp import web
from aiohttp.web import Response

from ..monitoring.log_buffer import get_buffer

logger = logging.getLogger(__name__)

#: Most records one request may take. The buffer holds ~1000, and a caller does
#: not get to choose how large a response the server builds.
MAX_LIMIT = 500

#: Default when the caller names no limit — a screenful of history, not the lot.
DEFAULT_LIMIT = 200

#: Severity order, lowest first. Mirrors `LEVEL_ORDER` in the frontend; the two
#: are separate because neither side should have to ask the other what to draw.
LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _clamp_limit(raw: str | None) -> int:
    """The number of records to return, whatever the caller asked for.

    Anything unparseable, negative or oversized becomes a sane value rather than
    an error: this is a diagnostic endpoint, and refusing the request outright
    helps nobody who is trying to read a log.
    """
    if raw is None:
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _at_or_above(level: str | None) -> set[str] | None:
    """The level names at or above `level`, or None to accept every record.

    An unknown name accepts everything rather than nothing — a filter nobody
    recognises should not silently empty the view someone opened to diagnose
    something.
    """
    if not level:
        return None
    wanted = level.strip().upper()
    if wanted not in LEVELS:
        return None
    return set(LEVELS[LEVELS.index(wanted) :])


def _matches(entry: dict[str, Any], allowed: set[str] | None, origin: str) -> bool:
    """Whether one record survives the requested filters.

    A record whose level we do not rank is *kept*. Python allows custom levels
    (`NOTICE`, `TRACE`, and whatever a library registers), and excluding them
    would mean even `?level=DEBUG` — the most permissive request there is —
    silently dropped records. That is the same mistake `_at_or_above` refuses to
    make for an unknown filter value, and it is worse here: nobody asked for it.
    """
    level = str(entry.get("level", ""))
    if allowed is not None and level in LEVELS and level not in allowed:
        return False
    return not origin or origin in str(entry.get("origin", "")).lower()


async def logs_handler(request: web.Request) -> Response:
    """GET /api/logs — recent application-log records, oldest first.

    Query: `limit` (clamped), `level` (this and above), `logger` (substring of
    the logger name).
    """
    buffer = get_buffer()
    if buffer is None:
        # Before `install()` runs, or in a process that never called it. Not an
        # error: there is simply nothing recorded yet.
        return web.json_response({"entries": [], "capacity": 0})

    limit = _clamp_limit(request.query.get("limit"))
    allowed = _at_or_above(request.query.get("level"))
    origin = request.query.get("logger", "").strip().lower()

    # Filtered before the limit is applied, so `limit=50&level=ERROR` means the
    # 50 most recent *errors* rather than the errors among the 50 most recent.
    matching = [e for e in buffer.snapshot() if _matches(e, allowed, origin)]
    return web.json_response(
        {"entries": matching[-limit:], "capacity": buffer.capacity},
    )
