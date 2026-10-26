"""The history of a model's pointers, as actions in the provenance graph.

The store's index keeps where each pointer is now and the version it left,
which is enough for one step back and nothing more: a history there would grow
with every retrain (docs/design/model-pointers.md). Each move is instead an
``action`` node of the graph in main's database (docs/design/labels-and-
provenance.md), written by main when it sees the change: who made it, why, from
which version to which. The models routes read it back as the history.

A move's node id is minted from the move itself -- the name, the pointer, the
version it reached and when -- so recording one again changes nothing. That is
what lets main record, when it starts, where each pointer stands: a move made
while it was away is written once, and one it already holds is passed over. A
clear made while it was away leaves nothing to find, and is not recorded.

Calls block on SQLite; from the event loop, go through ``asyncio.to_thread``.
"""

from __future__ import annotations

from typing import Any

from .artifacts import Pointer
from .provenance import ACTION, DONE, Provenance, ProvNode, stable_id

#: What the action did, in its payload's ``change``: a pointer set to a version,
#: or unset; ``promoted`` is the shadow cleared by a promotion.
MOVED = "moved"
CLEARED = "cleared"
PROMOTED = "promoted"

#: Most moves the history route returns when it is not told.
DEFAULT_LIMIT = 200


def source_of(name: str) -> str:
    """The graph's ``source`` for ``name``'s pointers, as its topics are named."""
    return f"models/{name}"


def record_move(prov: Provenance, now: Pointer) -> ProvNode:
    """Write that ``now.pointer`` was set to ``now.version``, as the store records the move."""
    return prov.add_node(
        ACTION,
        ts=now.moved_at,
        ts_end=now.moved_at,
        status=DONE,
        source=source_of(now.name),
        actor=now.by,
        payload={
            "change": MOVED,
            "pointer": now.pointer,
            "version": now.version,
            "from": now.previous,
            "sha256": now.sha256,
            "reason": now.reason,
        },
        node_id=stable_id("pointer", now.name, now.pointer, now.version, now.moved_at),
    )


def record_clear(
    prov: Provenance, was: Pointer, *, at: float, by: str = "", promoted: bool = False
) -> ProvNode:
    """Write that ``was.pointer`` was unset, by ``by`` where it is known.

    ``promoted`` says the shadow went because its version became active. The id
    names the pointer as it stood, so the same clear is written once: it cannot
    happen twice without a move between.
    """
    return prov.add_node(
        ACTION,
        ts=at,
        ts_end=at,
        status=DONE,
        source=source_of(was.name),
        actor=by,
        payload={
            "change": PROMOTED if promoted else CLEARED,
            "pointer": was.pointer,
            "version": None,
            "from": was.version,
        },
        node_id=stable_id("pointer-cleared", was.name, was.pointer, was.version, was.moved_at),
    )


def history(prov: Provenance, name: str, *, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """``name``'s pointer moves, newest first: what changed, when, by whom and why.

    By when each happened, not when main wrote it down: a move found at start
    is written late.
    """
    # The swaps that followed a move are about the model too (`model_swaps`),
    # and are not moves: a move says what it changed.
    nodes = [
        node
        for node in prov.nodes(ACTION, source=source_of(name), limit=None)
        if "change" in node.payload
    ]
    newest_first = sorted(nodes, key=lambda node: (node.ts, node.seq), reverse=True)
    return [_move(node) for node in newest_first[: max(0, limit)]]


def _move(node: ProvNode) -> dict[str, Any]:
    payload = node.payload
    return {
        "at": node.ts,
        "pointer": payload.get("pointer"),
        "change": payload.get("change"),
        "version": payload.get("version"),
        "from": payload.get("from"),
        "by": node.actor,
        "reason": payload.get("reason"),
    }
