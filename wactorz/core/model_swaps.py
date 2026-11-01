# SPDX-License-Identifier: Apache-2.0
"""A running agent changing the model version it serves, as an action in the provenance graph.

When a model's ``active`` or ``shadow`` pointer moves, every running agent whose
pin follows that pointer is re-pinned and reloaded in place (the hot swap, in
``docs/design/model-pointers.md``). Each swap that landed is an ``action`` node
here, about the agent that swapped, joined ``led_to`` from the move that caused
it (`wactorz.core.pointer_moves`) when that move is in the graph, so walking
down from a move finds every agent that followed it, and walking up from an
agent's later actions finds the version it was serving and why.

A swap that failed is not written: the agent kept what it had, and the feed
says so. Its id is minted from the swap's own token, so the same swap heard
twice over the broker is one node.

Calls block on SQLite; from the event loop, go through ``asyncio.to_thread``.
"""

from __future__ import annotations

from typing import Any

from .pointer_moves import source_of
from .provenance import ACTION, DONE, LED_TO, Provenance, ProvNode, stable_id

#: What the action did, in its payload's ``what``.
SWAP = "swap"


def swap_id(token: str) -> str:
    """The node id of the swap ``token`` names."""
    return stable_id("swap", token)


def move_id(name: str, pointer: str, version: int, moved_at: float) -> str:
    """The id of the move node `wactorz.core.pointer_moves.record_move` writes for that move."""
    return stable_id("pointer", name, pointer, version, moved_at)


def record_swap(
    prov: Provenance,
    *,
    token: str,
    agent: str,
    host: str,
    key: str,
    agent_id: str = "",
    name: str,
    pointer: str,
    from_version: int | None,
    to_version: int,
    sha256: str,
    by: str,
    at: float,
    moved_at: float | None = None,
) -> ProvNode:
    """Write that ``agent`` on ``host`` now serves ``name:to_version`` under ``key``.

    ``by`` is who moved the pointer, as the move records it. With ``moved_at``,
    the swap is joined from the move's node when the graph holds it; without,
    or when it does not, the swap stands alone.
    """
    node = prov.add_node(
        ACTION,
        ts=at,
        ts_end=at,
        status=DONE,
        source=source_of(name),
        actor=by,
        node=host,
        payload={
            "what": SWAP,
            "agent": agent,
            "agent_id": agent_id,
            "key": key,
            "model": name,
            "pointer": pointer,
            "from": from_version,
            "to": to_version,
            "sha256": sha256,
        },
        node_id=swap_id(token),
    )
    if moved_at is not None:
        cause = move_id(name, pointer, to_version, moved_at)
        if prov.get(cause) is not None:
            prov.add_edge(cause, node.id, LED_TO)
    return node


def is_swap(node: ProvNode) -> bool:
    """Whether ``node`` records a swap rather than a pointer's move."""
    return node.kind == ACTION and node.payload.get("what") == SWAP


def swaps_of(prov: Provenance, name: str) -> list[dict[str, Any]]:
    """Every swap of ``name`` the graph holds, newest first: which agent, where, to what."""
    nodes = [
        node for node in prov.nodes(ACTION, source=source_of(name), limit=None) if is_swap(node)
    ]
    newest_first = sorted(nodes, key=lambda node: (node.ts, node.seq), reverse=True)
    return [
        {
            "at": node.ts,
            "agent": node.payload.get("agent"),
            "host": node.node,
            "key": node.payload.get("key"),
            "pointer": node.payload.get("pointer"),
            "from": node.payload.get("from"),
            "to": node.payload.get("to"),
            "by": node.actor,
        }
        for node in newest_first
    ]
