"""The data directory over the API: which sources are recorded, and what a window of one holds.

- ``GET /api/data``: every source with files, what each holds (`wactorz.data.files.summary`)
  and the retention in force;
- ``GET /api/data/{source}/profile``: the dataset card `wactorz.data.frames.profile`
  makes of a window, ``since`` and ``until`` in Unix seconds, the last day when unsaid;
- ``GET /api/data/{source}/series``: one ``column`` over a window in ``points`` buckets
  (`wactorz.data.frames.series`), for a chart.

A source has slashes in it (``sensors/imu/left-wrist``), so it is the rest of
the path. Reading the files needs pyarrow, the ``data`` extra; without it the
routes answer ``503`` saying so. ``docs/design/data-storage.md`` is the decision.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from aiohttp import web

from ..config import DATA_RETENTION_DAYS
from ..core.paths import resolve_state_dir
from ..data.files import DataError, check_source, data_root, summaries

#: How far back a profile looks when the window is unsaid.
DEFAULT_WINDOW_S = 24 * 3600.0

#: The most rows a profile reads, so a request cannot ask main to hold a year.
MAX_ROWS = 2_000_000

#: How many buckets a series has when unsaid, and the most it may have.
DEFAULT_POINTS = 200
MAX_POINTS = 2000


class _BadQueryError(ValueError):
    """A query parameter that is not what the route takes."""

    @classmethod
    def not_seconds(cls, name: str) -> _BadQueryError:
        return cls(f"'{name}' is Unix seconds")

    @classmethod
    def ends_first(cls) -> _BadQueryError:
        return cls("'until' comes after 'since'")

    @classmethod
    def not_a_count(cls) -> _BadQueryError:
        return cls(f"'points' is a whole number from 1 to {MAX_POINTS}")


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


def _window(request: web.Request) -> tuple[float, float]:
    """The window asked for: ``[since, until)``, the last day ending now when unsaid."""
    since, until = _seconds(request, "since"), _seconds(request, "until")
    if until is None:
        until = time.time()
    if since is None:
        since = until - DEFAULT_WINDOW_S
    if until <= since:
        raise _BadQueryError.ends_first()
    return since, until


def _no_pyarrow() -> web.Response:
    return web.json_response(
        {"error": "the data files cannot be read: install wactorz[data]"}, status=503
    )


async def sources_handler(_request: web.Request) -> web.Response:
    """``GET /api/data``: each source with files, what it holds, and the retention in force."""
    found = await asyncio.to_thread(summaries, data_root(resolve_state_dir()))
    return web.json_response({"sources": found, "retention_days": DATA_RETENTION_DAYS or None})


async def series_handler(request: web.Request) -> web.Response:
    """``GET /api/data/{source}/series``: one column over a window, in buckets, for a chart."""
    source = request.match_info["source"]
    column = (request.query.get("column") or "").strip()
    if not column:
        return web.json_response({"error": "'column' names the column to draw"}, status=400)
    try:
        check_source(source)
        since, until = _window(request)
        points = _points(request)
    except (DataError, _BadQueryError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    try:
        from ..data import frames, parquet  # optional dependency: the `data` extra
    except ModuleNotFoundError:
        return _no_pyarrow()
    root = data_root(resolve_state_dir())

    def drawn() -> dict[str, Any]:
        table = parquet.frame(root, source, since, until, columns=[column])
        return frames.series(table, column, since=since, until=until, points=points)

    try:
        made = await asyncio.to_thread(drawn)
    except frames.SplitError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    return web.json_response({"source": source, **made})


def _points(request: web.Request) -> int:
    raw = request.query.get("points")
    if raw is None or raw == "":
        return DEFAULT_POINTS
    try:
        value = int(raw)
    except ValueError:
        raise _BadQueryError.not_a_count() from None
    if not 1 <= value <= MAX_POINTS:
        raise _BadQueryError.not_a_count()
    return value


async def profile_handler(request: web.Request) -> web.Response:
    """``GET /api/data/{source}/profile``: the dataset card of a window of ``source``."""
    source = request.match_info["source"]
    try:
        check_source(source)
        since, until = _window(request)
    except (DataError, _BadQueryError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    try:
        from ..data import frames, parquet  # optional dependency: the `data` extra
    except ModuleNotFoundError:
        return _no_pyarrow()
    root = data_root(resolve_state_dir())

    def card() -> dict[str, Any]:
        table = parquet.frame(root, source, since, until)
        if table.num_rows > MAX_ROWS:
            table = table.slice(0, MAX_ROWS)
        return frames.profile(table, source=source)

    made = await asyncio.to_thread(card)
    return web.json_response({**made, "since": since, "until": until})


def setup(app: web.Application) -> None:
    app.router.add_get("/api/data", sources_handler)
    app.router.add_get("/api/data/{source:.+}/profile", profile_handler)
    app.router.add_get("/api/data/{source:.+}/series", series_handler)
