"""migrate_agent refuses targets that don't exist or are offline.

Migrating to a node nobody has heard of (typo, powered-off Pi, runner never
deployed) used to be accepted blindly: the source stopped the agent — deleting
its state file — and published the hand-off to a node topic nobody was
listening on, so the agent simply vanished. The target node must now be online
(heartbeat within the last 30s) before anything destructive happens; otherwise
the migration is refused and the agent stays where it is.
"""

import time
from typing import Any

from wactorz.agents.main_actor import MainActor
from wactorz.agents.manifests import ManifestRegistry
from wactorz.agents.migration import Migration
from wactorz.agents.nodes import NodeManager


def _bare_main(registry: dict, known_nodes: dict[str, Any]) -> MainActor:
    """A MainActor with only what migrate_agent touches, stubbed.

    Records every MQTT publish in ``m.published`` so tests can assert that a
    refused migration never sends the destructive migrate/spawn commands.
    """
    published: list[tuple[str, Any]] = []
    m = MainActor()
    m.manifests = ManifestRegistry(m)
    m.nodes = NodeManager(m, m.manifests)
    m.migration = Migration(m, m.nodes)
    m.name = "main"
    m._known_nodes = known_nodes
    m._agent_manifests = {}
    m._registry = None
    m.recall = lambda *a, **k: registry
    m.persist = lambda *a, **k: None
    m.published = published  # pyright: ignore[reportAttributeAccessIssue]

    async def _publish(topic: str, payload: Any, **kwargs) -> None:
        m.published.append((topic, payload))  # pyright: ignore[reportAttributeAccessIssue]

    async def _update_desired_state(node, config=None, remove_name=None) -> None:
        pass

    m._mqtt_publish = _publish  # pyright: ignore[reportAttributeAccessIssue]
    m._update_node_desired_state = _update_desired_state  # pyright: ignore[reportAttributeAccessIssue]
    return m


def _registry_with_agent_on(node: str) -> dict:
    from wactorz.agents.helpers.main_actor_helpers import SPAWN_REGISTRY_KEY  # noqa: F401

    return {"temp-sensor": {"name": "temp-sensor", "node": node, "type": "dynamic", "code": "x"}}


async def test_migrate_to_unknown_node_is_refused():
    m = _bare_main(_registry_with_agent_on("rpi-a"), {"rpi-a": {"last_seen": time.time()}})
    result = await m.migrate_agent("temp-sensor", "rpi-doesnotexist")
    assert result["success"] is False
    assert "does not exist" in result["message"]
    assert m.published == []  # pyright: ignore[reportAttributeAccessIssue] # nothing destructive was sent


async def test_migrate_to_offline_node_is_refused():
    m = _bare_main(
        _registry_with_agent_on("rpi-a"),
        {
            "rpi-a": {"last_seen": time.time()},
            "rpi-b": {"last_seen": time.time() - 300},  # stale heartbeat
        },
    )
    result = await m.migrate_agent("temp-sensor", "rpi-b")
    assert result["success"] is False
    assert "offline" in result["message"]
    assert m.published == []  # pyright: ignore[reportAttributeAccessIssue]


async def test_refusal_lists_online_nodes():
    m = _bare_main(
        _registry_with_agent_on("rpi-a"),
        {
            "rpi-a": {"last_seen": time.time()},
            "rpi-b": {"last_seen": time.time()},
        },
    )
    result = await m.migrate_agent("temp-sensor", "rpi-typo")
    assert result["success"] is False
    assert "rpi-a" in result["message"]
    assert "rpi-b" in result["message"]


async def test_migrate_to_online_node_proceeds():
    m = _bare_main(
        _registry_with_agent_on("rpi-a"),
        {
            "rpi-a": {"last_seen": time.time()},
            "rpi-b": {"last_seen": time.time()},
        },
    )
    result = await m.migrate_agent("temp-sensor", "rpi-b")
    assert result["success"] is True
    # The migrate command reached the source node
    assert any(topic == "nodes/rpi-a/migrate" for topic, _ in m.published)  # pyright: ignore[reportAttributeAccessIssue]


async def test_migrate_back_to_local_needs_no_node_check():
    # "local" is always a valid target — the check only applies to remote nodes.
    m = _bare_main(_registry_with_agent_on("rpi-a"), {"rpi-a": {"last_seen": time.time()}})
    result = await m.migrate_agent("temp-sensor", "local")
    assert result["success"] is True
    assert any(topic == "nodes/rpi-a/migrate" for topic, _ in m.published)  # pyright: ignore[reportAttributeAccessIssue]


def test_node_is_online_window():
    m = _bare_main(
        {}, {"fresh": {"last_seen": time.time()}, "stale": {"last_seen": time.time() - 31}}
    )
    assert m._node_is_online("fresh") is True
    assert m._node_is_online("stale") is False
    assert m._node_is_online("never-seen") is False
    assert m._online_node_names() == ["fresh"]
