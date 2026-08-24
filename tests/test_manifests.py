"""What main does with the agent manifests that arrive over MQTT.

A remote agent lives in another process, so its published and subscribed topics
reach main only as a retained message on ``agents/+/manifest``. This listener is
the only channel that carries them, and it writes to three places at once: the
manifest table capability queries read, the topic registry, and the TopicBus the
planner auto-wires from. An empty payload is a tombstone and has to clear all
three, or a deleted agent stays visible forever.

The listener is driven here through a fake broker rather than a real one: it is
an MQTT loop, and the behaviour worth pinning is what one message does to those
three tables, not how the connection is made.
"""

import asyncio
import json
import logging
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.nodes import NodeManager
from wactorz.core.actor import ActorState


class _Message:
    """One MQTT message. A payload of ``b""`` is the deletion tombstone."""

    def __init__(self, topic: str, payload: bytes | None) -> None:
        self.topic = topic
        self.payload = payload


class _Client:
    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self._messages = messages
        self._on_drained = on_drained
        self.subscribed: list[str] = []

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for message in self._messages:
            yield message
        # Stop the actor once the queue is drained, or the listener reconnects
        # and waits for a broker that will never send anything again.
        self._on_drained()


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


class _Run:
    """One drive of the listener: the actor it ran against, and the broker."""

    def __init__(self, main: MainActor, broker: "_Broker") -> None:
        self.main = main
        self.broker = broker

    @property
    def manifests(self) -> dict[str, Any]:
        return self.main._agent_manifests

    @property
    def registry(self) -> dict[str, list[dict[str, Any]]]:
        return self.main._topic_registry


class _Bus:
    """The TopicBus, recording what the listener registers."""

    def __init__(self) -> None:
        self.contracts: dict[str, Any] = {}
        self.removed: list[str] = []

    def register_contract(self, contract: Any) -> None:
        self.contracts[contract.name] = contract

    def unregister(self, name: str) -> None:
        self.removed.append(name)
        self.contracts.pop(name, None)


def manifest(name: str, **over: Any) -> dict[str, Any]:
    """A manifest as an agent publishes it."""
    return {
        "name": name,
        "actor_id": f"{name}-id",
        "publishes": [f"sensors/{name}"],
        "subscribes": [],
        "produces_schema": {"value": "float"},
        **over,
    }


def message(name: str, **over: Any) -> _Message:
    body = manifest(name, **over)
    return _Message(f"agents/{body['actor_id']}/manifest", json.dumps(body).encode())


def tombstone(actor_id: str) -> _Message:
    return _Message(f"agents/{actor_id}/manifest", b"")


async def run_listener(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[_Message],
    *,
    bus: _Bus | None = None,
    manifests: dict[str, Any] | None = None,
    registry: dict[str, list[dict[str, Any]]] | None = None,
) -> _Run:
    """Drive the real listener over `messages` until they are exhausted."""
    main = MainActor.__new__(MainActor)
    main.name = "main"
    main.manifests = ManifestRegistry(main)
    main.nodes = NodeManager(main, main.manifests)
    main.migration = Migration(main, main.nodes)
    main.state = ActorState.RUNNING
    main._mqtt_broker = "localhost"
    main._mqtt_port = 1883
    main._agent_manifests = dict(manifests or {})
    main._topic_registry = dict(registry or {})

    def _stop() -> None:
        main.state = ActorState.STOPPED

    broker = _Broker(messages, _stop)
    monkeypatch.setattr("wactorz.agents.main.manifests.mqtt_client", broker)
    monkeypatch.setattr("wactorz.core.topic_bus.get_topic_bus", lambda: bus)

    await asyncio.wait_for(main._manifest_listener(), timeout=5)
    return _Run(main, broker)


class TestAcceptingAManifest:
    async def test_it_is_stored_under_the_agent_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_listener(monkeypatch, [message("weather")])

        assert run.manifests["weather"]["actor_id"] == "weather-id"

    async def test_the_published_topic_is_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_listener(monkeypatch, [message("weather")])

        assert [m["name"] for m in run.registry["sensors/weather"]] == ["weather"]

    async def test_two_agents_on_one_topic_are_both_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shared = {"publishes": ["sensors/shared"]}
        run = await run_listener(
            monkeypatch, [message("first", **shared), message("second", **shared)]
        )

        assert [m["name"] for m in run.registry["sensors/shared"]] == ["first", "second"]

    async def test_a_second_manifest_replaces_the_first_rather_than_doubling_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Manifests are retained and re-delivered on every reconnect, so
        # appending would grow the registry without bound.
        run = await run_listener(
            monkeypatch,
            [message("weather"), message("weather", produces_schema={"value": "int"})],
        )

        entries = run.registry["sensors/weather"]
        assert len(entries) == 1
        assert entries[0]["produces_schema"] == {"value": "int"}

    async def test_it_subscribes_to_every_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_listener(monkeypatch, [])

        assert run.broker.client.subscribed == ["agents/+/manifest"]


