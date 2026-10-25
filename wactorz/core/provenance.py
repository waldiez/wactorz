"""The model loop's record of what it saw, was told, decided and did.

One graph in main's database: nodes for observations, questions, labels,
decisions, actions, model versions and outcomes, joined by edges from cause to
effect. Walking down from a node finds everything that followed from it, which
is what an overridden answer needs; walking up explains why something is the
way it is. See docs/design/labels-and-provenance.md for what each kind and
relation means and why it is one graph.

Nodes are never deleted and never rewritten apart from their status: what
changed is itself recorded. A node written twice under the same id is written
once, so a writer that may repeat itself -- one replaying a retained message
after a restart -- mints a stable id with :func:`stable_id`.

The tables are SQLite's, through the one shared connection, so every call here
blocks; from the event loop, call through ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

    from .persistence.db import WactorzDB

OBSERVATION = "observation"
QUESTION = "question"
LABEL = "label"
DECISION = "decision"
ACTION = "action"
MODEL = "model"
OUTCOME = "outcome"

KINDS = (OBSERVATION, QUESTION, LABEL, DECISION, ACTION, MODEL, OUTCOME)

LED_TO = "led_to"
ANSWERED_BY = "answered_by"
TRAINED = "trained"
PRODUCED = "produced"
SUPERSEDED_BY = "superseded_by"
EVALUATED_BY = "evaluated_by"

RELATIONS = (LED_TO, ANSWERED_BY, TRAINED, PRODUCED, SUPERSEDED_BY, EVALUATED_BY)

#: The kinds each relation may join, cause first, where it is not any to any.
RELATION_KINDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    ANSWERED_BY: ((QUESTION,), (LABEL,)),
    SUPERSEDED_BY: ((LABEL,), (LABEL,)),
    TRAINED: ((LABEL,), (MODEL,)),
    PRODUCED: ((ACTION,), (MODEL,)),
    EVALUATED_BY: ((ACTION, DECISION, MODEL), (OUTCOME,)),
}

VALID = "valid"
PARTLY_SUPERSEDED = "partly_superseded"
SUPERSEDED = "superseded"
INVALIDATED = "invalidated"
PLANNED = "planned"
RUNNING = "running"
DONE = "done"
CANCELLED = "cancelled"
UNDONE = "undone"

#: The statuses a node of each kind may hold; its first is the one it starts with.
STATUSES: dict[str, tuple[str, ...]] = {
    LABEL: (VALID, PARTLY_SUPERSEDED, SUPERSEDED, INVALIDATED),
    ACTION: (PLANNED, RUNNING, DONE, CANCELLED, UNDONE, INVALIDATED),
}

#: The statuses of any kind :data:`STATUSES` does not name.
DEFAULT_STATUSES = (VALID, INVALIDATED)

#: How far a walk goes before it stops, so a graph that grew a cycle by mistake
#: still answers.
MAX_DEPTH = 64

#: The namespace stable ids are minted in.
_NAMESPACE = uuid.UUID("5d7f0c0e-3f7b-4f39-9a52-2a6f1c9d7b31")


class ProvenanceError(ValueError):
    """A node or edge the graph will not take, and why."""

    @classmethod
    def unknown_kind(cls, kind: str) -> ProvenanceError:
        return cls(f"unknown node kind {kind!r}; one of {', '.join(KINDS)}")

    @classmethod
    def unknown_relation(cls, relation: str) -> ProvenanceError:
        return cls(f"unknown relation {relation!r}; one of {', '.join(RELATIONS)}")

    @classmethod
    def bad_status(cls, kind: str, status: str) -> ProvenanceError:
        return cls(f"a {kind} cannot be {status!r}; one of {', '.join(statuses_of(kind))}")

    @classmethod
    def missing(cls, node_id: str) -> ProvenanceError:
        return cls(f"no node {node_id!r} in the graph")

    @classmethod
    def wrong_kinds(cls, relation: str, src: str, dst: str) -> ProvenanceError:
        return cls(f"{relation!r} does not join a {src} to a {dst}")

    @classmethod
    def self_edge(cls, node_id: str) -> ProvenanceError:
        return cls(f"a node cannot cause itself: {node_id!r}")


def statuses_of(kind: str) -> tuple[str, ...]:
    """The statuses a node of ``kind`` may hold, the starting one first."""
    return STATUSES.get(kind, DEFAULT_STATUSES)


def stable_id(*parts: object) -> str:
    """An id that is the same every time it is minted from the same ``parts``."""
    return uuid.uuid5(_NAMESPACE, "\x1f".join(str(p) for p in parts)).hex


def new_id() -> str:
    """A fresh id for a node written once."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class ProvNode:
    """One node of the graph."""

    id: str
    kind: str
    ts: float
    recorded: float
    status: str
    source: str = ""
    actor: str = ""
    node: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    #: Where the node's window ends; None for a window still open.
    ts_end: float | None = None
    #: The order it was written in, from the table itself: what breaks a tie
    #: between two nodes recorded at the same instant.
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "ts": self.ts,
            "ts_end": self.ts_end,
            "recorded": self.recorded,
            "status": self.status,
            "source": self.source,
            "actor": self.actor,
            "node": self.node,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ProvNode:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        ts_end = row["ts_end"]
        return cls(
            id=str(row["id"]),
            kind=str(row["kind"]),
            ts=float(row["ts"]),
            ts_end=None if ts_end is None else float(ts_end),
            seq=int(row["seq"] or 0),
            recorded=float(row["recorded"]),
            status=str(row["status"]),
            source=str(row["source"] or ""),
            actor=str(row["actor"] or ""),
            node=str(row["node"] or ""),
            payload=payload if isinstance(payload, dict) else {},
        )


