"""A factory reset deletes the agents on nodes and keeps the nodes.

A reset clears what was built on the install. A node is the machine it runs
on, and redeploying it is a trip to that machine, so the reset tells each node
to delete its agents one by one and leaves the node running and listed.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_reset import _make_request
from wactorz.web import api_reset, runtime


class _Broker:
    """Stands in for the server's broker client: records what is published."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, payload: str, **_kwargs: Any) -> None:
        self.sent.append((topic, json.loads(payload)))

    def to(self, leaf: str) -> list[tuple[str, dict[str, Any]]]:
        return [(t, p) for t, p in self.sent if t.endswith(f"/{leaf}")]


class _Main:
    """Main as the reset reads it: a spawn registry naming where each agent runs."""

    def __init__(self, registry: dict[str, dict[str, Any]]) -> None:
        self._registry = registry

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return self._registry


@pytest.fixture(name="broker")
def broker_fixture(monkeypatch: pytest.MonkeyPatch) -> _Broker:
    """A reset run against a recording broker, with the server's state isolated.

    Everything the reset changes is put back afterwards, the agents it marks
    deleted included: a test after this one that beats as one of them would
    otherwise find its heartbeat ignored.
    """
    for key in ("agents", "nodes", "alerts", "log_feed"):
        monkeypatch.setitem(runtime.state, key, type(runtime.state[key])())
    monkeypatch.setattr(runtime, "deleted_agent_ids", [])
    broker = _Broker()
    monkeypatch.setattr(runtime, "mqtt_client_ref", broker)
    registry = MagicMock()
    registry.all_actors.return_value = []
    monkeypatch.setattr(runtime, "registry", registry)
    return broker


def _node(name: str, *agents: str) -> None:
    runtime.state["nodes"][name] = {"node": name, "agents": list(agents), "last_seen": 1.0}


async def _reset(main: _Main | None = None) -> None:
    with (
        patch.object(api_reset, "find_main_actor", lambda _registry: main),
        patch("wactorz.web.ws.broadcast", new=AsyncMock()),
        patch("wactorz.reset.reset_all"),
        patch("wactorz.web.lifecycle.purge_agent_retained", new=AsyncMock()),
        patch("wactorz.web.lifecycle.purge_spawn_reconcile", new=AsyncMock()),
    ):
        resp = await api_reset.reset_handler(_make_request({"scope": "all"}))
    assert resp.status == 200


async def test_no_node_is_told_to_shut_down(broker: _Broker) -> None:
    _node("edge", "counter")

    await _reset()

    assert broker.to("stop_all") == []


async def test_each_agent_on_a_node_is_deleted_there(broker: _Broker) -> None:
    _node("edge", "counter", "asker")

    await _reset()

    assert sorted(broker.to("stop"), key=str) == [
        ("nodes/edge/stop", {"name": "asker", "delete": True}),
        ("nodes/edge/stop", {"name": "counter", "delete": True}),
    ]


async def test_a_node_that_is_away_is_told_from_the_spawn_registry(broker: _Broker) -> None:
    # Nothing heard from it, so only main knows it runs anything.
    main = _Main({"counter": {"node": "pi"}, "here": {"node": ""}})

    await _reset(main)

    assert broker.to("stop") == [("nodes/pi/stop", {"name": "counter", "delete": True})]


async def test_an_agent_only_the_dashboard_placed_is_deleted_too(broker: _Broker) -> None:
    runtime.state["agents"]["a1"] = {"name": "watcher", "node": "edge"}

    await _reset()

    assert broker.to("stop") == [("nodes/edge/stop", {"name": "watcher", "delete": True})]


async def test_the_node_stays_listed_running_nothing(broker: _Broker) -> None:
    _node("edge", "counter")

    await _reset()

    assert runtime.state["nodes"]["edge"]["agents"] == []
    assert runtime.state["nodes"]["edge"]["node"] == "edge"


async def test_without_a_broker_connection_the_reset_still_completes(
    broker: _Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "mqtt_client_ref", None)
    _node("edge", "counter")

    await _reset()

    assert broker.sent == []
    assert runtime.state["nodes"]["edge"]["agents"] == []
