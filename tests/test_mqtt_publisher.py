"""The publisher's promise: a caller hands a message over and stops caring.

Two things have to hold for that to be true. Publishing must not wait on the
broker, so an actor's loop is never paced by the network. And a message the
caller was told would be delivered must not disappear when the connection or the
process does — which is what the SQLite outbox is for, and why a message is only
removed from it once the broker has actually taken it.
"""

import asyncio
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from wactorz.core.mqtt_publisher import MQTTPublisher


class _FakeClient:
    """A broker that records what it was given, and can be told to refuse."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object, bool, int]] = []
        self.fail_next = 0
        self.saw_publish = asyncio.Event()

    async def publish(
        self,
        topic: str,
        payload: object,
        retain: bool = False,
        qos: int = 0,
    ) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("broker refused")
        self.published.append((topic, payload, retain, qos))
        self.saw_publish.set()


class _FakeConnection:
    """Stands in for ``mqtt_client`` — an async context manager over a client."""

    def __init__(self, client: _FakeClient) -> None:
        self._client = client
        self.connects = 0
        self.kwargs: dict = {}

    def __call__(
        self,
        _broker: str,
        _port: int,
        **kwargs: Any,
    ) -> "_FakeConnection":
        self.kwargs = kwargs
        return self

    async def __aenter__(self) -> _FakeClient:
        self.connects += 1
        return self._client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        # BaseException, not Exception: disconnect() cancels the drain loop from
        # inside this block, and CancelledError does not derive from Exception.
        return False


@pytest.fixture(name="broker")
def broker_fixture(monkeypatch: pytest.MonkeyPatch) -> _FakeConnection:
    """Replace the connection factory, leaving the publisher itself real."""
    client = _FakeClient()
    conn = _FakeConnection(client)
    monkeypatch.setattr("wactorz.core.mqtt.mqtt_client", conn)
    return conn


def _outbox(db_path: str | Path) -> list[tuple[Any]]:
    # `closing`, because a connection used as a context manager commits and
    # leaves the handle open — the same shape the publisher itself had.
    with closing(sqlite3.connect(db_path)) as db, db:
        return db.execute("SELECT topic, payload, retain, qos FROM outbox ORDER BY id").fetchall()


async def _settle(pub: MQTTPublisher, client: _FakeClient, expected: int = 1) -> None:
    """Wait for the drain loop to get through the queue."""
    for _ in range(200):
        if len(client.published) >= expected and pub.queue_depth == 0:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"drained {len(client.published)}, wanted {expected}")


class TestQoSRouting:
    """Callers pass a qos; some topics are too important, or too noisy, to trust it."""

    async def test_a_critical_topic_is_upgraded_to_qos_1(self, tmp_path: Path) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._init_db()
        pub._available = True

        await pub.publish("nodes/alpha/spawn", "{}", qos=0)

        # Spawn and stop instructions are not telemetry: losing one silently
        # leaves an operator's action with no effect and no report.
        assert _outbox(tmp_path / "outbox.db") == [("nodes/alpha/spawn", "{}", 0, 1)]

    async def test_telemetry_is_downgraded_and_never_persisted(self, tmp_path: Path) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._init_db()
        pub._available = True

        await pub.publish("agents/a1/heartbeat", "{}", qos=1)

        # A heartbeat is worthless by the time a reconnect would replay it, and
        # they arrive often enough to swamp the outbox.
        assert _outbox(tmp_path / "outbox.db") == []
        assert pub.queue_depth == 1

    async def test_telemetry_wins_when_a_topic_is_both(self, tmp_path: Path) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._init_db()
        pub._available = True

        # nodes/ is critical, /heartbeat is telemetry — the downgrade runs second.
        await pub.publish("nodes/alpha/heartbeat", "{}", qos=1)

        assert _outbox(tmp_path / "outbox.db") == []


class TestDurability:
    async def test_a_durable_message_is_persisted_before_it_is_queued(self, tmp_path: Path) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._init_db()
        pub._available = True

        await pub.publish("nodes/alpha/stop", "payload", qos=1)

        # On disk before anything is attempted: a crash between the two would
        # otherwise lose a message the caller was told had been accepted.
        assert _outbox(tmp_path / "outbox.db") == [("nodes/alpha/stop", "payload", 0, 1)]

    async def test_undelivered_messages_survive_the_process(self, tmp_path: Path) -> None:
        db = str(tmp_path / "outbox.db")
        first = MQTTPublisher(db_path=db)
        first._init_db()
        first._available = True
        await first.publish("nodes/alpha/spawn", "payload", qos=1)

        # A new publisher, as after a restart.
        second = MQTTPublisher(db_path=db)
        second._load_pending_from_db()

        assert second.queue_depth == 1
        topic, payload, _retain, qos, row_id = second._queue.get_nowait()
        assert (topic, payload, qos) == ("nodes/alpha/spawn", "payload", 1)
        assert row_id > 0

    async def test_a_delivered_message_is_dropped_from_the_outbox(
        self, tmp_path: Path, broker: _FakeConnection
    ) -> None:
        db = str(tmp_path / "outbox.db")
        pub = await MQTTPublisher.create("localhost", 1883, db_path=db)
        try:
            await pub.publish("nodes/alpha/spawn", "payload", qos=1)
            await _settle(pub, broker._client)

            # Left behind, it would be republished on every restart, forever.
            assert _outbox(db) == []
        finally:
            await pub.disconnect()

    async def test_a_bytes_payload_is_stored_as_text(self, tmp_path: Path) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._init_db()
        pub._available = True

        await pub.publish("nodes/alpha/spawn", b"raw bytes", qos=1)

        assert _outbox(tmp_path / "outbox.db") == [("nodes/alpha/spawn", "raw bytes", 0, 1)]

    async def test_an_unwritable_outbox_does_not_lose_the_message(self, tmp_path: Path) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "missing" / "outbox.db"))
        pub._available = True  # _init_db never ran, so there is no table

        await pub.publish("nodes/alpha/spawn", "payload", qos=1)

        # Degraded to best-effort rather than dropped: the message is still
        # queued for this process's lifetime.
        assert pub.queue_depth == 1


class TestABrokenOutbox:
    """A damaged outbox weakens the guarantee; it must not stop the publisher."""

    async def test_an_unreadable_outbox_starts_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "outbox.db"
        db.write_bytes(b"this is not a database")

        pub = MQTTPublisher(db_path=str(db))
        pub._load_pending_from_db()

        # Nothing to replay, but the publisher still comes up — refusing to
        # start would take the whole actor system down with it.
        assert pub.queue_depth == 0

    async def test_a_failed_delete_is_not_fatal(self, tmp_path: Path) -> None:
        db = tmp_path / "outbox.db"
        db.write_bytes(b"this is not a database")

        # The message was delivered; failing to tick it off means it is replayed
        # once more later, which is the harmless direction to fail in.
        MQTTPublisher(db_path=str(db))._delete_from_db(1)


class TestDelivery:
    async def test_publish_does_not_wait_for_the_broker(
        self, tmp_path: Path, broker: _FakeConnection
    ) -> None:
        broker._client.fail_next = 10_000  # the broker never accepts anything
        pub = await MQTTPublisher.create("localhost", 1883, db_path=str(tmp_path / "o.db"))
        try:
            await asyncio.wait_for(pub.publish("agents/a1/status", "{}"), timeout=0.5)
        finally:
            await pub.disconnect()

    async def test_a_message_the_broker_refused_is_kept(
        self, tmp_path: Path, broker: _FakeConnection
    ) -> None:
        broker._client.fail_next = 1
        pub = await MQTTPublisher.create("localhost", 1883, db_path=str(tmp_path / "o.db"))
        try:
            await pub.publish("nodes/alpha/spawn", "payload", qos=1)
            await _settle(pub, broker._client)

            # Retried on the next connection rather than dropped, and the
            # failure forced a reconnect rather than spinning on a dead socket.
            assert broker._client.published == [("nodes/alpha/spawn", "payload", False, 1)]
            assert broker.connects >= 2
        finally:
            await pub.disconnect()

    async def test_the_session_is_resumable(self, tmp_path: Path, broker: _FakeConnection) -> None:
        pub = await MQTTPublisher.create("localhost", 1883, db_path=str(tmp_path / "o.db"))
        try:
            await pub.publish("nodes/alpha/spawn", "payload", qos=1)
            await _settle(pub, broker._client)

            # A fixed identifier and a session the broker is asked to keep are
            # what let it hold QoS 1 messages for us across a disconnect. The
            # session names a lifetime: v3.1.1 offers none, so an install that
            # goes away would cost broker state for ever.
            assert broker.kwargs["clean_start"] is False
            assert broker.kwargs["properties"].SessionExpiryInterval > 0
            assert broker.kwargs["identifier"] == pub.client_id
        finally:
            await pub.disconnect()

    async def test_connected_reflects_the_link(
        self, tmp_path: Path, broker: _FakeConnection
    ) -> None:
        pub = await MQTTPublisher.create("localhost", 1883, db_path=str(tmp_path / "o.db"))
        try:
            await pub.publish("agents/a1/status", "{}")
            await _settle(pub, broker._client)
            assert pub.connected is True
        finally:
            await pub.disconnect()
        assert pub.connected is False


class TestWithoutABroker:
    async def test_publish_is_a_no_op_when_unavailable(self, tmp_path: Path) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))

        await pub.publish("nodes/alpha/spawn", "payload", qos=1)

        # Nothing queued and nothing written: create() failed, so there is no
        # drain loop that would ever empty either.
        assert pub.queue_depth == 0
        assert not (tmp_path / "outbox.db").exists()

    async def test_create_survives_an_outbox_it_cannot_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _explode(self) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(MQTTPublisher, "_init_db", _explode)
        pub = await MQTTPublisher.create("localhost", 1883, db_path=str(tmp_path / "o.db"))

        # A publisher that cannot start must not take the actor system with it.
        assert pub._available is False
        await pub.publish("nodes/alpha/spawn", "payload", qos=1)
        assert pub.queue_depth == 0

    async def test_disconnect_is_safe_before_create(self, tmp_path: Path) -> None:
        await MQTTPublisher(db_path=str(tmp_path / "o.db")).disconnect()

    async def test_a_broker_that_stays_down_is_reported_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _refuse(_broker: str, _port: int, **_kwargs: Any) -> None:
            raise ConnectionRefusedError("connection refused")

        monkeypatch.setattr("wactorz.core.mqtt.mqtt_client", _refuse)
        caplog.set_level(logging.DEBUG, logger="wactorz.core.mqtt_publisher")

        pub = await MQTTPublisher.create("nowhere", 1883, db_path=str(tmp_path / "o.db"))
        try:
            # Long enough for the first retry — the one that would repeat.
            await asyncio.sleep(1.2)
        finally:
            await pub.disconnect()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        # A broker that is down stays down for minutes. Repeating the same
        # warning every second buries everything else in the log.
        assert len(warnings) == 1
        assert any(r.levelno == logging.DEBUG for r in caplog.records)