@dataclass(frozen=True)
class ProvEdge:
    """One edge: ``src`` led to ``dst``, in the way ``relation`` says."""

    src: str
    dst: str
    relation: str
    recorded: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "src": self.src,
            "dst": self.dst,
            "relation": self.relation,
            "recorded": self.recorded,
        }


#: What a node is read with: its columns, and the table's own row order as ``seq``.
_NODE_COLUMNS = "id, kind, ts, ts_end, recorded, status, source, actor, node, payload, rowid AS seq"
_INSERT_COLUMNS = "id, kind, ts, ts_end, recorded, status, source, actor, node, payload"

_WALK_DOWN = f"""
WITH RECURSIVE walk(id, depth) AS (
    SELECT dst, 1 FROM prov_edges WHERE src = ?
    UNION
    SELECT e.dst, w.depth + 1 FROM prov_edges e JOIN walk w ON e.src = w.id
    WHERE w.depth < ?
)
SELECT {_NODE_COLUMNS} FROM prov_nodes
WHERE id IN (SELECT id FROM walk) AND id != ?
ORDER BY recorded ASC, seq ASC
"""  # noqa: S608  # the column list is a literal; every value is bound

_WALK_UP = f"""
WITH RECURSIVE walk(id, depth) AS (
    SELECT src, 1 FROM prov_edges WHERE dst = ?
    UNION
    SELECT e.src, w.depth + 1 FROM prov_edges e JOIN walk w ON e.dst = w.id
    WHERE w.depth < ?
)
SELECT {_NODE_COLUMNS} FROM prov_nodes
WHERE id IN (SELECT id FROM walk) AND id != ?
ORDER BY recorded ASC, seq ASC
"""  # noqa: S608  # the column list is a literal; every value is bound


