"""The history of a model's pointers, as actions in the provenance graph.

The store's index keeps where each pointer is now and the version it left,
which is enough for one step back and nothing more: a history there would grow
with every retrain (docs/design/model-pointers.md). Each move is instead an
``action`` node of the graph in main's database (docs/design/labels-and-
provenance.md), written by main from the store's journal of changes, which says
who made each and why whichever process made it (`ArtifactStore.pointer_changes`),
and from what main sees changed in the index. The models routes read it back as
the history.

A move's node id is minted from the move itself -- the name, the pointer, the
version it reached and when -- so recording one again changes nothing, and the
first record written stands. Main records the journal before what it sees in
the index, so a change is kept with its ``by`` and reason where the journal has
them; what it sees in the index covers a journal line lost to a crash between
the two writes.

Calls block on SQLite; from the event loop, go through ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .artifacts import CLEARED, MOVED, PROMOTED, Pointer
from .provenance import ACTION, DONE, Provenance, ProvNode, stable_id

logger = logging.getLogger(__name__)

#: What the action did, in its payload's ``change`` -- `MOVED`, `CLEARED` or
#: `PROMOTED`, the shadow cleared by a promotion -- in the words the store's
#: journal names its changes with.
CHANGES = (MOVED, CLEARED, PROMOTED)

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
    prov: Provenance,
    was: Pointer,
    *,
    at: float,
    by: str = "",
    reason: str | None = None,
    promoted: bool = False,
) -> ProvNode:
    """Write that ``was.pointer`` was unset, by ``by`` where it is known, and why.

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
            "reason": reason,
        },
        node_id=stable_id("pointer-cleared", was.name, was.pointer, was.version, was.moved_at),
    )


def record_change(prov: Provenance, change: Mapping[str, Any]) -> ProvNode | None:
    """Write one change of the store's journal; None for a line that is not one, which is logged."""
    try:
        pointer = Pointer(**dict(change["pointer"]))
        kind = change["change"]
        at = float(change["at"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("a pointer change in the journal is not one, passed over: %s", exc)
        return None
    if kind == MOVED:
        return record_move(prov, pointer)
    if kind not in CHANGES:
        logger.warning("a pointer change in the journal is not one, passed over: %r", kind)
        return None
    reason = change.get("reason")
    return record_clear(
        prov,
        pointer,
        at=at,
        by=str(change.get("by") or ""),
        reason=reason if isinstance(reason, str) else None,
        promoted=kind == PROMOTED,
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
