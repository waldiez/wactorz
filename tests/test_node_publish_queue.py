"""A node's publish queue is bounded, and gives way in a stated order.

Unbounded, a broker outage on a node that keeps publishing grows the queue until
the machine runs out of memory -- and these run on Raspberry Pis. Bounded, the
question becomes which message gives way, which is what these pin.
"""

import asyncio
from typing import Any

import pytest

from wactorz.node import publishing


@pytest.fixture(name="runner")
def runner_fixture() -> Any:
    """A publisher with its queue in place but nothing connected."""
    publisher = publishing.NodePublisher("localhost", 1883, "rpi")
    publisher._queue = publishing.new_pub_queue()
    return publisher


class TestTheQueueItself:
    def test_the_queue_the_runner_builds_is_bounded(self) -> None:
        # The cap tests below install their own small queue, so without this
        # nothing would notice the production one losing its bound.
        queue = publishing.new_pub_queue()

        assert queue.maxsize == publishing.MAX_QUEUED
        assert queue.maxsize > 0


class TestClassification:
    @pytest.mark.parametrize(
        "topic",
        [
            "nodes/rpi/heartbeat",
            "agents/abc/logs",
            "agents/abc/metrics",
            "nodes/rpi/status",
        ],
    )
    def test_telemetry_is_droppable(self, topic: str) -> None:
        assert not publishing.is_critical(topic)

    @pytest.mark.parametrize(
        "topic",
        [
            "agents/abc/results",
            "agents/abc/errors",
            "agents/abc/manifest",
            "nodes/rpi/migrate_result",
            "nodes/rpi/state_return",
            "nodes/other/spawn",
        ],
    )
    def test_everything_else_is_critical(self, topic: str) -> None:
        # A lost migrate_result or state_return loses an agent; a lost heartbeat
        # is replaced a second later.
        assert publishing.is_critical(topic)


class TestTheCap:
    async def test_it_stops_growing(self, runner: Any) -> None:
        runner._queue = asyncio.Queue(maxsize=4)

        for n in range(50):
            await runner.publish("nodes/rpi/heartbeat", {"n": n})

        assert runner._queue.qsize() == 4
        assert runner.dropped > 0

    async def test_telemetry_gives_way_before_control(self, runner: Any) -> None:

        runner._queue = asyncio.Queue(maxsize=3)
        await runner.publish("agents/abc/results", {"keep": "me"})
        for n in range(10):
            await runner.publish("nodes/rpi/heartbeat", {"n": n})

        queued = [runner._queue.get_nowait() for _ in range(runner._queue.qsize())]
        topics = [entry[0] for entry in queued]

        assert "agents/abc/results" in topics, "a result was dropped while telemetry queued"

    async def test_a_full_queue_of_control_drops_the_newcomer(self, runner: Any) -> None:
        # Nothing droppable is queued, so the incoming message gives way rather
        # than the caller being made to wait -- waiting would push a stalled
        # broker back into the agent code that called publish().

        runner._queue = asyncio.Queue(maxsize=2)
        await runner.publish("agents/abc/results", {"first": 1})
        await runner.publish("agents/abc/results", {"second": 2})

        await runner.publish("agents/abc/results", {"third": 3})

        assert runner._queue.qsize() == 2
        assert runner.dropped == 1

    async def test_publishing_never_blocks(self, runner: Any) -> None:
        # `wait_for`, not `asyncio.timeout`: the latter is 3.11+ and this
        # project supports 3.10.
        runner._queue = asyncio.Queue(maxsize=1)

        async def publish_many() -> None:
            for n in range(200):
                await runner.publish("agents/abc/results", {"n": n})

        await asyncio.wait_for(publish_many(), timeout=2)


