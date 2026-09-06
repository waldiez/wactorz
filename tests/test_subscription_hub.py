"""An actor's subscriptions share one MQTT connection.

Every `agent.subscribe()` used to open its own. Agent code is model-authored, so
a generated loop could open as many connections as it liked with nothing
reviewing it first -- and each durable session is broker state. One connection
per actor makes that cost constant however the program is written.
"""

import asyncio
import uuid
from typing import Any

import aiomqtt
import pytest

from wactorz.agents.dynamic import listener as listener_module
from wactorz.agents.dynamic.listener import SubscriptionHub
from wactorz.core.actor import derive_actor_id


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeClient:
    """One broker connection: records subscriptions, replays queued messages."""

    def __init__(self, broker: "FakeBroker") -> None:
        self._broker = broker
        self.subscribed: list[str] = []
        self.subscribed_qos: list[int] = []
        self.unsubscribed: list[str] = []

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 0, **_kwargs: Any) -> None:
        self.subscribed.append(topic)
        self.subscribed_qos.append(qos)

    async def unsubscribe(self, topic: str) -> None:
        self.unsubscribed.append(topic)

    @property
    def messages(self) -> Any:
        async def _stream() -> Any:
            while True:
                message = await self._broker.queue.get()
                yield message

        return _stream()


class FakeBroker:
    """Stands in for `mqtt_client`, counting how many connections were opened."""

    def __init__(self) -> None:
        self.connections: list[FakeClient] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.identifiers: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, _host: str, _port: int, **kwargs: Any) -> FakeClient:
        self.identifiers.append(kwargs.get("identifier", ""))
        self.kwargs.append(kwargs)
        client = FakeClient(self)
        self.connections.append(client)
        return client

    async def deliver(self, topic: str, payload: bytes = b"{}") -> None:
        await self.queue.put(FakeMessage(topic, payload))


class FakeActor:
    def __init__(self) -> None:
        self.name = "probe-agent"
        self.actor_id = "11111111-2222-3333-4444-555555555555"
        self._mqtt_broker = "localhost"
        self._mqtt_port = 1883
        self._cb_error_count: dict[str, int] = {}
        self._cb_error_last: dict[str, float] = {}
        self.state = "RUNNING"
        self.published_errors: list[dict] = []

    async def _publish_error(self, **kwargs: Any) -> None:
        self.published_errors.append(kwargs)


@pytest.fixture(name="broker")
def broker_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(listener_module, "mqtt_client", fake)
    return fake


async def _settle() -> None:
    """Let the hub's task run: connect, subscribe, dispatch."""
    for _ in range(6):
        await asyncio.sleep(0)


class TestOneConnectionPerActor:
    async def test_many_subscriptions_open_one_connection(self, broker: FakeBroker) -> None:
        actor = FakeActor()
        hub = SubscriptionHub(actor)

        for topic in ("a/one", "a/two", "a/three", "b/+/four"):
            hub.bind(topic, lambda _payload: None)
        await _settle()

        assert len(broker.connections) == 1
        assert set(broker.connections[0].subscribed) == {"a/one", "a/two", "a/three", "b/+/four"}
        hub._task.cancel()

    async def test_only_the_first_bind_starts_a_task(self, broker: FakeBroker) -> None:
        # The caller tracks the returned task on the actor; a second track would
        # cancel the shared connection twice on stop.
        hub = SubscriptionHub(FakeActor())

        first = hub.bind("a/one", lambda _payload: None)
        second = hub.bind("a/two", lambda _payload: None)

        assert first is not None
        assert second is None
        first.cancel()

    async def test_the_connection_carries_the_actor_id(self, broker: FakeBroker) -> None:
        actor = FakeActor()
        hub = SubscriptionHub(actor)

        hub.bind("a/one", lambda _payload: None)
        await _settle()

        assert broker.identifiers == [f"wactorz-agent-{actor.actor_id}"]
        hub._task.cancel()


