"""Deleting a remote agent must stop it on its node even when the spawn registry
no longer records the node.

Regression: if the registry entry is missing (already pruned) the node lookup
returned empty, delete fell to the local-only branch, and the agent kept running
on its node — heartbeating back and getting re-admitted (a resurrection loop).
The node is now resolved from live heartbeat telemetry as a fallback, mirroring
``migrate_agent``.
"""

# pylint: disable=missing-function-docstring

import time
import types
from unittest.mock import AsyncMock, Mock

from wactorz.agents.main_actor import MainActor


def _bare_main() -> MainActor:
    """A MainActor with just the surface delete_spawned_agent touches, stubbed."""
    m = MainActor.__new__(MainActor)
    m.name = "main"
    m.recall = lambda *a, **k: {}  # empty spawn registry (no node recorded)
    m.persist = lambda *a, **k: None
    m._registry = types.SimpleNamespace(find_by_name=lambda n: None)  # pyright: ignore[reportAttributeAccessIssue]
    m._known_nodes = {}
    m._update_node_desired_state = AsyncMock()
    m._mqtt_publish = AsyncMock()
    m._clear_agent_manifest = AsyncMock()
    m._purge_agent_retained_topics = AsyncMock()
    m._record_agent_deletion = Mock()
    return m


async def test_delete_stops_remote_agent_via_heartbeat_node() -> None:
    m = _bare_main()
    # Registry has no record of the agent, but a live node heartbeat lists it.
    m._known_nodes = {"rpi-test": {"last_seen": time.time(), "agents": ["system-monitor"]}}

    await m.delete_spawned_agent("system-monitor")

    # Delete must reach the node: clear its desired state and send a delete-stop.
    m._update_node_desired_state.assert_awaited_once_with("rpi-test", remove_name="system-monitor")  # pyright: ignore[reportAttributeAccessIssue]
    m._mqtt_publish.assert_awaited_once()  # pyright: ignore[reportAttributeAccessIssue]
    topic, payload = m._mqtt_publish.await_args.args[0], m._mqtt_publish.await_args.args[1]  # pyright: ignore[reportAttributeAccessIssue]
    assert topic == "nodes/rpi-test/stop"
    assert payload == {"name": "system-monitor", "delete": True}


async def test_stale_node_heartbeat_does_not_route_remote() -> None:
    m = _bare_main()
    # Heartbeat older than the freshness window — not a trustworthy location.
    m._known_nodes = {"rpi-test": {"last_seen": time.time() - 60, "agents": ["system-monitor"]}}

    await m.delete_spawned_agent("system-monitor")

    m._mqtt_publish.assert_not_called()  # no remote stop; local cleanup only  # pyright: ignore[reportAttributeAccessIssue]
