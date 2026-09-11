"""One message the publisher cannot send no longer holds up the rest.

The publisher sends one message at a time, in order, and a failed one goes
first after reconnecting. That is right for a broker that went away, and wrong
for a message that can never succeed: it was retried for ever, and every message
behind it — spawns, stops, migration acks — waited. A stored one came back after
a restart and stalled the queue again.

Seen against a real broker before this fix: a topic with a wildcard, which paho
refuses on this side, and an empty topic, which the broker answers by dropping
the connection. Now an unsendable topic is refused before it is queued, a message
paho refuses is dropped rather than retried, and one that keeps failing on a live
connection is dropped after `POISON_AFTER` attempts — counted per message, so a
flaky link failing different messages never adds up to a verdict.
"""

import asyncio
import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from wactorz.core import mqtt as core_mqtt
from wactorz.core import mqtt_publisher
from wactorz.core.mqtt_publisher import MQTTPublisher
from wactorz.core.topics import MAX_TOPIC_BYTES, publish_topic_error, topic_name_error


class _Client:
    """Publishes, refusing some topics the way paho or a broker would."""

    def __init__(
        self, refuse: dict[str, Exception] | None = None, fail_once: tuple[str, ...] = ()
    ) -> None:
        self.published: list[str] = []
        self.attempts: dict[str, int] = {}
        self.connections = 0
        self._refuse = refuse or {}
        self._fail_once = set(fail_once)

    async def publish(self, topic: str, payload: Any, **_kwargs: Any) -> None:
        self.attempts[topic] = self.attempts.get(topic, 0) + 1
        if topic in self._refuse:
            raise self._refuse[topic]
        if topic in self._fail_once:
            self._fail_once.discard(topic)
            raise RuntimeError("broker went away")
        self.published.append(topic)


@contextlib.asynccontextmanager
async def _fake_broker(client: _Client) -> Any:
    """Stands in for `mqtt_client`, so the *real* `_run` loop is what runs."""
    client.connections += 1
    yield client


async def _run_briefly(pub: MQTTPublisher, client: _Client, seconds: float = 0.3) -> None:
    """Drive the real `MQTTPublisher._run` against a fake broker, without backoff."""
    real_sleep = asyncio.sleep

    async def _no_backoff(delay: float, *a: Any, **kw: Any) -> Any:
        return await real_sleep(0 if delay >= 1 else delay, *a, **kw)

    # Patched on `core.mqtt`: `_run` imports `mqtt_client` from there, late.
    with (
        mock.patch.object(core_mqtt, "mqtt_client", lambda *a, **kw: _fake_broker(client)),
        mock.patch.object(mqtt_publisher.asyncio, "sleep", _no_backoff),
    ):
        task = asyncio.create_task(pub._run("localhost", 1883))
        await real_sleep(seconds)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture(name="pub")
def pub_fixture(tmp_path: Path) -> MQTTPublisher:
    pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
    pub._init_db()
    pub._available = True
    return pub


def _stored(pub: MQTTPublisher) -> list[str]:
    with sqlite3.connect(str(pub._db_path)) as db:
        return [row[0] for row in db.execute("SELECT topic FROM outbox ORDER BY id")]


def _queue_stored(pub: MQTTPublisher, topic: str) -> None:
    """A QoS 1 message as publish() leaves it: stored, then queued."""
    row_id = pub._save_to_db(topic, "x", False, 1)
    pub._queue.put_nowait((topic, "x", False, 1, row_id))


class TestTheTopicRules:
    # Named: pytest puts the test id in an environment variable, and Windows
    # refuses one longer than 32767 characters, which the long topic would be.
    @pytest.mark.parametrize(
        "topic",
        ["", "sensors/+/temp", "sensors/#", "a\x00b", "x" * (MAX_TOPIC_BYTES + 1)],
        ids=["empty", "plus", "hash", "nul", "too-long"],
    )
    def test_an_unsendable_topic_says_why(self, topic: str) -> None:
        assert publish_topic_error(topic)

    @pytest.mark.parametrize("topic", ["agents/by-name/weather/task", "nodes/rpi/spawn", "θέμα/α"])
    def test_an_ordinary_topic_passes(self, topic: str) -> None:
        assert publish_topic_error(topic) is None

    @pytest.mark.parametrize("name", ["c++ monitor", "all#", "", "   ", "a\x00b"])
    def test_a_name_that_cannot_be_a_topic_level_says_why(self, name: str) -> None:
        assert topic_name_error(name)

    @pytest.mark.parametrize("name", ["weather-agent", "rpi kitchen", "a/b"])
    def test_an_ordinary_name_passes(self, name: str) -> None:
        assert topic_name_error(name) is None


class TestAnUnsendableTopic:
    async def test_is_neither_queued_nor_stored(self, pub: MQTTPublisher) -> None:
        await pub.publish("sensors/+/temp", "x", qos=1)

        assert pub._queue.empty()
        assert _stored(pub) == []

    async def test_is_warned_about_once(
        self, pub: MQTTPublisher, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                await pub.publish("", "x")

        assert sum("refused to publish" in r.message for r in caplog.records) == 1


class TestAMessageTheClientRefuses:
    @pytest.mark.parametrize(
        "error", [ValueError("Publish topic cannot contain wildcards."), TypeError("payload")]
    )
    async def test_is_dropped_and_the_next_one_goes(
        self, pub: MQTTPublisher, error: Exception
    ) -> None:
        # Stored before this fix, say, and replayed: it never passed publish().
        _queue_stored(pub, "bad")
        _queue_stored(pub, "good")
        client = _Client(refuse={"bad": error})

        await _run_briefly(pub, client)

        assert client.published == ["good"]
        assert client.attempts["bad"] == 1
        assert _stored(pub) == []
        assert client.connections == 1, "a refused message is no reason to reconnect"


class TestAMessageThatKeepsFailing:
    async def test_is_dropped_after_its_attempts_and_the_next_one_goes(
        self, pub: MQTTPublisher, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(MQTTPublisher, "POISON_AFTER", 3)
        _queue_stored(pub, "poison")
        _queue_stored(pub, "good")
        client = _Client(refuse={"poison": RuntimeError("malformed packet")})

        await _run_briefly(pub, client)

        assert client.attempts["poison"] == 3
        assert client.published == ["good"]
        assert _stored(pub) == [], "a stored poison message would stall again after a restart"

    async def test_failures_of_different_messages_never_add_up(
        self, pub: MQTTPublisher, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A flaky link failing each message once must not look like poison.
        monkeypatch.setattr(MQTTPublisher, "POISON_AFTER", 2)
        for topic in ("a", "b", "c"):
            _queue_stored(pub, topic)
        client = _Client(fail_once=("a", "b", "c"))

        await _run_briefly(pub, client)

        assert client.published == ["a", "b", "c"]
