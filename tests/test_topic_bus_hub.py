"""The shared-state hub and the bus that owns it.

`init_topic_bus` sets a module-level singleton, so every test here restores it;
a leaked bus would decide the outcome of anything that later calls
`get_topic_bus()`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from wactorz.core import topic_bus as tb
from wactorz.core.topic_bus import (
    SharedStateHub,
    TopicBus,
    TopicContract,
    get_topic_bus,
    init_topic_bus,
)


class FakeMQTT:
    """Records publishes instead of making them."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.published.append({"topic": topic, "payload": payload, "retain": retain, "qos": qos})


@pytest.fixture(name="restore_singleton", autouse=True)
def restore_singleton_fixture() -> Iterator[None]:
    previous = tb._topic_bus
    yield
    tb._topic_bus = previous


class TestSharedStateHub:
    async def test_publishing_caches_and_sends(self) -> None:
        mqtt = FakeMQTT()
        hub = SharedStateHub(mqtt)

        await hub.publish_state("home/state", {"a": 1})

        assert hub.get_cached("home/state") == {"a": 1}
        (call,) = mqtt.published
        assert call["topic"] == "home/state"
        assert json.loads(call["payload"]) == {"a": 1}

    async def test_state_is_retained_and_at_least_once_by_default(self) -> None:
        """A late subscriber needs the current value, so these are the defaults
        that make the topic a state topic rather than an event stream.
        """
        mqtt = FakeMQTT()
        await SharedStateHub(mqtt).publish_state("home/state", {"a": 1})
        assert mqtt.published[0]["retain"] is True
        assert mqtt.published[0]["qos"] == 1

    async def test_retain_can_be_turned_off(self) -> None:
        mqtt = FakeMQTT()
        await SharedStateHub(mqtt).publish_state("home/x", {"a": 1}, retain=False)
        assert mqtt.published[0]["retain"] is False

    async def test_a_string_payload_is_sent_unwrapped(self) -> None:
        mqtt = FakeMQTT()
        await SharedStateHub(mqtt).publish_state("home/x", "already text")
        assert mqtt.published[0]["payload"] == "already text"

    async def test_without_a_client_it_still_caches(self) -> None:
        """The cache is what local reads use, so it must not depend on a broker."""
        hub = SharedStateHub(None)
        await hub.publish_state("home/state", {"a": 1})
        assert hub.get_cached("home/state") == {"a": 1}

    def test_an_unknown_topic_caches_as_none(self) -> None:
        assert SharedStateHub(None).get_cached("never/published") is None

    async def test_presence_carries_the_zone_and_people(self) -> None:
        mqtt = FakeMQTT()
        await SharedStateHub(mqtt).publish_presence("kitchen", True, people=["sam"])

        body = json.loads(mqtt.published[0]["payload"])
        assert body["zone"] == "kitchen"
        assert body["present"] is True
        assert body["people"] == ["sam"]
        assert "ts" in body

    async def test_presence_defaults_to_nobody_named(self) -> None:
        mqtt = FakeMQTT()
        await SharedStateHub(mqtt).publish_presence("hall", False)
        assert json.loads(mqtt.published[0]["payload"])["people"] == []

    async def test_energy_carries_the_reading(self) -> None:
        mqtt = FakeMQTT()
        await SharedStateHub(mqtt).publish_energy(3.5, cost_per_hour=0.42, source="meter")

        body = json.loads(mqtt.published[0]["payload"])
        assert body["kwh"] == 3.5
        assert body["cost_per_hour"] == 0.42
        assert body["source"] == "meter"

    async def test_agent_data_is_namespaced_per_agent(self) -> None:
        mqtt = FakeMQTT()
        await SharedStateHub(mqtt).publish_agent_data("weather", "forecast", {"c": 21})
        assert "weather" in mqtt.published[0]["topic"]


class TestTopicBus:
    def test_registering_a_contract_reaches_the_registry(self) -> None:
        bus = TopicBus()
        bus.register_contract(TopicContract(name="a", publishes=["x/y"]))
        assert bus.registry.get("a") is not None

    def test_unregister_removes_it(self) -> None:
        bus = TopicBus()
        bus.register_contract(TopicContract(name="a", publishes=["x/y"]))
        bus.unregister("a")
        assert bus.registry.get("a") is None

    def test_registering_a_matching_pair_is_reported_not_wired(self) -> None:
        """The bus logs the opportunity; it does not connect the two agents.

        Nothing here subscribes on their behalf, so a pair showing up in the
        summary means "these could be wired", not "these are talking".
        """
        bus = TopicBus()
        bus.register_contract(TopicContract(name="producer", publishes=["sensors/temp"]))
        bus.register_contract(TopicContract(name="consumer", subscribes=["sensors/temp"]))
        assert bus.summary()["registry"]["wiring_pairs"] == 1

    def test_summary_carries_the_broker_it_would_use(self) -> None:
        bus = TopicBus(mqtt_broker="broker.local", mqtt_port=8883)
        summary = bus.summary()
        assert summary["mqtt_broker"] == "broker.local"
        assert summary["mqtt_port"] == 8883

    def test_planner_context_comes_from_the_registry(self) -> None:
        bus = TopicBus()
        bus.register_contract(TopicContract(name="thermo", publishes=["sensors/temp"]))
        assert "thermo" in bus.to_planner_context()


class TestTheSingleton:
    def test_it_is_unset_until_initialised(self) -> None:
        tb._topic_bus = None
        assert get_topic_bus() is None

    def test_init_installs_a_bus_that_get_returns(self) -> None:
        bus = init_topic_bus(mqtt_broker="b", mqtt_port=1)
        assert get_topic_bus() is bus

    def test_re_initialising_replaces(self) -> None:
        first = init_topic_bus()
        second = init_topic_bus()
        assert get_topic_bus() is second
        assert first is not second