class TestPublishQoS:
    """Telemetry goes out at QoS 0, and it is not only about queue space.

    At QoS 1 the broker holds heartbeats for a subscriber that is away and
    replays them on reconnect. Main records a heartbeat as "seen just now" on
    receipt, so a replayed batch marks a node that died hours ago as online --
    and that is what gates migrating an agent onto it.
    """

    @pytest.mark.parametrize(
        ("topic", "expected"),
        [
            ("nodes/rpi/heartbeat", 0),
            ("agents/abc/logs", 0),
            ("agents/abc/metrics", 0),
            ("nodes/rpi/status", 0),
            ("agents/abc/results", 1),
            ("nodes/rpi/state_return", 1),
            ("nodes/rpi/spawn_ack", 1),
            ("agents/abc/manifest", 1),
        ],
    )
    async def test_the_queued_entry_carries_the_right_class(
        self, runner: Any, topic: str, expected: int
    ) -> None:
        await runner.publish(topic, {"x": 1})

        _topic, _payload, _retain, critical = runner._queue.get_nowait()
        assert (1 if critical else 0) == expected

    async def test_the_publisher_sends_telemetry_at_qos_zero(self, runner: Any) -> None:
        # The classification has to reach the wire, not only the queue.
        sent: list[tuple[str, int]] = []

        class _Client:
            def publish(
                self, topic: str, _payload: Any, qos: int = 0, retain: bool = False
            ) -> None:
                sent.append((topic, qos))

        await runner.publish("nodes/rpi/heartbeat", {"x": 1})
        await runner.publish("agents/abc/results", {"x": 1})
        await runner.publish_one_queued(_Client())
        await runner.publish_one_queued(_Client())

        assert sent == [("nodes/rpi/heartbeat", 0), ("agents/abc/results", 1)]


class TestOrdering:
    async def test_the_rebuild_keeps_the_survivors_in_order(self, runner: Any) -> None:
        # _discard_one_telemetry drains the queue and refills it, because
        # asyncio.Queue offers no way to remove from the middle. A reversal
        # there would silently reorder a node's control messages, so the mix
        # here is chosen to force the rebuild rather than a plain drop: the
        # queue must be full *and* hold something droppable.
        runner._queue = asyncio.Queue(maxsize=3)
        await runner.publish("nodes/rpi/heartbeat", {"tag": "old-telemetry"})
        await runner.publish("agents/abc/results", {"tag": "first-result"})
        await runner.publish("agents/abc/logs", {"tag": "later-telemetry"})

        await runner.publish("agents/abc/errors", {"tag": "arrives-last"})

        queued = [runner._queue.get_nowait() for _ in range(runner._queue.qsize())]

        # Oldest telemetry evicted; everything else keeps its arrival order.
        assert [entry[0] for entry in queued] == [
            "agents/abc/results",
            "agents/abc/logs",
            "agents/abc/errors",
        ]
        assert runner.dropped == 1


class _Reason:
    """A paho v2 reason code, which reports failure through `is_failure`."""

    def __init__(self, failure: bool, text: str = "Not authorized") -> None:
        self.is_failure = failure
        self._text = text

    def __str__(self) -> str:
        return self._text


class _Paho:
    """A paho client that answers the connection the way the broker would."""

    def __init__(self, reason: _Reason | None) -> None:
        self._reason = reason
        self.on_connect = None
        self.closed = False

    def username_pw_set(self, *_a: Any, **_kw: Any) -> None: ...
    def tls_set_context(self, *_a: Any, **_kw: Any) -> None: ...
    def loop_start(self) -> None: ...

    def connect(self, *_a: Any, **_kw: Any) -> None:
        # The answer arrives on the network loop, after connect() returns —
        # which is the whole reason this has to be waited for.
        if self._reason is not None and self.on_connect is not None:
            self.on_connect(self, None, None, self._reason, None)

    def loop_stop(self) -> None:
        self.closed = True

    def disconnect(self) -> None:
        self.closed = True


class TestTheConnectionIsConfirmed:
    """`connect` returns once the CONNECT is away; being let in is a later answer.

    Without waiting for it, a node whose credentials the broker refuses reported
    a connection it did not have and then dropped everything it published —
    heartbeats included — so the node was simply absent with nothing saying why.
    """

    def _publisher(self, monkeypatch: pytest.MonkeyPatch, reason: _Reason | None) -> Any:
        made: list[_Paho] = []

        def _client(*_a: Any, **_kw: Any) -> _Paho:
            made.append(_Paho(reason))
            return made[-1]

        monkeypatch.setattr(publishing.paho_mqtt, "Client", _client)
        monkeypatch.setattr(publishing, "CONNACK_TIMEOUT_S", 0.2)
        return publishing.NodePublisher("broker.lan", 8883, "rpi"), made

    def test_a_refusal_is_raised_with_its_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        publisher, made = self._publisher(monkeypatch, _Reason(failure=True))

        with pytest.raises(ConnectionError) as caught:
            publisher.connect()

        assert "Not authorized" in str(caught.value)
        assert made[0].closed, "the refused client was left open"

    def test_silence_is_raised_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A broker that takes the connection and never answers is the same
        # problem wearing a different hat: nothing published would arrive.
        publisher, made = self._publisher(monkeypatch, None)

        with pytest.raises(ConnectionError) as caught:
            publisher.connect()

        assert "did not answer" in str(caught.value)
        assert made[0].closed

    def test_being_let_in_returns_the_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        publisher, made = self._publisher(monkeypatch, _Reason(failure=False, text="Success"))

        assert publisher.connect() is made[0]
        assert not made[0].closed


