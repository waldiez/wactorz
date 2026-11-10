# SPDX-License-Identifier: Apache-2.0
"""An agent program writes Wactorz's topics bare, and `AgentAPI` puts them under the namespace.

Generated code is taught ``agents/{id}/answer`` and ``agents/{name}/data/{key}``,
and a person's program writes what it likes. Every way a program names a topic
-- publish, subscribe, a one-shot read, a window, a contract, the contract it
was spawned with -- takes it as it travels (`wire_topic`), before it is
deduplicated or recorded; what is handed back to the program is shown bare. Code
that opens its own broker connection is not covered.

The namespace is set by patching it here: `wire_topic` reads it on each call.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic import streams
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.dynamic.api import AgentAPI
from wactorz.core import topic_bus, topics
from wactorz.core.topic_bus import StreamWindow, TopicBus, TopicContract


@pytest.fixture(autouse=True)
def namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topics, "NAMESPACE", "lab")


@pytest.fixture(name="bus")
def bus_fixture(monkeypatch: pytest.MonkeyPatch) -> TopicBus:
    bus = TopicBus()
    monkeypatch.setattr(topic_bus, "_topic_bus", bus)
    return bus


class _Hub:
    """Stands in for the subscription hub: records what is bound, connects to nothing."""

    def __init__(self) -> None:
        self.bound: list[str] = []

    def bind(self, topic: str, _callback: Any) -> None:
        self.bound.append(topic)


@pytest.fixture(name="api")
def api_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentAPI:
    actor = DynamicAgent(name="probe", code="", persistence_dir=str(tmp_path))
    sent: list[str] = []

    async def _publish(topic: str, _data: Any, **_kwargs: Any) -> None:
        sent.append(topic)

    async def _manifest(_manifest: Any) -> None:
        return None

    monkeypatch.setattr(actor, "_mqtt_publish", _publish)
    monkeypatch.setattr(actor, "_send_manifest", _manifest)
    actor.sent = sent  # pyright: ignore[reportAttributeAccessIssue]
    hub = _Hub()
    monkeypatch.setattr(streams, "hub_for", lambda _actor: hub)
    actor.hub = hub  # pyright: ignore[reportAttributeAccessIssue]
    return AgentAPI(actor)


async def _ignore(_payload: Any) -> None:
    return None


class TestPublishing:
    async def test_a_bare_own_topic_goes_under_the_namespace(
        self, api: AgentAPI, bus: TopicBus
    ) -> None:
        await api.publish("agents/a1/data/level", {"v": 1})
        await api.publish("sensors/air", {"v": 1})

        assert api._actor.sent == ["lab/agents/a1/data/level", "sensors/air"]  # pyright: ignore[reportAttributeAccessIssue]

    async def test_bare_and_namespaced_are_one_topic_in_the_contract(
        self, api: AgentAPI, bus: TopicBus
    ) -> None:
        await api.publish("agents/a1/data/level", {"v": 1})
        await api.publish("lab/agents/a1/data/level", {"v": 2})

        contract = bus.registry.get(api.name, api.actor_id, None)
        assert contract is not None
        assert contract.publishes == ["lab/agents/a1/data/level"]
        assert api._published_topics == {"lab/agents/a1/data/level"}


class TestReading:
    def test_a_subscription_is_bound_once_under_the_namespace(
        self, api: AgentAPI, bus: TopicBus
    ) -> None:
        api.subscribe("agents/a1/answer", _ignore)
        api.subscribe("lab/agents/a1/answer", _ignore)

        assert api._actor.hub.bound == ["lab/agents/a1/answer"]  # pyright: ignore[reportAttributeAccessIssue]
        contract = bus.registry.get(api.name, api.actor_id, None)
        assert contract is not None and contract.subscribes == ["lab/agents/a1/answer"]

    def test_a_window_is_over_the_namespaced_topic(
        self, api: AgentAPI, bus: TopicBus, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(StreamWindow, "start", lambda *_args: None)

        api.window("agents/a1/data/level", seconds=60)
        again = api.window("lab/agents/a1/data/level", seconds=60)

        assert list(api._windows) == ["lab/agents/a1/data/level"]
        assert again is api._windows["lab/agents/a1/data/level"]

    async def test_a_one_shot_read_subscribes_under_the_namespace(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        subscribed: list[str] = []

        class _Client:
            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def subscribe(self, topic: str, **_kw: Any) -> None:
                subscribed.append(topic)

            @property
            def messages(self) -> Any:
                return self._none()

            async def _none(self) -> Any:
                return
                yield

        monkeypatch.setattr(streams, "mqtt_client", lambda *_a, **_kw: _Client())

        await api.mqtt_get("agents/a1/data/level", timeout=0.5)

        assert subscribed == ["lab/agents/a1/data/level"]


class TestContracts:
    async def test_a_declared_contract_holds_the_namespaced_topics(
        self, api: AgentAPI, bus: TopicBus
    ) -> None:
        api.declare_contract(
            publishes="agents/a1/data/level", subscribes=["agents/+/answer", "sensors/#"]
        )

        contract = api._actor._topic_contract
        assert contract.publishes == ["lab/agents/a1/data/level"]
        assert contract.subscribes == ["lab/agents/+/answer", "sensors/#"]

    def test_the_contract_an_agent_is_spawned_with_does_too(self) -> None:
        contract = TopicContract.from_spawn_config(
            {"name": "x", "publishes": ["agents/x/data/y"], "subscribes": ["models/air/drift"]}
        )

        assert contract.publishes == ["lab/agents/x/data/y"]
        assert contract.subscribes == ["lab/models/air/drift"]

    def test_what_is_handed_back_to_the_program_is_bare(self, api: AgentAPI, bus: TopicBus) -> None:
        bus.register_contract(
            TopicContract(name="maker", publishes=["lab/agents/m/data/level"], actor_id="m")
        )
        bus.register_contract(
            TopicContract(
                name=api.name, subscribes=["lab/agents/m/data/level"], actor_id=api.actor_id
            )
        )

        assert api.wiring_opportunities() == [
            {"producer": "maker", "consumer": api.name, "topic": "agents/m/data/level"}
        ]
