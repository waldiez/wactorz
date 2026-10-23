"""The models main keeps, and their pointers, for the dashboard.

``GET /api/models`` lists each name with its newest version and its pointers;
``GET /api/models/{name}`` gives every version, with what was said of it, and
the pointers. The rest move a pointer, as the CLI and the chat do, recorded as
made ``by`` the dashboard:

- ``POST /api/models/{name}/active`` and ``.../shadow``, ``{"version": 3}``;
- ``POST /api/models/{name}/promote``: active to where shadow is, shadow cleared;
- ``POST /api/models/{name}/rollback``: active back to the version it left;
- ``DELETE /api/models/{name}/shadow``: the trial ended.

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
from ..core.paths import resolve_state_dir

#: Who a move made here is recorded as made by.
BY = "dashboard"

#: The longest reason kept with a move. A reason is a sentence, not a document.
MAX_REASON_CHARS = 500


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
    """End the trial: unset ``shadow``."""
    name = _name(request)
    if isinstance(name, web.Response):
        return name
    return await _moved(_store().clear, "shadow", name)
