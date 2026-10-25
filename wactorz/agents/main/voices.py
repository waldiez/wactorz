"""Which node may speak for an agent: the rule main checks a node's signature against.

A node signs what it publishes (see :mod:`wactorz.core.node_publications`), which
says which node sent a message and nothing about whether that node should be
sending it. This is the second half: main's own records say where each agent
is, and only that node may answer for it. A heartbeat is never what decides,
since a heartbeat is the node's own claim, and a node claiming another's agent
is the case this exists for.

Its own module because the node table and the manifest registry both need it,
and the node table already imports the registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...config import RESERVED_NODE_NAMES

if TYPE_CHECKING:
    from .hosts import VoiceHost


def placed_node(host: VoiceHost, agent_name: str) -> str:
    """The node main's spawn registry places ``agent_name`` on, or "" for none or for main."""
    entry = host._get_spawn_registry().get(agent_name) or {}
    placed = str(entry.get("node") or "").strip()
    return "" if placed.lower() in RESERVED_NODE_NAMES else placed


def speaks_for(host: VoiceHost, node_name: str, agent_name: str) -> bool:
    """Whether a message from ``node_name`` may speak for ``agent_name``.

    An agent in main's process, or one the spawn registry keeps on main, is
    spoken for by no node, and one it places on a node by that node alone. An
    agent being moved is spoken for by the node it is moving to as well, which
    starts it before the registry is changed. An agent main has no record of --
    a node started it itself -- is its node's, whichever signs for it.
    """
    if not node_name:
        return False
    if host.migration.moving_to(agent_name) == node_name:
        return True
    if host._registry is not None and host._registry.find_by_name(agent_name):
        return False
    entry = host._get_spawn_registry().get(agent_name)
    if entry is None:
        return True
    placed = placed_node(host, agent_name)
    return bool(placed) and placed == node_name
