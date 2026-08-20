"""Learning what a remote agent actually publishes.

An agent declares a `produces_schema` when it announces itself, and that
declaration is written by whoever wrote the agent — often a model, often
wrong. This listener watches the traffic instead and records the field names
that really turn up, so the planner reasons about what is on the wire rather
than about what someone claimed would be.

It subscribes broadly and then filters, because it cannot know in advance which
topics a remote agent will use. Everything it skips is skipped for a reason:
internal wactorz topics are machinery rather than data, a payload that is not a
JSON object has no field names to learn, and a topic no contract claims belongs
to nothing it can record against.
"""

import asyncio
import json
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.nodes import NodeManager
from wactorz.core.actor import ActorState


class _Contract:
    """One agent's contract, which records what it was seen publishing."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.observed: list[tuple[str, dict[str, Any]]] = []

    def update_observed(self, topic: str, payload: dict[str, Any]) -> None:
        self.observed.append((topic, payload))


class _BusRegistry:
    def __init__(self, producers: dict[str, str], contracts: dict[str, _Contract]) -> None:
        self._producers = producers
        self._contracts = contracts
        #: Every name a recording was attempted against.
        self.lookups: list[str | None] = []

    def producers_of(self, topic: str) -> list[_Contract]:
        name = self._producers.get(topic)
        return [self._contracts[name]] if name and name in self._contracts else []

    def get(self, name: str) -> _Contract | None:
        self.lookups.append(name)
        return self._contracts.get(name)


class _Bus:
    def __init__(self, producers: dict[str, str] | None = None) -> None:
        self.contracts = {n: _Contract(n) for n in set((producers or {}).values())}
        self.registry = _BusRegistry(producers or {}, self.contracts)


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _Client:
    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self.subscribed: list[str] = []
        self._messages = messages
        self._on_drained = on_drained

    async def subscribe(self, pattern: str) -> None:
        self.subscribed.append(pattern)

    def __aiter__(self) -> "_Client":
        return self

    async def __anext__(self) -> _Message:
        if not self._messages:
            self._on_drained()
            raise StopAsyncIteration
        return self._messages.pop(0)

    @property
    def messages(self) -> "_Client":
        return self


class _Broker:
    """A stand-in for `mqtt_client`, which is an async context manager."""

    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self.client = _Client(messages, on_drained)
        self.connections = 0

    def __call__(self, _host: str, _port: int) -> "_Broker":
        self.connections += 1
        return self

    async def __aenter__(self) -> _Client:
        return self.client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def sample(topic: str, **fields: Any) -> _Message:
    return _Message(topic, json.dumps(fields).encode())


async def run_listener(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[_Message],
    *,
    producers: dict[str, str] | None = None,
) -> tuple[_Bus, _Broker]:
    """Drive the real listener over `messages` until they run out."""
    main = MainActor.__new__(MainActor)
    main.name = "main"
    main.manifests = ManifestRegistry(main)
    main.nodes = NodeManager(main, main.manifests)
    main.state = ActorState.RUNNING
    main._mqtt_broker = "localhost"
    main._mqtt_port = 1883

    def _stop() -> None:
        main.state = ActorState.STOPPED

    bus = _Bus(producers)
    broker = _Broker(messages, _stop)
    monkeypatch.setattr("wactorz.agents.main.manifests.mqtt_client", broker)
    monkeypatch.setattr("wactorz.core.topic_bus.get_topic_bus", lambda: bus)

    await asyncio.wait_for(main.manifests._remote_observed_samples_listener(), timeout=5)
    return bus, broker


class TestWhatItListensTo:
    """Subscribed broadly, because a remote agent picks its own topics."""

    async def test_it_subscribes_to_every_data_namespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, broker = await run_listener(monkeypatch, [])

        assert broker.client.subscribed == ["agents/+/data/#", "custom/#", "sensors/#"]


class TestRecordingWhatArrives:
    """A payload from a known producer updates that agent's contract."""

    async def test_a_sample_reaches_the_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bus, _ = await run_listener(
            monkeypatch,
            [sample("sensors/kitchen", degrees_c=21.5)],
            producers={"sensors/kitchen": "thermometer"},
        )

        assert bus.contracts["thermometer"].observed == [("sensors/kitchen", {"degrees_c": 21.5})]

    async def test_every_sample_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bus, _ = await run_listener(
            monkeypatch,
            [sample("sensors/kitchen", t=1), sample("sensors/kitchen", t=2)],
            producers={"sensors/kitchen": "thermometer"},
        )

        assert len(bus.contracts["thermometer"].observed) == 2


class TestWhatItSkips:
    """Each skip is a different kind of thing that is not a data sample."""

    @pytest.mark.parametrize(
        "topic", ["agents/by-name/weather/task", "nodes/rpi/spawn", "main/llm_request"]
    )
    async def test_internal_topics_are_not_samples(
        self, monkeypatch: pytest.MonkeyPatch, topic: str
    ) -> None:
        # Machinery rather than data: recording it would teach the planner that
        # an agent publishes spawn commands.
        bus, _ = await run_listener(
            monkeypatch, [sample(topic, x=1)], producers={topic: "thermometer"}
        )

        assert not bus.contracts["thermometer"].observed

    async def test_a_payload_that_is_not_json_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The subscription is a wildcard over topics an agent may use for
        # anything, so this is ordinary traffic rather than a fault.
        bus, _ = await run_listener(
            monkeypatch,
            [_Message("sensors/kitchen", b"21.5 degrees")],
            producers={"sensors/kitchen": "thermometer"},
        )

        assert not bus.contracts["thermometer"].observed

    async def test_a_payload_that_is_not_an_object_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Valid JSON, but a bare number has no field names to learn.
        bus, _ = await run_listener(
            monkeypatch,
            [_Message("sensors/kitchen", b"21.5")],
            producers={"sensors/kitchen": "thermometer"},
        )

        assert not bus.contracts["thermometer"].observed

    async def test_an_empty_payload_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bus, _ = await run_listener(
            monkeypatch,
            [_Message("sensors/kitchen", b"")],
            producers={"sensors/kitchen": "thermometer"},
        )

        assert not bus.contracts["thermometer"].observed

    async def test_a_topic_no_contract_claims_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Asserted on the attempt rather than the outcome: with a contract in
        # the bus for another agent, recording against nobody still writes
        # nothing, so only the lookup shows whether it was tried at all.
        bus, _ = await run_listener(
            monkeypatch,
            [sample("sensors/attic", x=1)],
            producers={"sensors/kitchen": "thermometer"},
        )

        assert not bus.registry.lookups
        assert not bus.contracts["thermometer"].observed


class TestStopping:
    """The loop follows the actor rather than running on after it."""

    async def test_it_stops_when_the_actor_does(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Draining the messages stops the actor; the listener must notice
        # rather than reconnecting forever.
        _, broker = await run_listener(monkeypatch, [sample("sensors/kitchen", x=1)])

        assert broker.connections == 1