class Provenance:
    """The graph in ``db``: add nodes and edges, change a status, walk it."""

    def __init__(self, db: WactorzDB) -> None:
        self.db = db

    # ── Writing ──────────────────────────────────────────────────────────────

    def add_node(
        self,
        kind: str,
        *,
        ts: float,
        ts_end: float | None = None,
        status: str | None = None,
        source: str = "",
        actor: str = "",
        node: str = "",
        payload: Mapping[str, Any] | None = None,
        node_id: str | None = None,
        recorded: float | None = None,
    ) -> ProvNode:
        """Write a node, or return the one already written under ``node_id``.

        ``status`` defaults to the first a ``kind`` may hold. ``recorded`` is
        when it was written, now unless said; ``ts`` the event time it is
        about, and ``ts_end`` where its window ends: None for a window still
        open, and ``ts`` itself for a node about one moment.
        """
        if kind not in KINDS:
            raise ProvenanceError.unknown_kind(kind)
        status = status or statuses_of(kind)[0]
        if status not in statuses_of(kind):
            raise ProvenanceError.bad_status(kind, status)
        candidate = ProvNode(
            id=node_id or new_id(),
            kind=kind,
            ts=float(ts),
            ts_end=None if ts_end is None else float(ts_end),
            recorded=time.time() if recorded is None else float(recorded),
            status=status,
            source=str(source),
            actor=str(actor),
            node=str(node),
            payload=dict(payload or {}),
        )
        with self.db.transaction() as conn:
            existing = _node(conn, candidate.id)
            if existing is not None:
                return existing
            cursor = conn.execute(
                f"INSERT INTO prov_nodes ({_INSERT_COLUMNS}) "  # noqa: S608  # the column list is a literal; every value is bound
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.id,
                    candidate.kind,
                    candidate.ts,
                    candidate.ts_end,
                    candidate.recorded,
                    candidate.status,
                    candidate.source,
                    candidate.actor,
                    candidate.node,
                    json.dumps(candidate.payload, default=str),
                ),
            )
            seq = int(cursor.lastrowid or 0)
        return replace(candidate, seq=seq)

    def add_edge(
        self, src: str, dst: str, relation: str, *, recorded: float | None = None
    ) -> ProvEdge:
        """Join ``src`` to ``dst``; an edge already there is left as it was."""
        if relation not in RELATIONS:
            raise ProvenanceError.unknown_relation(relation)
        if src == dst:
            raise ProvenanceError.self_edge(src)
        edge = ProvEdge(src, dst, relation, time.time() if recorded is None else float(recorded))
        with self.db.transaction() as conn:
            cause, effect = _node(conn, src), _node(conn, dst)
            if cause is None:
                raise ProvenanceError.missing(src)
            if effect is None:
                raise ProvenanceError.missing(dst)
            allowed = RELATION_KINDS.get(relation)
            if allowed is not None and (
                cause.kind not in allowed[0] or effect.kind not in allowed[1]
            ):
                raise ProvenanceError.wrong_kinds(relation, cause.kind, effect.kind)
            conn.execute(
                "INSERT OR IGNORE INTO prov_edges (src, dst, relation, recorded) VALUES (?, ?, ?, ?)",
                (edge.src, edge.dst, edge.relation, edge.recorded),
            )
        return edge

    def set_status(self, node_id: str, status: str) -> ProvNode:
        """Change a node's status, the one thing about a node that changes."""
        with self.db.transaction() as conn:
            node = _node(conn, node_id)
            if node is None:
                raise ProvenanceError.missing(node_id)
            if status not in statuses_of(node.kind):
                raise ProvenanceError.bad_status(node.kind, status)
            conn.execute("UPDATE prov_nodes SET status = ? WHERE id = ?", (status, node_id))
        return replace(node, status=status)

    # ── Reading ──────────────────────────────────────────────────────────────

    def get(self, node_id: str) -> ProvNode | None:
        with self.db.transaction() as conn:
            return _node(conn, node_id)

    def nodes(
        self,
        kind: str | None = None,
        *,
        source: str | None = None,
        statuses: Iterable[str] | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int | None = 1000,
    ) -> list[ProvNode]:
        """Nodes by kind, source and status, oldest recorded first.

        ``since`` and ``until`` keep the nodes whose window ``[ts, ts_end)``
        touches ``[since, until)``; a window with no end is open and reaches
        past any ``since``. ``limit`` None returns every one.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if until is not None:
            conditions.append("ts < ?")
            params.append(float(until))
        if since is not None:
            conditions.append("(ts_end IS NULL OR ts_end > ?)")
            params.append(float(since))
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)
        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        wanted = list(statuses or ())
        if wanted:
            conditions.append(f"status IN ({', '.join('?' for _ in wanted)})")
            params.extend(wanted)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_NODE_COLUMNS} FROM prov_nodes {where} "  # noqa: S608  # the fragments are literal; every value is bound
                "ORDER BY recorded ASC, seq ASC LIMIT ?",
                [*params, -1 if limit is None else int(limit)],
            ).fetchall()
        return [ProvNode.from_row(r) for r in rows]

    def edges(self, node_id: str) -> list[ProvEdge]:
        """Every edge into or out of ``node_id``."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT src, dst, relation, recorded FROM prov_edges "
                "WHERE src = ? OR dst = ? ORDER BY recorded ASC",
                (node_id, node_id),
            ).fetchall()
        return [ProvEdge(str(r[0]), str(r[1]), str(r[2]), float(r[3])) for r in rows]

    def descendants(self, node_id: str, *, depth: int = MAX_DEPTH) -> list[ProvNode]:
        """Everything that followed from ``node_id``, however far down, oldest first."""
        return self._walk(_WALK_DOWN, node_id, depth)

    def ancestors(self, node_id: str, *, depth: int = MAX_DEPTH) -> list[ProvNode]:
        """Everything ``node_id`` followed from, however far up, oldest first."""
        return self._walk(_WALK_UP, node_id, depth)

    def _walk(self, query: str, node_id: str, depth: int) -> list[ProvNode]:
        bounded = max(1, min(int(depth), MAX_DEPTH))
        with self.db.transaction() as conn:
            rows = conn.execute(query, (node_id, bounded, node_id)).fetchall()
        return [ProvNode.from_row(r) for r in rows]


def _node(conn: sqlite3.Connection, node_id: str) -> ProvNode | None:
    row = conn.execute(
        f"SELECT {_NODE_COLUMNS} FROM prov_nodes WHERE id = ?",  # noqa: S608  # the column list is a literal; the id is bound
        (node_id,),
    ).fetchone()
    return None if row is None else ProvNode.from_row(row)
