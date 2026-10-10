"""A spawn withdrawn while its node is away does not start when the node returns.

A spawn published to an offline node waits in the node's broker session and is
delivered on its return, whatever main has decided since. A stop published after
it waits behind it and undoes it. Delete and a rolled-back migration already send
one; these pin the two withdrawals that did not: clearing spawns in a reset, and
main giving up on a node that has been silent too long.
"""

import time
from typing import Any, cast

import pytest

from wactorz.agents.main.hosts import NodeHost
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.nodes import OFFLINE_GRACE_S, NodeManager
from wactorz.web import lifecycle, runtime

# ── Main gives up on a silent node ─────────────────────────────────────────────


class _NodeHost:
    """Records, in order, what forgetting a node removes and publishes."""

    name = "main"

    def __init__(self, registry: dict[str, dict[str, Any]]) -> None:
        self.registry = dict(registry)
        self.events: list[tuple[Any, ...]] = []

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return self.registry

    def _remove_from_spawn_registry(self, name: str) -> None:
        self.registry.pop(name, None)
        self.events.append(("unregistered", name))

    async def _update_node_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        self.events.append(("desired_state", node, remove_name))

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.events.append(("publish", topic, payload, qos))

    async def _clear_agent_manifest(self, name: str, actor_id: str | None = None) -> None:
        self.events.append(("manifest", name))

    def _record_agent_deletion(self, name: str, reason: str = "") -> None:
        self.events.append(("deleted", name))

    def _queue_notification(self, notice: dict[str, Any]) -> None:
        self.events.append(("notice", notice["message"]))


def _nodes(host: _NodeHost) -> NodeManager:
    return NodeManager(cast("NodeHost", host), cast("ManifestRegistry", object()))


class TestANodeThatWentSilent:
    async def test_each_forgotten_agent_gets_a_stop_queued_on_its_node(self) -> None:
        host = _NodeHost(
            {
                "collector": {"node": "rpi"},
                "fan": {"node": "rpi"},
                "local": {},
                "elsewhere": {"node": "nuc"},
            }
        )
        nodes = _nodes(host)
        now = time.time()
        nodes.known = {"rpi": {"last_seen": now - OFFLINE_GRACE_S - 1}, "nuc": {"last_seen": now}}

        await nodes.forget_offline_nodes(now)

        assert [event for event in host.events if event[0] == "publish"] == [
            ("publish", "nodes/rpi/stop", {"name": "collector"}, 1),
            ("publish", "nodes/rpi/stop", {"name": "fan"}, 1),
        ]
        assert set(nodes.known) == {"nuc"}

    async def test_the_stop_follows_the_desired_state_that_drops_the_agent(self) -> None:
        # The same order a delete uses: what the node should run is settled first.
        host = _NodeHost({"collector": {"node": "rpi"}})
        nodes = _nodes(host)
        now = time.time()
        nodes.known = {"rpi": {"last_seen": now - OFFLINE_GRACE_S - 1}}

        await nodes.forget_offline_nodes(now)

        order = [event[0] for event in host.events if event[0] in ("desired_state", "publish")]
        assert order == ["desired_state", "publish"]

    async def test_a_node_heard_from_within_the_grace_keeps_its_agents(self) -> None:
        host = _NodeHost({"collector": {"node": "rpi"}})
        nodes = _nodes(host)
        now = time.time()
        nodes.known = {"rpi": {"last_seen": now - OFFLINE_GRACE_S + 1}}

        await nodes.forget_offline_nodes(now)

        assert host.events == []
        assert "rpi" in nodes.known


# ── A reset clears the spawns ──────────────────────────────────────────────────


class _Nodes:
    def __init__(self, online: set[str]) -> None:
        self.online = online

    def is_online(self, name: str) -> bool:
        return name in self.online


class _Main:
    """The surface `purge_spawn_reconcile` reaches on the main actor."""

    def __init__(self, registry: dict[str, dict[str, Any]], online: set[str]) -> None:
        self.registry = dict(registry)
        self.nodes = _Nodes(online)
        self.published: list[tuple[str, Any, int]] = []
        self.persisted: dict[str, Any] = {}

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return self.registry

    def recall(self, key: str, default: Any = None) -> Any:
        return self.registry

    def persist(self, key: str, value: Any) -> None:
        self.persisted[key] = value

    async def _update_node_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        return None

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload, qos))


REGISTRY = {
    "collector": {"node": "rpi"},
    "fan": {"node": "rpi"},
    "lamp": {"node": "nuc"},
    "local": {},
}


@pytest.fixture(autouse=True)
def _no_live_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(runtime.state, "nodes", {})
    monkeypatch.setattr(runtime, "mqtt_client_ref", None)


def _use(monkeypatch: pytest.MonkeyPatch, main: _Main | None) -> None:
    monkeypatch.setattr(lifecycle, "find_main_actor", lambda _registry: main)


class TestClearingSpawns:
    async def test_every_withdrawn_agent_on_an_offline_node_is_stopped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = _Main(REGISTRY, online={"nuc"})
        _use(monkeypatch, main)

        await lifecycle.purge_spawn_reconcile(None)

        assert sorted(main.published, key=lambda sent: sent[1]["name"]) == [
            ("nodes/rpi/stop", {"name": "collector"}, 1),
            ("nodes/rpi/stop", {"name": "fan"}, 1),
        ]

    async def test_one_withdrawn_agent_stops_only_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = _Main(REGISTRY, online=set())
        _use(monkeypatch, main)

        await lifecycle.purge_spawn_reconcile("collector")

        assert main.published == [("nodes/rpi/stop", {"name": "collector"}, 1)]

    async def test_an_agent_on_an_online_node_is_left_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing is queued for a node that is connected, and this reset does not
        # stop the agents it forgets anywhere else.
        main = _Main(REGISTRY, online={"nuc"})
        _use(monkeypatch, main)

        await lifecycle.purge_spawn_reconcile("lamp")

        assert main.published == []

    async def test_without_a_main_actor_nothing_is_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use(monkeypatch, None)

        await lifecycle.purge_spawn_reconcile(None)  # must not raise
