"""A message whose publish failed is retried before anything queued behind it.

The old code put it back with `await self._queue.put(item)` under a comment
saying "put back at front of queue". `asyncio.Queue.put` appends to the *tail*,
so a failed message came back out after every message produced during the
outage — an agent's ordered updates arrived out of order, and the comment said
the opposite of what the line did.
"""

import asyncio
import contextlib
from typing import Any
from unittest import mock

import pytest

from wactorz.core import mqtt as core_mqtt
from wactorz.core import mqtt_publisher
from wactorz.core.mqtt_publisher import MQTTPublisher


class _Client:
    """Publishes, failing the first attempt at a nominated topic."""

    def __init__(self, fail_once_on: str | None = None) -> None:
        self.published: list[str] = []
        self._fail_on = fail_once_on

    async def publish(self, topic: str, payload: Any, **_kwargs: Any) -> None:
        if topic == self._fail_on:
            self._fail_on = None
            raise RuntimeError("broker went away")
        self.published.append(topic)


@contextlib.asynccontextmanager
async def _fake_broker(client: "_Client", **_kwargs: Any) -> Any:
    """Stands in for `mqtt_client`, so the *real* `_run` loop is what runs."""
    yield client


async def _run_briefly(pub: MQTTPublisher, client: "_Client", seconds: float = 0.25) -> None:
    """Drive `MQTTPublisher._run` against a fake broker for a moment.

    The real loop, not a re-implementation of it. An earlier version of this
    file reproduced the retry logic in the test helper, so it passed against the
    old tail-requeue too — a test that cannot fail.
    """
    # Patched on `core.mqtt`, not on `mqtt_publisher`: `_run` imports it
    # function-locally to avoid an import cycle, so the name never exists as an
    # attribute of the module under test.
    with mock.patch.object(
        core_mqtt,
        "mqtt_client",
        lambda *a, **kw: _fake_broker(client),
    ):
        # The failure path reconnects behind a 1s backoff, which would outlast
        # any sane test window — collapse the sleep so the retry happens now.
        real_sleep = asyncio.sleep

        async def _no_backoff(delay: float, *a: Any, **kw: Any) -> Any:
            return await real_sleep(0 if delay >= 1 else delay, *a, **kw)

        with mock.patch.object(mqtt_publisher.asyncio, "sleep", _no_backoff):
            task = asyncio.create_task(pub._run("localhost", 1883))
            await real_sleep(seconds)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.fixture(name="publisher")
def publisher_fixture(tmp_path: Any) -> MQTTPublisher:
    return MQTTPublisher(db_path=str(tmp_path / "outbox.db"))


class TestOrderAcrossAFailure:
    async def test_the_failed_message_goes_first_not_last(self, publisher: MQTTPublisher) -> None:
        for topic in ("first", "second", "third"):
            publisher._queue.put_nowait((topic, b"x", False, 0, -1))
        client = _Client(fail_once_on="first")

        await _run_briefly(publisher, client)

        # "first" failed, then was retried ahead of the messages behind it.
        assert client.published == ["first", "second", "third"]

    async def test_nothing_is_lost_when_the_publish_fails(self, publisher: MQTTPublisher) -> None:
        publisher._queue.put_nowait(("only", b"x", False, 0, -1))
        client = _Client(fail_once_on="only")

        await _run_briefly(publisher, client)

        assert client.published == ["only"]

    async def test_a_clean_run_keeps_its_order(self, publisher: MQTTPublisher) -> None:
        for topic in ("a", "b", "c"):
            publisher._queue.put_nowait((topic, b"x", False, 0, -1))
        client = _Client()

        await _run_briefly(publisher, client)

        # The order, not just the empty slot: asserting only `_retry is None`
        # would pass on shuffled delivery, which is the thing under test.
        assert client.published == ["a", "b", "c"]
        assert publisher._retry is None

    async def test_the_retry_slot_is_cleared_once_it_succeeds(
        self, publisher: MQTTPublisher
    ) -> None:
        # A slot left full would replay the same message on every reconnect.
        publisher._queue.put_nowait(("only", b"x", False, 0, -1))

        await _run_briefly(publisher, _Client(fail_once_on="only"))

        assert publisher._retry is None
