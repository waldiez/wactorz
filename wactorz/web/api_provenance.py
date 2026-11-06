"""The provenance graph main keeps, read back as it is.

``GET /api/provenance`` lists nodes, oldest recorded first, by ``kind``
(``observation``, ``question``, ``label``, ``decision``, ``action``, ``model``
or ``outcome``), ``source``, ``actor`` (``agent:drift``, ``operator``, ...),
``status`` (repeatable) and a window of event time (``since``, ``until``, Unix
seconds), up to ``limit``; with ``edges=1`` every edge into or out of a listed
node comes with them. ``GET /api/provenance/{id}`` is one node with its
edges, and, with ``walk=down`` or ``walk=up`` (``depth`` at most
`MAX_DEPTH`), everything that followed from it or that it followed from.

What a report of a run reads: the alarms, the decisions, the runs and the
moves, and which led to which, without reaching into main's database. Nodes
are written by the agents, the rules, the policy and the operator's answers,
never here; see docs/design/labels-and-provenance.md. The database blocks,
so every call goes through a thread rather than holding the event loop.
"""

import asyncio
import math
from typing import Any

from aiohttp import web

from ..core.persistence.stores import get_db
from ..core.provenance import KINDS, MAX_DEPTH, Provenance, ProvNode
from . import runtime

#: The most nodes one listing returns.
MAX_LIMIT = 5000

#: How many a listing returns when it does not say.
DEFAULT_LIMIT = 1000

#: The walks one node's route offers.
WALKS = ("down", "up")


class _BadQueryError(ValueError):
    """A query parameter that is not what the route takes."""

    @classmethod
    def not_seconds(cls, name: str) -> "_BadQueryError":
        return cls(f"'{name}' is Unix seconds")

    @classmethod
    def not_a_count(cls, name: str) -> "_BadQueryError":
        return cls(f"'{name}' is a whole number")

    @classmethod
    def ends_first(cls) -> "_BadQueryError":
        return cls("'until' comes after 'since'")

    @classmethod
    def unknown_kind(cls, kind: str) -> "_BadQueryError":
        return cls(f"'kind' is one of {', '.join(KINDS)}, not {kind!r}")

    @classmethod
    def unknown_walk(cls, walk: str) -> "_BadQueryError":
        return cls(f"'walk' is one of {', '.join(WALKS)}, not {walk!r}")


def _graph() -> Provenance | None:
    db = runtime.db if runtime.db is not None else get_db()
    return None if db is None else Provenance(db)


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


def _count(request: web.Request, name: str, default: int, most: int) -> int:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _BadQueryError.not_a_count(name) from None
    if value < 1:
        raise _BadQueryError.not_a_count(name)
    return min(value, most)


def _flag(request: web.Request, name: str) -> bool:
    return request.query.get(name) in ("1", "true", "yes")


def _unavailable() -> web.Response:
    return web.json_response(
        {"error": "persistence is not running, so there is no provenance"}, status=503
    )


async def nodes_handler(request: web.Request) -> web.Response:
    """``GET /api/provenance``: nodes by kind, source, actor, status and event-time window."""
    graph = _graph()
    if graph is None:
        return _unavailable()
    try:
        kind = request.query.get("kind") or None
        if kind is not None and kind not in KINDS:
            raise _BadQueryError.unknown_kind(kind)
        since, until = _window(request)
        limit = _count(request, "limit", DEFAULT_LIMIT, MAX_LIMIT)
    except _BadQueryError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    statuses = [s for s in request.query.getall("status", []) if s]
    found = await asyncio.to_thread(
        graph.nodes,
        kind,
        source=request.query.get("source") or None,
        actor=request.query.get("actor") or None,
        statuses=statuses or None,
        since=since,
        until=until,
        limit=limit,
    )
    body: dict[str, Any] = {"nodes": [node.to_dict() for node in found]}
    if _flag(request, "edges"):
        edges = await asyncio.to_thread(graph.edges_among, [node.id for node in found])
        body["edges"] = [edge.to_dict() for edge in edges]
    return web.json_response(body)


async def node_handler(request: web.Request) -> web.Response:
    """``GET /api/provenance/{id}``: one node, its edges, and a walk down or up from it when asked."""
    graph = _graph()
    if graph is None:
        return _unavailable()
    node_id = request.match_info["id"]
    try:
        walk = request.query.get("walk") or None
        if walk is not None and walk not in WALKS:
            raise _BadQueryError.unknown_walk(walk)
        depth = _count(request, "depth", MAX_DEPTH, MAX_DEPTH)
    except _BadQueryError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    node = await asyncio.to_thread(graph.get, node_id)
    if node is None:
        return web.json_response({"error": f"no node {node_id!r}"}, status=404)
    edges = await asyncio.to_thread(graph.edges, node_id)
    body: dict[str, Any] = {"node": node.to_dict(), "edges": [edge.to_dict() for edge in edges]}
    if walk is not None:
        walked: list[ProvNode] = await asyncio.to_thread(
            graph.descendants if walk == "down" else graph.ancestors, node_id, depth=depth
        )
        body["walk"] = walk
        body["nodes"] = [found.to_dict() for found in walked]
        among = await asyncio.to_thread(graph.edges_among, [node_id, *(n.id for n in walked)])
        body["walk_edges"] = [edge.to_dict() for edge in among]
    return web.json_response(body)
