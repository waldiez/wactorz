"""QoS 1 that does not fit in memory waits on disk and comes back; old commands do not.

A QoS 1 message published while the queue is full is already in the SQLite
outbox, so it can wait there. What it must not do is wait for a restart: on a
process that stays up it would never be sent. The drain loop reloads it once the
queue has room, and every QoS 1 message published meanwhile waits behind it, so
the order they were published in survives.

A command for a node is different from other messages: it is an instruction for
the moment it was given, and a node still accepts it hours later. So a stored one
expires in minutes rather than days, and is not replayed after a restart.
"""

import asyncio
import contextlib
import sqlite3
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from wactorz.core import mqtt as core_mqtt
from wactorz.core import mqtt_publisher
from wactorz.core.mqtt_publisher import MQTTPublisher


class _Client:
    """Records what it publishes; can fail one topic once, dropping the link."""

    def __init__(self, fail_once_on: str | None = None) -> None:
        self.published: list[str] = []
        self.connections = 0
        self._fail_on = fail_once_on

    async def publish(self, topic: str, payload: Any, **_kwargs: Any) -> None:
        if topic == self._fail_on:
            self._fail_on = None
            raise RuntimeError("broker went away")
        self.published.append(topic)


@contextlib.asynccontextmanager
async def _fake_broker(client: _Client) -> AsyncIterator[_Client]:
    """Stands in for `mqtt_client`, so the real `_run` loop is what runs."""
    client.connections += 1
    yield client