class TestDrainingTheQueue:
    """The loop that actually gets a node's messages onto the wire.

    It is the only thing standing between the queue and the broker, and it has
    to survive a broker that goes away — a node is not somewhere anyone is
    going to restart a process by hand.
    """

    class _Client:
        def __init__(self, fail_after: int | None = None) -> None:
            self.sent: list[tuple[str, int]] = []
            self.closed = False
            self._fail_after = fail_after

        def publish(self, topic: str, _payload: Any, qos: int = 0, retain: bool = False) -> None:
            if self._fail_after is not None and len(self.sent) >= self._fail_after:
                raise OSError("the broker went away")
            self.sent.append((topic, qos))

        def loop_stop(self) -> None:
            self.closed = True

        def disconnect(self) -> None:
            self.closed = True

    async def test_it_sends_what_was_queued(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._Client()
        publisher = publishing.NodePublisher("broker.lan", 8883, "rpi")
        monkeypatch.setattr(publisher, "connect", lambda: client)

        async def _queue_then_wait() -> None:
            await publisher.publish("nodes/rpi/heartbeat", {"n": 1})
            await publisher.publish("agents/abc/results", {"n": 2})

        ready = asyncio.Event()
        task = asyncio.create_task(publisher.run(ready))
        try:
            await asyncio.wait_for(ready.wait(), timeout=2)
            await _queue_then_wait()
            for _ in range(200):
                if len(client.sent) >= 2:
                    break
                await asyncio.sleep(0.005)
        finally:
            publisher.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        # Telemetry at QoS 0, anything whose loss would cost something at 1.
        assert client.sent == [("nodes/rpi/heartbeat", 0), ("agents/abc/results", 1)]

    async def test_a_broker_that_goes_away_is_reconnected_to(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made: list[Any] = []

        def _connect() -> Any:
            made.append(self._Client(fail_after=0 if not made else None))
            return made[-1]

        publisher = publishing.NodePublisher("broker.lan", 8883, "rpi")
        monkeypatch.setattr(publisher, "connect", _connect)
        # The real pause is for a real broker; nothing is learned by waiting it out.
        monkeypatch.setattr(publishing, "RETRY_DELAY_S", 0.0)

        ready = asyncio.Event()
        task = asyncio.create_task(publisher.run(ready))
        try:
            await asyncio.wait_for(ready.wait(), timeout=2)
            await publisher.publish("agents/abc/results", {"n": 1})
            for _ in range(400):
                if len(made) >= 2 and made[1].sent:
                    break
                await asyncio.sleep(0.005)
        finally:
            publisher.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert made[0].closed, "the broken client was kept"
        # The message survived the broker going away. It had already been taken
        # off the queue when the send failed, so without holding it there was
        # nowhere left for it to be — and a node has no outbox behind this.
        assert made[1].sent == [("agents/abc/results", 1)]

    async def test_a_message_the_client_will_never_take_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Held messages are retried after a reconnect, so one that can never be
        # sent — an impossible topic, a payload past the protocol's size — would
        # otherwise stop everything behind it for ever.
        class _Picky(self._Client):
            def publish(self, topic: str, _payload: Any, qos: int = 0, retain: bool = False):
                if topic.startswith("bad/"):
                    raise ValueError("payload too large")
                self.sent.append((topic, qos))

        client = _Picky()
        publisher = publishing.NodePublisher("broker.lan", 8883, "rpi")
        monkeypatch.setattr(publisher, "connect", lambda: client)
        monkeypatch.setattr(publishing, "RETRY_DELAY_S", 0.0)

        ready = asyncio.Event()
        task = asyncio.create_task(publisher.run(ready))
        try:
            await asyncio.wait_for(ready.wait(), timeout=2)
            await publisher.publish("bad/topic", {"n": 1})
            await publisher.publish("agents/abc/results", {"n": 2})
            for _ in range(200):
                if client.sent:
                    break
                await asyncio.sleep(0.005)
        finally:
            publisher.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert client.sent == [("agents/abc/results", 1)], "the queue stalled on it"
        assert publisher.dropped == 1
