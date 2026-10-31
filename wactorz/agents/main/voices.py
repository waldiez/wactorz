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

from .spawn_registry import placed_on

if TYPE_CHECKING:
    from .hosts import VoiceHost


def placed_node(host: VoiceHost, agent_name: str, actor_id: str = "") -> str:
    """The node main's spawn registry places the agent ``agent_name`` on, or "" for none or main.

    The agent ``actor_id`` is, when the registry keeps that id under that name
    -- what a message's topic says -- or else the only agent of the name; a
    name kept in several places, with no id to tell them apart, gives "".
    """
    registry = host._get_spawn_registry()
    entry = registry.by_id(actor_id) if actor_id else None
    if entry is None or entry["name"] != agent_name:
        entry = registry.only(agent_name)
    return placed_on(entry) if entry is not None else ""


def placed_nodes(host: VoiceHost, agent_name: str) -> list[str]:
    """Every node main's spawn registry places an agent called ``agent_name`` on; main is not one."""
    return [n for e in host._get_spawn_registry().named(agent_name) if (n := placed_on(e))]


def speaks_for(host: VoiceHost, node_name: str, agent_name: str) -> bool:
    """Whether a message from ``node_name`` may speak for its agent called ``agent_name``.

    A name is unique on a host, so this is a question about one agent: the
    one of that name on that node. The spawn registry placing an agent of the
    name there says it may. An agent being moved is spoken for by the node it
    is moving to as well, which starts it before the registry is changed. An
    agent main has no record of anywhere -- a node started it itself -- is its
    node's, whichever signs for it. Otherwise the registry keeps the name
    somewhere else, or main runs it in its own process, and that node may not.
    """
    if not node_name:
        return False
    if host.migration.moving_to(agent_name) == node_name:
        return True
    registry = host._get_spawn_registry()
    if registry.at(agent_name, node_name) is not None:
        return True
    if host._registry is not None and host._registry.find_by_name(agent_name):
        return False
    return not registry.named(agent_name)
