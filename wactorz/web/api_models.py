"""The models main keeps, and their pointers, for the dashboard.

``GET /api/models`` lists each name with its newest version and its pointers;
``GET /api/models/{name}`` gives every version, with what was said of it, and
the pointers. The rest move a pointer, as the CLI and the chat do, recorded as
made ``by`` the dashboard:

- ``POST /api/models/{name}/active`` and ``.../shadow``, ``{"version": 3}``;
- ``POST /api/models/{name}/promote``: active to where shadow is, shadow cleared;
- ``POST /api/models/{name}/rollback``: active back to the version it left;
- ``DELETE /api/models/{name}/shadow``: the trial ended.

``GET /api/models/{name}/trial`` is what the shadow trial shows so far, or showed
over a window: its pairs compared, and against the labels of a source when
``source`` and ``positive`` say which values count; ``/trials`` the outcomes of
the trials that ended. ``GET /api/models/{name}/moves`` is the history of those moves, newest first,
from the provenance graph in main's database (`wactorz.core.pointer_moves`).

Each move takes an optional ``reason``. The answer is where the pointer stands
now; the retained ``models/<name>/<pointer>`` topic follows within main's next
look at the index (`wactorz.agents.main.artifact_service`). A move the pointers
do not allow -- already there, nothing to promote or roll back -- is a 409, a
name or version that is not stored a 404, a request that is not one a 400.

The store reads and writes files under a lock, so every call goes through a
thread rather than holding the event loop.
"""

import asyncio
import json
import time
from dataclasses import asdict
from typing import Any

from aiohttp import web

from ..core.artifacts import (
    Artifact,
    ArtifactConflictError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactStore,
    Pointer,
    describe,
    name_error,
)
from ..core.labels import Labels
from ..core.paths import resolve_state_dir
from ..core.persistence.stores import get_db
from ..core.pointer_moves import DEFAULT_LIMIT, history
from ..core.provenance import Provenance
from ..core.shadow_trial import compare, outcomes_of
from ..data.files import DataError, data_root
from . import runtime

#: Who a move made here is recorded as made by.
BY = "dashboard"

#: The longest reason kept with a move. A reason is a sentence, not a document.
MAX_REASON_CHARS = 500

#: Most moves one request for the history may ask for.
MAX_MOVES = 1000


def _store() -> ArtifactStore:
    """The store, where the state directory is at the time of asking."""
    return ArtifactStore.in_state_dir(resolve_state_dir())


def _pointer(pointer: Pointer, versions: list[Artifact]) -> dict[str, Any]:
    """A pointer as the dashboard shows it, saying whether its version still holds its bytes."""
    held = next((v for v in versions if v.version == pointer.version), None)
    return {**asdict(pointer), "holds": held is not None and held.sha256 == pointer.sha256}


def _summary(store: ArtifactStore, name: str) -> dict[str, Any] | None:
    versions = store.versions(name)
    if not versions:
        return None
    return {
        "name": name,
        "newest": describe(versions[-1]),
        "versions": len(versions),
        "pointers": {
            which: _pointer(pointer, versions) for which, pointer in store.pointers(name).items()
        },
    }


def _listing(store: ArtifactStore) -> list[dict[str, Any]]:
    return [s for s in (_summary(store, name) for name in store.names()) if s is not None]


def _detail(store: ArtifactStore, name: str) -> dict[str, Any]:
    versions = store.versions(name)
    if not versions:
        raise ArtifactNotFoundError(name)
    return {
        "name": name,
        "versions": [describe(v) for v in versions],
        "pointers": {
            which: _pointer(pointer, versions) for which, pointer in store.pointers(name).items()
        },
    }


def _refusal(exc: ArtifactError) -> web.Response:
    """The status an artifact error answers with, and its words."""
    if isinstance(exc, ArtifactNotFoundError):
        status = 404
    elif isinstance(exc, ArtifactConflictError):
        status = 409
    else:
        status = 400
    return web.json_response({"error": str(exc)}, status=status)


def _name(request: web.Request) -> str | web.Response:
    """The name in the path, or the 400 that says why it cannot name a model."""
    name = request.match_info["name"]
    error = name_error(name)
    return web.json_response({"error": error}, status=400) if error else name


async def _body(request: web.Request) -> dict[str, Any] | web.Response:
    """The request's JSON object, empty when there is no body, or the 400 for one that is not."""
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "the body is not JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "the body is a JSON object"}, status=400)
    return body


def _reason(body: dict[str, Any]) -> str | web.Response | None:
    """The move's reason: a string, None when it gives none, or the 400 for one that is not."""
    reason = body.get("reason")
    if reason is None:
        return None
    if not isinstance(reason, str):
        return web.json_response({"error": "a reason is text"}, status=400)
    return reason.strip()[:MAX_REASON_CHARS] or None


def _version(body: dict[str, Any]) -> int | web.Response:
    """The version a move names, or the 400 for a body that names none."""
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return web.json_response({"error": "name the version, a number from 1"}, status=400)
    return version


async def models_handler(_request: web.Request) -> web.Response:
    """Every model name, its newest version and its pointers."""
    try:
        listing = await asyncio.to_thread(_listing, _store())
    except ArtifactError as exc:
        return _refusal(exc)
    return web.json_response({"models": listing})


async def model_handler(request: web.Request) -> web.Response:
    """One model: every version, with what was said of it, and its pointers."""
    name = _name(request)
    if isinstance(name, web.Response):
        return name
    try:
        detail = await asyncio.to_thread(_detail, _store(), name)
    except ArtifactError as exc:
        return _refusal(exc)
    return web.json_response(detail)


