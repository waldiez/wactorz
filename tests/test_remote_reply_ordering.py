"""A remote agent that answers immediately must still be heard.

Both delegation paths published the task and *then* started subscribing to the
reply topic. A node that answered inside that gap answered into nothing: MQTT
drops a message with no subscriber, so the reply vanished and the caller waited
out its whole timeout for work that had in fact succeeded — which reads as a
flaky node rather than a bug here, and is why it survived so long.

The ordering is asserted directly rather than through a race. A test that
publishes and hopes to lose the race would pass on a slow machine and prove
nothing; recording the order the two operations happen in proves the property
every time.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from wactorz.agents.main.actor import MainActor


class _Client:
    """An aiomqtt client that records when it was subscribed."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.messages = _never()

    async def subscribe(self, topic: str, qos: int = 0, **_kwargs: Any) -> None:
        self._log.append(f"subscribe {topic}")

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def _never() -> Any:
    """A message stream that never yields — the reply arrives by other means."""
    await asyncio.Event().wait()
    yield  # pragma: no cover - unreachable, and required to make this a generator


@pytest.fixture(name="main")
def main_fixture(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A MainActor wired to a fake broker that records the order of operations."""
    actor = MainActor(llm_provider=None, name="main", persistence_dir=str(tmp_path))
    order: list[str] = []

    def _client(_host: str, _port: int, **_kw: Any) -> _Client:
        return _Client(order)

    monkeypatch.setattr("wactorz.agents.main.delegation.mqtt_client", _client)
    actor._mqtt_publish = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
        side_effect=lambda topic, *_a, **_kw: order.append(f"publish {topic}")
    )
    actor.order = order  # pyright: ignore[reportAttributeAccessIssue]
    return actor


class TestTheReplyChannel:
    async def test_it_subscribes_before_the_caller_publishes(self, main: Any) -> None:
        async with main.delegation._reply_topic() as (topic, _future):
            await main._mqtt_publish("agents/by-name/rpi/task", {"_reply_topic": topic})

        assert [step.split()[0] for step in main.order] == ["subscribe", "publish"]

    async def test_the_topic_it_yields_is_the_one_it_subscribed(self, main: Any) -> None:
        # Or the reply lands somewhere nobody is listening, which is the same
        # failure wearing a different shape.
        async with main.delegation._reply_topic() as (topic, _future):
            pass

        assert main.order == [f"subscribe {topic}"]

    async def test_a_reply_that_arrives_resolves_the_caller(self, main: Any) -> None:
        async with main.delegation._reply_topic() as (_topic, future):
            future.set_result({"result": "done"})

            assert await asyncio.wait_for(future, timeout=1) == {"result": "done"}

    async def test_the_topic_is_forgotten_afterwards(self, main: Any) -> None:
        # It is keyed per call, so leaving them behind grows the dict for the
        # life of the process.
        async with main.delegation._reply_topic() as (topic, _future):
            assert topic in main._result_futures

        assert topic not in main._result_futures

    async def test_a_broker_that_never_subscribes_does_not_hang_the_caller(
        self, main: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Publishing anyway is the deliberate choice: the task still gets done,
        # and losing the answer beats losing the work. Waiting forever for a
        # subscription that is not coming would do neither.
        monkeypatch.setattr("wactorz.agents.main.delegation.SUBSCRIBE_TIMEOUT_S", 0.05)

        def _hangs(_host: str, _port: int, **_kw: Any) -> Any:
            raise ConnectionRefusedError("no broker")

        monkeypatch.setattr("wactorz.agents.main.delegation.mqtt_client", _hangs)

        async with main.delegation._reply_topic() as (topic, future):
            assert topic  # reached at all, rather than blocking until the timeout

        # The listener puts the connection error on the future, and in the real
        # caller `wait_for` collects it. Collected here too, or asyncio reports
        # an exception nobody retrieved once the test has finished.
        with pytest.raises(ConnectionRefusedError):
            await future