class TestDurability:
    """Only an actor whose id survives a restart holds a session.

    A named actor derives its id from its name, so it reconnects as the same
    client and resumes what the broker held. An anonymous one gets a fresh id
    every incarnation, so a session kept under the old id is unreachable --
    durability there is meaningless rather than harmful.
    """

    def test_a_named_actor_is_durable(self) -> None:
        actor = FakeActor()
        actor.name = "kitchen-sensor"
        actor.actor_id = derive_actor_id("kitchen-sensor")

        assert listener_module.is_durable_actor(actor)

    def test_an_anonymous_actor_is_not(self) -> None:
        # Actor() without a name: a uuid4 id and a name derived from it.
        actor = FakeActor()
        actor.actor_id = str(uuid.uuid4())
        actor.name = f"actor-{actor.actor_id[:8]}"

        assert not listener_module.is_durable_actor(actor)

    async def test_a_durable_hub_asks_for_a_v5_session_with_an_expiry(
        self, broker: FakeBroker
    ) -> None:
        hub = SubscriptionHub(FakeActor(), durable=True)

        hub.bind("a/one", lambda _p: None)
        await _settle()

        kwargs = broker.kwargs[0]
        # The protocol itself, not just its properties: v3.1.1 has no session
        # expiry at all, which is the whole reason this role speaks v5.
        assert kwargs["protocol"] is aiomqtt.ProtocolVersion.V5
        assert kwargs["clean_start"] is False
        assert kwargs["properties"].SessionExpiryInterval == hub.SESSION_EXPIRY_SECONDS
        assert broker.connections[0].subscribed_qos == [1]
        hub._task.cancel()

    async def test_a_non_durable_hub_keeps_a_clean_session(self, broker: FakeBroker) -> None:
        # QoS 1 on a clean session buys nothing -- there is no session for the
        # broker to hold a message in -- so it stays at 0 rather than implying it.
        hub = SubscriptionHub(FakeActor(), durable=False)

        hub.bind("a/one", lambda _p: None)
        await _settle()

        assert "clean_start" not in broker.kwargs[0]
        assert "properties" not in broker.kwargs[0]
        assert broker.connections[0].subscribed_qos == [0]
        hub._task.cancel()


class TestDispatch:
    async def test_two_callbacks_on_one_topic_both_run(self, broker: FakeBroker) -> None:
        # Bindings are a list, not a dict keyed by topic: keying by topic drops
        # the first silently, and only the second callback would ever run.
        seen: list[str] = []
        hub = SubscriptionHub(FakeActor())
        hub.bind("shared/topic", lambda _p: seen.append("first"))
        hub.bind("shared/topic", lambda _p: seen.append("second"))
        await _settle()

        await broker.deliver("shared/topic")
        await _settle()

        assert sorted(seen) == ["first", "second"]
        # ...and the filter is only subscribed once on the wire.
        assert broker.connections[0].subscribed == ["shared/topic"]
        hub._task.cancel()

    async def test_a_message_reaches_only_matching_bindings(self, broker: FakeBroker) -> None:
        seen: list[str] = []
        hub = SubscriptionHub(FakeActor())
        hub.bind("sensors/+/temp", lambda _p: seen.append("wildcard"))
        hub.bind("sensors/kitchen/temp", lambda _p: seen.append("exact"))
        hub.bind("other/thing", lambda _p: seen.append("unrelated"))
        await _settle()

        await broker.deliver("sensors/kitchen/temp")
        await _settle()

        assert sorted(seen) == ["exact", "wildcard"]
        hub._task.cancel()


class TestPerTopicOrdering:
    """Messages on one topic stay serialised, as they were before the refactor.

    The connection this replaced awaited each callback, so a topic's messages
    were handled one at a time in arrival order. Agent code is model-authored
    and stateful, and none of it is written to be re-entrant -- two messages
    from the same topic running concurrently would interleave at every `await`
    inside the callback.
    """

    async def test_callbacks_on_one_topic_do_not_overlap(self, broker: FakeBroker) -> None:
        overlaps: list[int] = []
        active = 0
        order: list[int] = []

        async def slow(payload: Any) -> None:
            nonlocal active
            active += 1
            overlaps.append(active)
            await asyncio.sleep(0)
            order.append(payload["n"])
            active -= 1

        hub = SubscriptionHub(FakeActor())
        hub.bind("same/topic", slow)
        await _settle()

        await broker.deliver("same/topic", b'{"n": 1}')
        await broker.deliver("same/topic", b'{"n": 2}')
        await broker.deliver("same/topic", b'{"n": 3}')
        await _settle()

        assert max(overlaps) == 1, "two callbacks on one topic ran concurrently"
        assert order == [1, 2, 3], "messages were reordered"
        hub._task.cancel()

    async def test_a_slow_topic_does_not_hold_up_a_different_one(self, broker: FakeBroker) -> None:
        # Serial per topic, concurrent across topics -- the point of sharing one
        # connection rather than opening several.
        fast_ran = asyncio.Event()

        async def slow(_payload: Any) -> None:
            await asyncio.sleep(30)

        async def fast(_payload: Any) -> None:
            fast_ran.set()

        hub = SubscriptionHub(FakeActor())
        hub.bind("slow/topic", slow)
        hub.bind("fast/topic", fast)
        await _settle()

        await broker.deliver("slow/topic")
        await _settle()
        await broker.deliver("fast/topic")
        await _settle()

        assert fast_ran.is_set()
        hub._task.cancel()