async def moves_handler(request: web.Request) -> web.Response:
    """A model's pointer moves, newest first; ``limit`` caps how many.

    For any name that can be one: the history outlives the versions it moved
    between, so a model whose versions were all removed still has its moves.
    """
    name = _name(request)
    if isinstance(name, web.Response):
        return name
    limit = _count(request.query.get("limit"))
    if limit is None:
        return web.json_response({"error": f"'limit' is a count from 1 to {MAX_MOVES}"}, status=400)
    db = runtime.db if runtime.db is not None else get_db()
    if db is None:
        return web.json_response(
            {"error": "persistence is not running, so no moves are recorded"}, status=503
        )
    moves = await asyncio.to_thread(history, Provenance(db), name, limit=limit)
    return web.json_response({"name": name, "moves": moves})


async def trial_handler(request: web.Request) -> web.Response:
    """What the shadow trial of a model shows: its pairs compared over a window.

    The window is the trial's, from when ``shadow`` was set to now, unless
    ``since`` and ``until`` (Unix seconds) say otherwise; with no shadow set
    and no window given there is no trial to show. ``source`` and ``positive``
    (label values, comma separated) add each side's precision and recall over
    the pairs a label of that source covers.
    """
    name = _name(request)
    if isinstance(name, web.Response):
        return name
    store = _store()
    try:
        shadow = await asyncio.to_thread(store.pointer, name, "shadow")
    except ArtifactError as exc:
        return _refusal(exc)
    since = _moment(request.query.get("since"))
    until = _moment(request.query.get("until"))
    if since is False or until is False:
        return web.json_response({"error": "'since' and 'until' are Unix seconds"}, status=400)
    if since is None:
        if shadow is None:
            return web.json_response(
                {"error": f"{name} has no shadow version on trial"}, status=404
            )
        since = shadow.moved_at
    if until is None:
        until = time.time()
    try:
        from ..data import parquet  # optional dependency: the `data` extra
    except ModuleNotFoundError:
        return web.json_response(
            {"error": "the pairs cannot be read: install wactorz[data]"}, status=503
        )
    root = data_root(resolve_state_dir())

    def read() -> list[dict[str, Any]]:
        return parquet.frame(root, f"shadow/{name}", since, until).to_pylist()

    try:
        rows = await asyncio.to_thread(read)
    except DataError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    source = (request.query.get("source") or "").strip()
    positive = [v.strip() for v in (request.query.get("positive") or "").split(",") if v.strip()]
    intervals = None
    if source and positive:
        db = runtime.db if runtime.db is not None else get_db()
        if db is None:
            return web.json_response(
                {"error": "persistence is not running, so no labels can be read"}, status=503
            )
        intervals = await asyncio.to_thread(Labels(db).effective, source, since=since, until=until)
    summary = compare(rows, intervals=intervals, positive=positive)
    return web.json_response(
        {
            "name": name,
            "shadow": None if shadow is None else asdict(shadow),
            "since": since,
            "until": until,
            "source": source or None,
            **summary,
        }
    )


async def trials_handler(request: web.Request) -> web.Response:
    """The outcomes of the model's trials that ended, newest first."""
    name = _name(request)
    if isinstance(name, web.Response):
        return name
    db = runtime.db if runtime.db is not None else get_db()
    if db is None:
        return web.json_response(
            {"error": "persistence is not running, so no trials are recorded"}, status=503
        )
    trials = await asyncio.to_thread(outcomes_of, Provenance(db), name)
    return web.json_response({"name": name, "trials": trials})


def _moment(raw: str | None) -> float | bool | None:
    """``raw`` as Unix seconds; None when unsaid; False when it is not a time."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return False


def _count(raw: str | None) -> int | None:
    """``limit`` as a count of moves, the default when unsaid; None when it is not one."""
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= MAX_MOVES else None


async def _moved(call: Any, *args: Any, **kwargs: Any) -> web.Response:
    """Run one pointer change on the store and answer with where the pointer stands."""
    try:
        pointer = await asyncio.to_thread(call, *args, **kwargs)
    except ArtifactError as exc:
        return _refusal(exc)
    return web.json_response({"pointer": asdict(pointer)})


async def _point(request: web.Request, which: str) -> web.Response:
    name = _name(request)
    if isinstance(name, web.Response):
        return name
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    version = _version(body)
    if isinstance(version, web.Response):
        return version
    reason = _reason(body)
    if isinstance(reason, web.Response):
        return reason
    return await _moved(_store().move, which, f"{name}:{version}", by=BY, reason=reason)


async def set_active_handler(request: web.Request) -> web.Response:
    """Point ``active`` at the version the body names."""
    return await _point(request, "active")


async def set_shadow_handler(request: web.Request) -> web.Response:
    """Point ``shadow`` at the version the body names."""
    return await _point(request, "shadow")


async def _named_move(request: web.Request, method: str) -> web.Response:
    """A move that needs only the name and a reason: promote or roll back."""
    name = _name(request)
    if isinstance(name, web.Response):
        return name
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    reason = _reason(body)
    if isinstance(reason, web.Response):
        return reason
    return await _moved(getattr(_store(), method), name, by=BY, reason=reason)


async def promote_handler(request: web.Request) -> web.Response:
    """Make the shadow version active, and end the trial."""
    return await _named_move(request, "promote")


async def rollback_handler(request: web.Request) -> web.Response:
    """Move ``active`` back to the version it named before its last move."""
    return await _named_move(request, "rollback")


async def clear_shadow_handler(request: web.Request) -> web.Response:
    """End the trial: unset ``shadow``, with the body's optional ``reason``."""
    name = _name(request)
    if isinstance(name, web.Response):
        return name
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    reason = _reason(body)
    if isinstance(reason, web.Response):
        return reason
    return await _moved(_store().clear, "shadow", name, by=BY, reason=reason)