async def _drain(pub: MQTTPublisher, client: _Client, expected: int) -> None:
    """Run the real drain loop until ``expected`` messages are out, or fail."""
    # Patched on `core.mqtt`: `_run` imports it function-locally.
    with mock.patch.object(core_mqtt, "mqtt_client", lambda *a, **kw: _fake_broker(client)):
        task = asyncio.create_task(pub._run("localhost", 1883))
        try:

            async def sent() -> None:
                while len(client.published) < expected:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(sent(), 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _publisher(tmp_path: Path, cap: int = 3) -> MQTTPublisher:
    pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
    pub._init_db()
    pub._available = True
    pub._queue = asyncio.Queue(maxsize=cap)
    pub.MAX_QUEUED = cap  # type: ignore[misc]
    return pub


def _stored_topics(pub: MQTTPublisher) -> list[str]:
    with sqlite3.connect(str(pub._db_path)) as db:
        return [row[0] for row in db.execute("SELECT topic FROM outbox ORDER BY id")]


class TestSpilling:
    async def test_what_does_not_fit_waits_on_disk(self, tmp_path: Path) -> None:
        pub = _publisher(tmp_path)

        for i in range(5):
            await pub.publish(f"agents/by-name/m{i}", "x", qos=1)

        assert pub.queue_depth == 3
        assert len(pub._spilled) == 2
        assert _stored_topics(pub) == [f"agents/by-name/m{i}" for i in range(5)]
        pub._close_db()

    async def test_later_messages_wait_behind_the_spilled_ones(self, tmp_path: Path) -> None:
        # Or a message published after a spill would jump the queue as soon as
        # one slot opened, ahead of the ones waiting on disk.
        pub = _publisher(tmp_path)
        for i in range(4):
            await pub.publish(f"agents/by-name/m{i}", "x", qos=1)
        pub._queue.get_nowait()
        pub._queue.task_done()

        await pub.publish("agents/by-name/late", "x", qos=1)

        assert pub.queue_depth == 2
        assert len(pub._spilled) == 2
        pub._close_db()

    async def test_telemetry_is_still_queued_in_memory(self, tmp_path: Path) -> None:
        pub = _publisher(tmp_path)
        for i in range(4):
            await pub.publish(f"agents/by-name/m{i}", "x", qos=1)
        pub._queue.get_nowait()
        pub._queue.task_done()

        await pub.publish("agents/a/heartbeat", "x")

        assert pub.queue_depth == 3
        pub._close_db()


class TestRefilling:
    async def test_a_live_connection_sends_them_all_in_order(self, tmp_path: Path) -> None:
        # No reconnect happens here: the refill has to come from draining.
        pub = _publisher(tmp_path)
        topics = [f"agents/by-name/m{i}" for i in range(10)]
        for topic in topics:
            await pub.publish(topic, "x", qos=1)
        client = _Client()

        await _drain(pub, client, expected=len(topics))

        assert client.published == topics
        assert not pub._spilled
        assert _stored_topics(pub) == []
        pub._close_db()

    async def test_a_row_expired_meanwhile_is_skipped(self, tmp_path: Path) -> None:
        pub = _publisher(tmp_path, cap=1)
        await pub.publish("agents/by-name/first", "x", qos=1)
        await pub.publish("agents/by-name/gone", "x", qos=1)
        await pub.publish("agents/by-name/last", "x", qos=1)
        with sqlite3.connect(str(pub._db_path)) as db:
            db.execute("DELETE FROM outbox WHERE topic = 'agents/by-name/gone'")
        client = _Client()

        await _drain(pub, client, expected=2)

        assert client.published == ["agents/by-name/first", "agents/by-name/last"]
        pub._close_db()

    async def test_a_failed_read_keeps_them_waiting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pub = _publisher(tmp_path, cap=1)
        await pub.publish("agents/by-name/first", "x", qos=1)
        await pub.publish("agents/by-name/second", "x", qos=1)
        pub._queue.get_nowait()
        pub._queue.task_done()
        monkeypatch.setattr(pub, "_read_rows", lambda _ids: None)

        await pub._refill()

        assert len(pub._spilled) == 1
        assert pub.queue_depth == 0
        pub._close_db()

    async def test_after_a_failed_read_the_loop_tries_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Waiting on an empty queue instead would strand the backlog until
        # something else happened to be published.
        pub = _publisher(tmp_path, cap=1)
        await pub.publish("agents/by-name/first", "x", qos=1)
        await pub.publish("agents/by-name/second", "x", qos=1)
        real = pub._read_rows
        calls: list[int] = []

        def flaky(ids: list[int]) -> list[tuple] | None:
            calls.append(len(ids))
            return None if len(calls) == 1 else real(ids)

        monkeypatch.setattr(pub, "_read_rows", flaky)
        client = _Client()

        await _drain(pub, client, expected=2)

        assert client.published == ["agents/by-name/first", "agents/by-name/second"]
        assert len(calls) == 2
        pub._close_db()

    def test_a_refill_waits_for_room_for_a_batch(self, tmp_path: Path) -> None:
        # One row read back per message sent would put a query on every publish.
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._spilled.extend(range(1, 2000))
        for _ in range(pub.MAX_QUEUED - pub.REFILL_BATCH + 1):
            pub._queue.put_nowait(("t", "x", False, 0, -1, None))

        assert pub._refill_due() is False
        pub._queue.get_nowait()
        assert pub._refill_due() is True


class TestAPublishDuringARefill:
    """The refill awaits its read; a publish can land while it does."""

    async def test_it_neither_overflows_nor_overtakes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pub = _publisher(tmp_path, cap=2)
        for i in range(3):
            await pub.publish(f"agents/by-name/m{i}", "x", qos=1)
        for _ in range(2):
            pub._queue.get_nowait()
            pub._queue.task_done()
        reading = threading.Event()
        release = threading.Event()
        real = pub._read_rows

        def slow(ids: list[int]) -> list[tuple] | None:
            reading.set()
            release.wait(5)
            return real(ids)

        monkeypatch.setattr(pub, "_read_rows", slow)
        refill = asyncio.create_task(pub._refill())
        await asyncio.to_thread(reading.wait, 5)

        # The room the refill measured is taken, and a newer QoS 1 arrives.
        await pub.publish("agents/a/heartbeat", "x")
        await pub.publish("agents/b/heartbeat", "x")
        await pub.publish("agents/by-name/newer", "x", qos=1)
        release.set()
        assert await refill is True

        queued = [entry[0] for entry in pub._queue._queue]  # type: ignore[attr-defined]
        # Telemetry gave way to the spilled message; the newer one still waits
        # behind it, on disk.
        assert queued == ["agents/b/heartbeat", "agents/by-name/m2"]
        assert len(pub._spilled) == 1
        pub._close_db()


class TestAcrossAReconnect:
    async def test_the_backlog_is_sent_in_order_after_the_link_drops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pub = _publisher(tmp_path)
        topics = [f"agents/by-name/m{i}" for i in range(8)]
        for topic in topics:
            await pub.publish(topic, "x", qos=1)
        client = _Client(fail_once_on="agents/by-name/m2")
        real_sleep = asyncio.sleep

        async def no_backoff(delay: float, *args: Any, **kwargs: Any) -> Any:
            return await real_sleep(0 if delay >= 1 else delay, *args, **kwargs)

        monkeypatch.setattr(mqtt_publisher.asyncio, "sleep", no_backoff)

        await _drain(pub, client, expected=len(topics))

        assert client.published == topics
        assert client.connections == 2
        assert _stored_topics(pub) == []
        pub._close_db()


class TestWithoutAnOutbox:
    async def test_qos_1_is_delivered_from_memory(self, tmp_path: Path) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._available = True
        pub._memory_only = True
        client = _Client()

        await pub.publish("nodes/rpi/spawn", "x", qos=1)
        await _drain(pub, client, expected=1)

        assert client.published == ["nodes/rpi/spawn"]
        assert not (tmp_path / "outbox.db").exists()

    async def test_an_outbox_that_opens_later_is_used(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A full disk that frees, a mount that arrives: durability comes back
        # without a restart, and what an earlier run left is replayed.
        earlier = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        earlier._init_db()
        earlier._save_to_db("agents/by-name/left", "x", False, 1)
        earlier._close_db()
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._available = True
        pub._memory_only = True

        with caplog.at_level("WARNING"):
            await pub._reopen()
        await pub.publish("agents/by-name/new", "x", qos=1)

        assert pub._memory_only is False
        assert "open again" in caplog.text
        assert [entry[0] for entry in pub._queue._queue] == [  # type: ignore[attr-defined]
            "agents/by-name/left",
            "agents/by-name/new",
        ]
        assert _stored_topics(pub) == ["agents/by-name/left", "agents/by-name/new"]
        pub._close_db()

    async def test_one_that_still_cannot_open_stays_in_memory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._memory_only = True

        def refuse() -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(pub, "_init_db", refuse)

        await pub._reopen()

        assert pub._memory_only is True
        assert pub._db is None


class TestCommandExpiry:
    @staticmethod
    def _store(pub: MQTTPublisher, topic: str, age_s: float, retain: bool = False) -> None:
        with sqlite3.connect(str(pub._db_path)) as db:
            db.execute(
                "INSERT INTO outbox (topic, payload, retain, qos, ts) VALUES (?, '{}', ?, 1, ?)",
                (topic, int(retain), time.time() - age_s),
            )

    @pytest.mark.parametrize("days", [7.0, 0.0])
    def test_an_old_command_is_not_replayed(self, tmp_path: Path, days: float) -> None:
        # Whatever the setting for everything else: a spawn from an hour ago
        # would undo what has happened since, and the node would accept it.
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"), dead_letter_days=days)
        pub._init_db()
        self._store(pub, "nodes/rpi/spawn", MQTTPublisher.COMMAND_EXPIRY_S + 60)
        self._store(pub, "nodes/rpi/stop", 5)

        pub._expire()

        assert _stored_topics(pub) == ["nodes/rpi/stop"]
        pub._close_db()

    def test_a_retained_desired_state_keeps_the_ordinary_expiry(self, tmp_path: Path) -> None:
        # State, not a command: dropping it would leave an older copy at the broker.
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._init_db()
        self._store(pub, "nodes/rpi/desired_state", 3600, retain=True)

        pub._expire()

        assert _stored_topics(pub) == ["nodes/rpi/desired_state"]
        pub._close_db()

    def test_an_old_task_for_an_agent_is_not_replayed(self, tmp_path: Path) -> None:
        # The rest of the control plane: a task from an hour ago is not one to do now.
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._init_db()
        self._store(pub, "agents/by-name/lights", MQTTPublisher.COMMAND_EXPIRY_S + 60)

        pub._expire()

        assert _stored_topics(pub) == []
        pub._close_db()

    def test_other_messages_keep_the_ordinary_expiry(self, tmp_path: Path) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._init_db()
        self._store(pub, "custom/sensor/reading", 3600)

        pub._expire()

        assert _stored_topics(pub) == ["custom/sensor/reading"]
        pub._close_db()

    async def test_a_startup_that_cannot_expire_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._init_db()
        monkeypatch.setattr(pub, "_expire", lambda: False)

        with caplog.at_level("WARNING"):
            await pub._replay_stored()

        assert "expired" in caplog.text and "replayed" in caplog.text
        pub._close_db()

    def test_the_expiry_is_named_in_the_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._init_db()
        self._store(pub, "nodes/rpi/migrate", MQTTPublisher.COMMAND_EXPIRY_S + 60)

        with caplog.at_level("WARNING"):
            pub._expire()

        assert "nodes/rpi/migrate" in caplog.text
        assert "minutes" in caplog.text
        pub._close_db()