class TestBackpressure:
    async def test_a_callback_that_falls_behind_drops_the_oldest(self, broker: FakeBroker) -> None:
        # The serialising queue is otherwise the unbounded backlog the old
        # design avoided by blocking. Bounded, oldest-first: for the sensor
        # streams these carry, the freshest reading is the useful one.
        hub = SubscriptionHub(FakeActor())
        hub.QUEUE_MAX = 2
        seen: list[int] = []

        async def slow(payload: Any) -> None:
            await asyncio.sleep(0)
            seen.append(payload["n"])

        hub.bind("busy/topic", slow)
        await _settle()
        binding = hub._bindings[0]

        for n in range(6):
            binding.offer({"n": n})

        assert binding.dropped > 0
        assert binding.queue.qsize() <= 2
        hub._task.cancel()


class TestTheErrorBudget:
    async def test_an_exhausted_budget_fails_the_actor_and_drops_the_binding(
        self, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Crosses into the actor/Supervisor boundary, so it is the path most
        # likely to rot unnoticed.
        monkeypatch.setattr(listener_module, "CB_ERROR_REPORT_INTERVAL", 0.0)
        actor = FakeActor()
        hub = SubscriptionHub(actor)

        async def always_fails(_payload: Any) -> None:
            raise RuntimeError("callback is broken")

        hub.bind("bad/topic", always_fails)
        await _settle()

        for _ in range(listener_module.CB_MAX_ESCALATIONS):
            await broker.deliver("bad/topic")
            await _settle()

        assert actor.published_errors, "no error was escalated"
        assert actor.published_errors[-1]["fatal"] is True
        assert str(actor.state) == "ActorState.FAILED"
        assert not [b for b in hub._bindings if b.topic == "bad/topic"]
        hub._task.cancel()

    async def test_a_recovering_callback_clears_its_budget(self, broker: FakeBroker) -> None:
        actor = FakeActor()
        hub = SubscriptionHub(actor)
        fail = [True]

        async def flaky(_payload: Any) -> None:
            if fail[0]:
                raise RuntimeError("transient")

        hub.bind("flaky/topic", flaky)
        await _settle()
        await broker.deliver("flaky/topic")
        await _settle()
        assert actor._cb_error_count.get("flaky/topic")

        fail[0] = False
        await broker.deliver("flaky/topic")
        await _settle()

        assert "flaky/topic" not in actor._cb_error_count
        hub._task.cancel()


class TestRestart:
    async def test_a_revived_hub_still_delivers(self, broker: FakeBroker) -> None:
        # Cancelling the hub task cancels the binding workers with it, and
        # `bind` revives the task once it has ended. Without reviving the
        # workers too, the hub re-subscribes and then queues into queues nobody
        # drains -- silently deaf. Found against a real broker, not here.
        seen: list[str] = []
        hub = SubscriptionHub(FakeActor())
        hub.bind("topic/x", lambda _p: seen.append("got"))
        await _settle()

        hub._task.cancel()
        await _settle()
        hub._task = asyncio.create_task(hub.run())
        await _settle()

        await broker.deliver("topic/x")
        await _settle()

        assert seen == ["got"], "a restarted hub stopped delivering"
        hub._task.cancel()


class TestRepairUnbinds:
    async def test_clear_stops_callbacks_and_unsubscribes(self, broker: FakeBroker) -> None:
        # The constraint the shared connection introduces: cancelling the task
        # used to stop the old program's callbacks, because the task owned the
        # only connection carrying them. It no longer does.
        seen: list[str] = []
        hub = SubscriptionHub(FakeActor())
        hub.bind("old/topic", lambda _p: seen.append("old"))
        await _settle()

        await hub.clear()
        await broker.deliver("old/topic")
        await _settle()

        assert not seen, "a repaired-away callback still received a message"
        assert broker.connections[0].unsubscribed == ["old/topic"]
        hub._task.cancel()

    async def test_rebinding_after_a_repair_works_on_the_same_connection(
        self, broker: FakeBroker
    ) -> None:
        seen: list[str] = []
        hub = SubscriptionHub(FakeActor())
        hub.bind("topic/x", lambda _p: seen.append("before"))
        await _settle()
        await hub.clear()

        hub.bind("topic/x", lambda _p: seen.append("after"))
        await _settle()
        await broker.deliver("topic/x")
        await _settle()

        assert seen == ["after"]
        assert len(broker.connections) == 1, "a repair should not rebuild the connection"
        hub._task.cancel()
