# SPDX-License-Identifier: Apache-2.0
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
the trials that ended; ``/trial/series`` the same pairs over time, for a chart.
``GET /api/models/{name}/drift`` is the drift alarms the drift agent raised
about the model, newest first (`wactorz.drift`).
``GET /api/models/{name}/moves`` is the history of those moves, newest first,
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
import math
import time
from dataclasses import asdict, dataclass, replace
from typing import Any

from aiohttp import web

from ..agents.shadow_serving import shadow_source
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
from ..core.judgement import Judgement, JudgementError, declared
from ..core.judges import labels_for
from ..core.labels import Labels
from ..core.paths import resolve_state_dir
from ..core.persistence.stores import get_db
from ..core.pointer_moves import DEFAULT_LIMIT, history
from ..core.promotion import PolicyError, judge_trial
from ..core.promotion import declared as policy_declared
from ..core.provenance import Provenance
from ..core.shadow_trial import (
    compare,
    event_span,
    judgement_of_trial,
    outcomes_of,
    series,
)
from ..data.files import LAST_SECOND, DataError, data_root
from ..drift import alarms_of
from . import runtime

#: Who a move made here is recorded as made by.
BY = "dashboard"

#: The longest reason kept with a move. A reason is a sentence, not a document.
MAX_REASON_CHARS = 500

#: Most moves one request for the history may ask for.
MAX_MOVES = 1000

#: How many stretches a trial's series is cut into, unless asked otherwise, and at most.
SERIES_POINTS = 120
MAX_SERIES_POINTS = 1000


def _store() -> ArtifactStore:
    """The store, where the state directory is at the time of asking."""
    return ArtifactStore.in_state_dir(resolve_state_dir())


def _pointer(pointer: Pointer, versions: list[Artifact]) -> dict[str, Any]:
    """A pointer as the dashboard shows it, saying whether its version still holds its bytes."""
    held = next((v for v in versions if v.version == pointer.version), None)
    return {**asdict(pointer), "holds": held is not None and held.sha256 == pointer.sha256}


def _policy_of(store: ArtifactStore, name: str) -> dict[str, Any] | None:
    policy = store.policy(name)
    return None if policy is None else policy.to_json()


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
        "policy": _policy_of(store, name),
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
        "policy": _policy_of(store, name),
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


@dataclass(frozen=True)
class _Window:
    """The stretch of a trial a request asks about: the time its pairs arrived in, not their event time."""

    name: str
    shadow: Pointer | None
    since: float
    until: float


