"""Every actor can subscribe to topics and keep rolling windows, not only generated ones.

A native agent used to open a broker connection of its own for each topic it
listened on. The base class now carries the one-connection hub a generated
program has, so a subclass -- or a function declared with ``wactorz.agent`` --
subscribes with one call and the connection is closed when the actor stops.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic import listener as listener_module
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.core import subscriptions as subscriptions_module
from wactorz.core import topic_bus
from wactorz.core.actor import Actor, ActorState, Message


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeClient:
    def __init__(self, broker: "FakeBroker") -> None:
        self._broker = broker
        self.subscribed: list[str] = []

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 0, **_kwargs: Any) -> None:
        self.subscribed.append(topic)

    async def unsubscribe(self, topic: str) -> None:
        return None

    @property
    def messages(self) -> Any:
        async def _stream() -> Any:
            while True:
                yield await self._broker.queue.get()

        return _stream()


class FakeBroker:
    def __init__(self) -> None:
        self.connections: list[FakeClient] = []
        self.queue: asyncio.Queue = asyncio.Queue()

    def __call__(self, _host: str, _port: int, **_kwargs: Any) -> FakeClient:
        client = FakeClient(self)
        self.connections.append(client)
        return client

    async def deliver(self, topic: str, payload: bytes = b"{}") -> None:
        await self.queue.put(FakeMessage(topic, payload))


class Probe(Actor):
    """The plainest actor there is."""

    async def handle_message(self, msg: Message) -> None:
        return None


@pytest.fixture(name="broker")
def broker_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(subscriptions_module, "mqtt_client", fake)
    monkeypatch.setattr(listener_module, "mqtt_client", fake)
    return fake


@pytest.fixture(name="probe")
def probe_fixture(tmp_path: Path) -> Probe:
    return Probe(name="probe", persistence_dir=str(tmp_path))


async def _settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


async def _stop_hub(actor: Actor) -> None:
    for task in actor._tasks:
        task.cancel()
    await asyncio.gather(*actor._tasks, return_exceptions=True)


class TestSubscribe:
    async def test_an_async_callback_gets_the_decoded_payload(
        self, broker: FakeBroker, probe: Probe
    ) -> None:
        seen: list[Any] = []

        async def on_reading(payload: Any) -> None:
            seen.append(payload)

        probe.subscribe("sensors/imu/#", on_reading)
        await _settle()
        await broker.deliver("sensors/imu/left", b'{"ax": 1.5}')
        await _settle()

        assert seen == [{"ax": 1.5}]
        await _stop_hub(probe)

    async def test_a_plain_function_is_run_off_the_event_loop(
        self, broker: FakeBroker, probe: Probe
    ) -> None:
        import threading

        threads: list[str] = []

        def on_reading(payload: Any) -> None:
            threads.append(threading.current_thread().name)

        probe.subscribe("sensors/temp", on_reading)
        await _settle()
        await broker.deliver("sensors/temp", b'{"c": 21}')
        for _ in range(20):
            await asyncio.sleep(0.01)
            if threads:
                break

        assert threads and threads[0] != threading.main_thread().name
        await _stop_hub(probe)

    async def test_every_subscription_shares_one_connection(
        self, broker: FakeBroker, probe: Probe
    ) -> None:
        async def ignore(_payload: Any) -> None:
            return None

        probe.subscribe("a/one", ignore)
        probe.subscribe("a/two", ignore)
        await _settle()

        assert len(broker.connections) == 1
        assert set(broker.connections[0].subscribed) == {"a/one", "a/two"}
        # The hub task is the actor's to stop: tracked once, not per topic.
        assert len(probe._tasks) == 1
        await _stop_hub(probe)

    async def test_the_connection_is_stopped_with_the_actor(
        self, broker: FakeBroker, probe: Probe
    ) -> None:
        async def ignore(_payload: Any) -> None:
            return None

        probe.subscribe("a/one", ignore)
        await _settle()
        hub_task = probe._tasks[0]

        await probe.stop()

        assert hub_task.done()

    async def test_a_callback_must_be_callable(self, probe: Probe) -> None:
        with pytest.raises(TypeError):
            probe.subscribe("a/one", "not a function")  # type: ignore[arg-type]  # the refused value

    async def test_a_callback_that_keeps_failing_fails_the_actor(
        self, broker: FakeBroker, probe: Probe
    ) -> None:
        async def broken(_payload: Any) -> None:
            raise RuntimeError("model not loaded")

        probe.subscribe("bad/topic", broken)
        await _settle()
        for _ in range(subscriptions_module.MAX_CONSECUTIVE_FAILURES):
            await broker.deliver("bad/topic")
            await _settle()

        assert probe.state == ActorState.FAILED
        assert probe.metrics.errors == subscriptions_module.MAX_CONSECUTIVE_FAILURES
        await _stop_hub(probe)

    async def test_a_generated_agent_keeps_its_repairing_hub(self, tmp_path: Path) -> None:
        """`Actor.subscribe` on a DynamicAgent must use the hub that asks for repairs."""
        agent = DynamicAgent(name="gen", code="", persistence_dir=str(tmp_path))

        assert isinstance(agent._make_hub(), listener_module.SubscriptionHub)


class TestWindow:
    @pytest.fixture(autouse=True)
    def _no_window_connections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        started: list[str] = []
        stopped: list[str] = []

        def start(self: topic_bus.StreamWindow, _broker: str, _port: int) -> Any:
            started.append(self.topic)
            return self

        def stop(self: topic_bus.StreamWindow) -> None:
            stopped.append(self.topic)

        monkeypatch.setattr(topic_bus.StreamWindow, "start", start)
        monkeypatch.setattr(topic_bus.StreamWindow, "stop", stop)
        monkeypatch.setattr(topic_bus, "_topic_bus", None)
        self.started = started
        self.stopped = stopped

    async def test_a_window_is_started_once_per_topic(self, probe: Probe) -> None:
        first = probe.window("sensors/temp", seconds=60)
        again = probe.window("sensors/temp", seconds=60)

        assert again is first
        assert self.started == ["sensors/temp"]
        assert first.seconds == 60

    async def test_windows_are_closed_when_the_actor_stops(self, probe: Probe) -> None:
        probe.window("sensors/temp")
        probe.window("sensors/hum")

        await probe.stop()

        assert sorted(self.stopped) == ["sensors/hum", "sensors/temp"]
        assert probe._windows == {}
