"""The in-memory outbox has a ceiling, and telemetry is what gives way.

A broker that is absent, or slower than the app publishes, grew this queue
without limit until the process died. The failure was a memory graph rather than
a message, which is what made it easy to leave alone.

What is discarded matters more than that something is. QoS 1 is written to the
SQLite outbox *before* it is queued, so dropping it from memory costs a wait
until the reconnect that reloads it — not the message. QoS 0 is memory-only, and
it is heartbeats, metrics and status: the next sample supersedes the last, so
the oldest is the right one to lose.
"""

import asyncio

import pytest

from wactorz.core.mqtt_publisher import MQTTPublisher

TELEMETRY = ("agents/a/heartbeat", "payload", False, 0, -1)
DURABLE = ("nodes/rpi/spawn", "payload", False, 1, 7)


@pytest.fixture(name="publisher")
def publisher_fixture(tmp_path) -> MQTTPublisher:  # type: ignore[no-untyped-def]
    publisher = MQTTPublisher.__new__(MQTTPublisher)
    publisher._queue = asyncio.Queue(maxsize=3)
    publisher.MAX_QUEUED = 3  # type: ignore[misc]
    publisher._dropped = 0
    return publisher


def _queued(publisher: MQTTPublisher) -> list[tuple]:
    return list(publisher._queue._queue)  # type: ignore[attr-defined]


class TestTheCeiling:
    def test_the_queue_stops_growing(self, publisher: MQTTPublisher) -> None:
        for _ in range(50):
            publisher._enqueue(TELEMETRY)

        assert publisher._queue.qsize() == 3

    def test_publishing_never_blocks_the_caller(self, publisher: MQTTPublisher) -> None:
        # `publish()` promises to return immediately. Waiting for space would
        # push a slow broker back into the actor loop that called it, which is
        # the thing this class exists to prevent.
        for _ in range(50):
            publisher._enqueue(TELEMETRY)  # would hang here if it awaited space

    def test_it_keeps_the_newest_telemetry(self, publisher: MQTTPublisher) -> None:
        # The freshest sample is the useful one — an old heartbeat tells you
        # nothing the new one does not.
        for i in range(6):
            publisher._enqueue(("agents/a/heartbeat", f"sample-{i}", False, 0, -1))

        assert [entry[1] for entry in _queued(publisher)] == ["sample-3", "sample-4", "sample-5"]


class TestWhatGivesWay:
    def test_telemetry_is_discarded_before_a_durable_message(
        self, publisher: MQTTPublisher
    ) -> None:
        publisher._enqueue(DURABLE)
        publisher._enqueue(TELEMETRY)
        publisher._enqueue(DURABLE)

        publisher._enqueue(DURABLE)

        assert [entry[3] for entry in _queued(publisher)] == [1, 1, 1]

    def test_a_queue_of_durable_messages_drops_the_incoming_one(
        self, publisher: MQTTPublisher
    ) -> None:
        # Nothing droppable is queued, so the new arrival gives way instead. It
        # is already in the SQLite outbox, so what is lost is the wait until the
        # next connect reloads it.
        for _ in range(3):
            publisher._enqueue(DURABLE)

        publisher._enqueue(("nodes/rpi/stop", "payload", False, 1, 9))

        assert [entry[0] for entry in _queued(publisher)] == ["nodes/rpi/spawn"] * 3

    def test_nothing_is_discarded_below_the_cap(self, publisher: MQTTPublisher) -> None:
        publisher._enqueue(TELEMETRY)
        publisher._enqueue(DURABLE)

        assert publisher._dropped == 0


class TestSayingSo:
    def test_the_first_drop_is_reported(self, publisher: MQTTPublisher, caplog) -> None:  # type: ignore[no-untyped-def]
        # A queue silently discarding messages is indistinguishable from a
        # broker silently losing them.
        with caplog.at_level("WARNING"):
            for _ in range(4):
                publisher._enqueue(TELEMETRY)

        assert "outbox full" in caplog.text

    def test_it_does_not_report_every_one(self, publisher: MQTTPublisher, caplog) -> None:  # type: ignore[no-untyped-def]
        # A broker that is down drops thousands; a line each would bury the
        # rest of the log, which is where the reason for the outage is.
        with caplog.at_level("WARNING"):
            for _ in range(500):
                publisher._enqueue(TELEMETRY)

        assert caplog.text.count("outbox full") == 1
        assert publisher._dropped == 497