async def _trial_window(request: web.Request) -> _Window | web.Response:
    """The window asked for: the trial's own, from when ``shadow`` was set to now,
    unless ``since`` and ``until`` (Unix seconds) say otherwise; with no shadow set
    and no window given there is no trial to show.
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
    return _Window(name, shadow, since, time.time() if until is None else until)


async def _pairs(window: _Window) -> list[dict[str, Any]] | web.Response:
    """The pairs that arrived in ``window``, or why they cannot be read."""
    try:
        from ..data import parquet  # optional dependency: the `data` extra
    except ModuleNotFoundError:
        return web.json_response(
            {"error": "the pairs cannot be read: install wactorz[data]"}, status=503
        )
    root = data_root(resolve_state_dir())
    try:
        return await asyncio.to_thread(
            parquet.rows_arrived_in, root, shadow_source(window.name), window.since, window.until
        )
    except DataError as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def trial_handler(request: web.Request) -> web.Response:
    """What the shadow trial of a model shows: its pairs compared over a window.

    The pairs are judged against the labels as the version on trial declares
    (`wactorz.core.judgement`), else as the active version does. ``positive``
    (label values, comma separated) judges them as a detector of those values
    instead, and ``source`` names the one label source every input is judged
    against, over the declaration's or each input's own.
    """
    window = await _trial_window(request)
    if isinstance(window, web.Response):
        return window
    rows = await _pairs(window)
    if isinstance(rows, web.Response):
        return rows
    judged = await _judgement(request, window, rows)
    if isinstance(judged, web.Response):
        return judged
    judgement, judged_from = judged
    labels = None
    unavailable = None
    if judgement is not None:
        db = runtime.db if runtime.db is not None else get_db()
        if db is not None:
            labels = await asyncio.to_thread(labels_for, rows, judgement, Labels(db).effective)
        elif judged_from == ASKED:
            return web.json_response({"error": NO_LABELS}, status=503)
        else:
            unavailable = NO_LABELS
    summary = compare(rows, judgement=judgement, labels=labels)
    if "labels" in summary:
        summary["labels"]["from"] = judged_from
    policy = await asyncio.to_thread(_store().policy, window.name)
    verdict = (
        None
        if policy is None
        else judge_trial(summary, policy, since=window.since, now=window.until).to_dict()
    )
    return web.json_response(
        {
            "name": window.name,
            "shadow": None if window.shadow is None else asdict(window.shadow),
            "since": window.since,
            "until": window.until,
            "source": _source_asked(request),
            **summary,
            **({"labels_unavailable": unavailable} if unavailable else {}),
            "policy": verdict,
        }
    )


#: What ``labels.from`` says when the request named how to judge rather than a version.
ASKED = "request"

NO_LABELS = "persistence is not running, so no labels can be read"


def _source_asked(request: web.Request) -> str | None:
    """The label source the request names for every input, or None."""
    return (request.query.get("source") or "").strip() or None


async def _judgement(
    request: web.Request, window: _Window, rows: list[dict[str, Any]]
) -> tuple[Judgement | None, str] | web.Response:
    """How the trial is judged, and who said so: the request's ``positive``, else what a version declares.

    A ``source`` the request names replaces the declaration's, so a version can
    be judged against labels filed under another name without changing it.
    """
    positive = [v.strip() for v in (request.query.get("positive") or "").split(",") if v.strip()]
    source = _source_asked(request)
    if positive:
        try:
            return declared(positive=positive, source=source, by=ASKED), ASKED
        except JudgementError as exc:
            return web.json_response({"error": str(exc)}, status=400)
    shadow = window.shadow.version if window.shadow is not None else None
    found = await asyncio.to_thread(judgement_of_trial, _store(), window.name, rows, shadow=shadow)
    if found is None:
        return None, ""
    judgement, judged_from = found
    return (replace(judgement, source=source) if source else judgement), judged_from


async def trial_series_handler(request: web.Request) -> web.Response:
    """The trial's pairs over time, in ``points`` stretches, for a chart."""
    points = _points(request.query.get("points"))
    if points is None:
        return web.json_response(
            {"error": f"'points' is a count from 1 to {MAX_SERIES_POINTS}"}, status=400
        )
    window = await _trial_window(request)
    if isinstance(window, web.Response):
        return window
    rows = await _pairs(window)
    if isinstance(rows, web.Response):
        return rows
    since, until = event_span(rows) or (window.since, window.until)
    buckets = series(rows, since=since, until=until, points=points)
    return web.json_response(
        {"name": window.name, "since": window.since, "until": window.until, "buckets": buckets}
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


async def drift_handler(request: web.Request) -> web.Response:
    """The drift alarms raised about the model, newest first."""
    name = _name(request)
    if isinstance(name, web.Response):
        return name
    db = runtime.db if runtime.db is not None else get_db()
    if db is None:
        return web.json_response(
            {"error": "persistence is not running, so no drift alarms are recorded"}, status=503
        )
    alarms = await asyncio.to_thread(alarms_of, Provenance(db), name)
    return web.json_response({"name": name, "alarms": alarms})


def _moment(raw: str | None) -> float | bool | None:
    """``raw`` as Unix seconds; None when unsaid; False when it is not a time a date can hold."""
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        return False
    return value if math.isfinite(value) and 0 <= value <= LAST_SECOND else False


def _points(raw: str | None) -> int | None:
    """``points`` as a count of stretches, the default when unsaid; None when it is not one."""
    if raw is None or raw == "":
        return SERIES_POINTS
    try:
        points = int(raw)
    except ValueError:
        return None
    return points if 1 <= points <= MAX_SERIES_POINTS else None


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


async def set_policy_handler(request: web.Request) -> web.Response:
    """Say when the model's shadow may become active: the body's fields over what is set.

    ``{"metric": "f1", "margin": 0.02, "min_days": 1, "min_labelled": 100,
    "significant": true, "automatic": false}``, any of them; the rest keep what
    was set, else the default.
    """
    name = _name(request)
    if isinstance(name, web.Response):
        return name
    body = await _body(request)
    if isinstance(body, web.Response):
        return body
    store = _store()
    try:
        policy = policy_declared(body, over=await asyncio.to_thread(store.policy, name), by=BY)
        set_to = await asyncio.to_thread(store.set_policy, name, policy)
    except PolicyError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except ArtifactError as exc:
        return _refusal(exc)
    return web.json_response({"policy": set_to.to_json()})


async def clear_policy_handler(request: web.Request) -> web.Response:
    """Unset the model's policy; the answer says what it was, or null."""
    name = _name(request)
    if isinstance(name, web.Response):
        return name
    try:
        was = await asyncio.to_thread(_store().clear_policy, name)
    except ArtifactError as exc:
        return _refusal(exc)
    return web.json_response({"policy": None, "was": None if was is None else was.to_json()})