class TestMirroringIntoTheTopicBus:
    """The planner auto-wires from the bus, and a remote agent reaches it only here."""

    async def test_an_accepted_manifest_becomes_a_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = _Bus()

        await run_listener(monkeypatch, [message("weather")], bus=bus)

        assert bus.contracts["weather"].publishes == ["sensors/weather"]

    async def test_observed_field_names_win_over_the_declared_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # What the code actually publishes beats what an LLM said it would, or
        # the planner wires on a field name that never appears on the wire.
        bus = _Bus()
        observed = {"sensors/weather": {"fields": {"temp_c": "float"}}}

        await run_listener(monkeypatch, [message("weather", observed_samples=observed)], bus=bus)

        assert bus.contracts["weather"].produces_schema == {"value": "float", "temp_c": "float"}

    async def test_a_missing_bus_does_not_stop_the_manifest_being_stored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_listener(monkeypatch, [message("weather")], bus=None)

        assert "weather" in run.manifests

    async def test_an_unnamed_manifest_is_not_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "?" is what a manifest with no name reads as, and it is not an agent.
        bus = _Bus()
        unnamed = _Message("agents/x/manifest", json.dumps({"publishes": ["sensors/x"]}).encode())

        await run_listener(monkeypatch, [unnamed], bus=bus)

        assert not bus.contracts


class TestTheDeletionTombstone:
    """An empty retained payload means the agent is gone."""

    async def test_it_drops_the_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_listener(monkeypatch, [message("weather"), tombstone("weather-id")])

        assert not run.manifests

    async def test_it_drops_the_topic_registration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_listener(monkeypatch, [message("weather"), tombstone("weather-id")])

        assert "sensors/weather" not in run.registry

    async def test_a_topic_another_agent_still_publishes_survives(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shared = {"publishes": ["sensors/shared"]}
        run = await run_listener(
            monkeypatch,
            [message("first", **shared), message("second", **shared), tombstone("first-id")],
        )

        assert [m["name"] for m in run.registry["sensors/shared"]] == ["second"]

    async def test_the_planner_stops_seeing_the_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The bus is how the planner finds wiring targets, so an agent left
        # there outlives its own deletion.
        bus = _Bus()

        await run_listener(monkeypatch, [message("weather"), tombstone("weather-id")], bus=bus)

        assert bus.removed == ["weather"]
        assert not bus.contracts

    async def test_a_tombstone_for_an_unknown_agent_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_listener(monkeypatch, [message("weather"), tombstone("ghost-id")])

        assert "weather" in run.manifests


class TestWhatIsSaidAboutIt:
    """The tombstone line is how an operator learns an agent went away.

    Nothing else announces it: the agent stops appearing, which reads the same
    as it never having been there.
    """

    async def test_a_removal_is_announced(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="wactorz.agents.main.manifests"):
            await run_listener(monkeypatch, [message("weather"), tombstone("weather-id")])

        assert "removed 'weather'" in caplog.text

    async def test_a_tombstone_for_an_unknown_agent_announces_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="wactorz.agents.main.manifests"):
            await run_listener(monkeypatch, [message("weather"), tombstone("ghost-id")])

        assert "removed" not in caplog.text


class TestPayloadsThatCannotBeUsed:
    """Rejected without ending the listener — one bad publisher must not deafen main."""

    @pytest.mark.parametrize(
        "payload",
        [b"{not json", b"[]", b'"a string"', b"123"],
        ids=["malformed", "list", "string", "number"],
    )
    async def test_it_is_ignored(self, monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
        run = await run_listener(monkeypatch, [_Message("agents/x/manifest", payload)])

        assert not run.manifests

    async def test_a_later_good_manifest_is_still_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_listener(
            monkeypatch, [_Message("agents/x/manifest", b"{not json"), message("weather")]
        )

        assert "weather" in run.manifests
