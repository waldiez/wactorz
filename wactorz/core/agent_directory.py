"""Where a node hears the other agents are, from their retained manifests.

Main resolves an address from its own records. A node has none, but every
agent leaves a manifest retained on ``agents/<id>/manifest``, and an agent on a
node of this release names its node there. From those a node resolves an
address the way main does, so an agent here that sends to a name other nodes
share is refused rather than sent to all of them.

A manifest that names no node does not say where its agent is: main's own
agents send none, and nor does an agent on a node from before nodes said so.
Such an agent is kept as one whose place is unknown, never taken for main's.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple

from .addresses import Address, Resolution, node_of, resolve

#: What a node subscribes to for the directory.
MANIFESTS = "agents/+/manifest"


class _Entry(NamedTuple):
    address: Address
    #: Whether its node takes a task on its own topic, as the manifest says.
    by_node: bool


class AgentDirectory:
    """The agents a node has heard of, by id, from their retained manifests."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def take(self, topic: str, payload: bytes) -> None:
        """Take one ``agents/<id>/manifest`` message: a manifest, or its withdrawal.

        An agent's manifest names its node; one that names none is main's own.
        A manifest this cannot read is ignored, as main ignores one.
        """
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "agents" or parts[2] != "manifest":
            return
        actor_id = parts[1]
        if not payload:
            self._entries.pop(actor_id, None)
            return
        try:
            manifest = json.loads(payload)
        except ValueError:
            return
        if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str):
            return
        self._entries[actor_id] = _Entry(
            Address(manifest["name"], _node_of(manifest)),
            manifest.get("tasks_by_node") is True,
        )

    def resolve(self, wanted: str) -> Resolution:
        """Which agents ``wanted``, a bare name or ``name@node``, finds among those whose node is known."""
        return resolve(wanted, [e.address for e in self._entries.values() if e.address.node])

    def unplaced(self, name: str) -> bool:
        """Whether an agent called ``name`` was heard of whose manifest names no node."""
        return any(e.address == Address(name) for e in self._entries.values())

    def takes_tasks_by_node(self, address: Address) -> bool:
        """Whether the agent at ``address`` said its node takes a task on its own topic."""
        return any(entry.by_node for entry in self._entries.values() if entry.address == address)

    def nodes_running(self, name: str) -> set[str]:
        """The nodes known to run an agent called ``name``."""
        return {
            entry.address.node
            for entry in self._entries.values()
            if entry.address.name == name and entry.address.node
        }


def _node_of(manifest: dict[str, Any]) -> str:
    """The node a manifest names, or "" for one that does not say."""
    node = manifest.get("node")
    return node_of(node) if isinstance(node, str) and node.strip() else ""
