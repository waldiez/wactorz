"""What a node does with what arrives on the broker.

A node subscribes to topics named by generated agent code and feeds whatever
arrives into that code, which is worth covering with a broker in the loop rather
than at the seam below it. The paths all share one shape — open a client,
subscribe, iterate `client.messages` — so one stand-in serves them.

They run the same window and the same subscription hub main runs; what is being
checked is that a node reaches them correctly, credentials and all.

The stand-in replaces `mqtt_client`, the factory every connection in the package
goes through, wherever a module bound the name. That is the seam `conftest`'s
`_no_ambient_broker` uses to refuse real connections, and it says a test driving
one substitutes its own factory and wins -- which is what this does.
"""

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic.api import AgentAPI
from wactorz.core import mqtt as core_mqtt
from wactorz.core.topic_bus import StreamWindow
from wactorz.node.agent import NodeAgent
from wactorz.node.runner import NodeRunner


class _Message:
    """An MQTT message as the code reads it: `.payload.decode()`."""

    def __init__(self, payload: bytes | str, topic: str = "sensors/x") -> None:
        self.payload = payload if isinstance(payload, bytes) else payload.encode()
        self.topic = topic


class _FakeClient:
    """A broker that hands over a fixed sequence, then holds the connection open.

    Holding it open rather than ending the iteration matters: every caller wraps
    the loop in `while True`, so a stream that finishes means an immediate
    reconnect and the test races the retry instead of observing the messages.
    """

    def __init__(self, messages: Iterable[_Message], drained: asyncio.Event) -> None:
        self._messages = list(messages)
        self._drained = drained
        self.subscribed: list[str] = []
        self.published: list[tuple[str, Any]] = []

    async def subscribe(self, topic: str, **_kwargs: Any) -> None:
        self.subscribed.append(topic)

    async def publish(self, topic: str, payload: Any = None, **_kwargs: Any) -> None:
        self.published.append((topic, payload))

    @property
    def messages(self) -> AsyncIterator[_Message]:
        """An async iterable, as on the real client — not a coroutine."""
        return self._stream()

    async def _stream(self) -> AsyncIterator[_Message]:
        for message in self._messages:
            yield message
        self._drained.set()
        await asyncio.Event().wait()  # stay connected, as a real broker would


class _FakeBroker:
    """Callable stand-in for `aiomqtt.Client`, usable as an async context manager."""

    def __init__(self, messages: Iterable[_Message]) -> None:
        self.drained = asyncio.Event()
        self.client = _FakeClient(messages, self.drained)
        self.connects = 0
        self.kwargs: dict = {}

    def __call__(self, host: str, port: int, **kwargs: Any) -> "_FakeBroker":
        self.host, self.port, self.kwargs = host, port, kwargs
        return self

    async def __aenter__(self) -> _FakeClient:
        self.connects += 1
        return self.client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


@pytest.fixture(name="broker")
def broker_fixture(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _FakeBroker]:
    """Build a broker serving the given messages, with the factory replaced.

    Every module that imported the name is patched, not only the one that
    defines it: a dozen do `from ..core.mqtt import mqtt_client`, which binds
    the function into their own namespace.
    """

    def _build(*messages: _Message) -> _FakeBroker:
        fake = _FakeBroker(messages)
        monkeypatch.setattr(core_mqtt, "mqtt_client", fake)
        for module in list(sys.modules.values()):
            bound = getattr(module, "mqtt_client", None)
            # Only the refusal `conftest` installed: a module holding anything
            # else is one another fixture is already driving.
            if module is not core_mqtt and getattr(bound, "__module__", "") == "tests.conftest":
                monkeypatch.setattr(module, "mqtt_client", fake)
        return fake

    return _build


async def _until(event: asyncio.Event, task: asyncio.Task, timeout: float = 2.0) -> None:
    """Wait for the broker to be drained, then stop the task under test."""
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        await asyncio.sleep(0)  # let the final message be processed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class TestStreamWindowListener:
    async def test_json_payloads_land_in_the_buffer(self, broker: Any) -> None:
        fake = broker(_Message(json.dumps({"value": 21})), _Message(json.dumps({"value": 23})))
        window = StreamWindow("sensors/x")

        window.start("localhost", 1883)
        assert window._task
        await _until(fake.drained, window._task)

        assert window.values() == [21, 23]
        assert fake.client.subscribed == ["sensors/x"]

    async def test_every_entry_is_stamped_on_arrival(self, broker: Any) -> None:
        fake = broker(_Message(json.dumps({"value": 1})))
        window = StreamWindow("sensors/x")

        window.start("localhost", 1883)
        assert window._task
        await _until(fake.drained, window._task)

        # Without a stamp, trimming and absent_for have nothing to measure.
        assert window._buffer[0]["_ts"] > 0

    async def test_a_payload_that_is_not_json_is_kept_as_a_value(self, broker: Any) -> None:
        fake = broker(_Message("not json at all"))
        window = StreamWindow("sensors/x")

        window.start("localhost", 1883)
        assert window._task
        await _until(fake.drained, window._task)

        # Devices publish bare readings; dropping them would lose the data
        # silently, so they are wrapped rather than discarded.
        assert window.values() == ["not json at all"]

    async def test_a_json_scalar_is_wrapped_too(self, broker: Any) -> None:
        fake = broker(_Message("42"))
        window = StreamWindow("sensors/x")

        window.start("localhost", 1883)
        assert window._task
        await _until(fake.drained, window._task)

        # Valid JSON, but not a dict — the queries index by key.
        assert window.values() == [42]

    async def test_it_dials_the_broker_it_was_given(self, broker: Any) -> None:
        # Credentials and TLS are the factory's business, not this listener's --
        # see `test_mqtt_tls.py::TestTheServerFactory`. What matters here is
        # that the listener goes through it rather than around it, which is
        # also what keeps a test from opening a real connection.
        fake = broker(_Message(json.dumps({"value": 1})))
        window = StreamWindow("sensors/x")

        window.start("broker.lan", 8883)
        assert window._task
        await _until(fake.drained, window._task)

        assert (fake.host, fake.port) == ("broker.lan", 8883)

    async def test_starting_twice_runs_one_listener(self, broker: Any) -> None:
        fake = broker(_Message(json.dumps({"value": 1})))
        window = StreamWindow("sensors/x")

        window.start("localhost", 1883)
        assert window._task
        first = window._task
        window.start("localhost", 1883)

        try:
            assert window._task is first
        finally:
            await _until(fake.drained, window._task)


@pytest.fixture(name="api")
def api_fixture(tmp_path: Path) -> AgentAPI:
    """The API a node agent hands generated code, on a runner going nowhere."""
    runner = NodeRunner("localhost", 1883, "node-a", state_dir=str(tmp_path))
    return NodeAgent({"name": "edge-agent", "code": ""}, runner)._api


def _hub_task(api: AgentAPI) -> Any:
    """The one connection every subscription on this agent shares."""
    (task,) = [t for t in api._actor._tasks if not t.done()]
    return task


class TestSubscribeDeliversToAgentCode:
    """The callback here is LLM-generated: it can be wrong in ordinary ways."""

    async def test_a_json_payload_reaches_the_callback(self, broker: Any, api: AgentAPI) -> None:
        fake = broker(_Message(json.dumps({"temp": 21})))
        seen: list[dict] = []

        async def _on_msg(payload: dict) -> None:
            seen.append(payload)

        api.subscribe("sensors/x", _on_msg)
        await _until(fake.drained, _hub_task(api))

        assert seen == [{"temp": 21}]
        assert fake.client.subscribed == ["sensors/x"]

    async def test_a_non_json_payload_arrives_under_raw(self, broker: Any, api: AgentAPI) -> None:
        fake = broker(_Message("plain text"))
        seen: list[dict] = []

        async def _on_msg(payload: dict) -> None:
            seen.append(payload)

        api.subscribe("sensors/x", _on_msg)
        await _until(fake.drained, _hub_task(api))

        # Agent code can still see it, under a key that says what happened.
        assert seen == [{"raw": "plain text"}]

    async def test_a_callback_that_raises_does_not_stop_the_stream(
        self, broker: Any, api: AgentAPI
    ) -> None:
        fake = broker(_Message(json.dumps({"n": 1})), _Message(json.dumps({"n": 2})))
        seen: list[int] = []

        async def _on_msg(payload: dict) -> None:
            if payload["n"] == 1:
                raise ValueError("generated code is like this")
            seen.append(payload["n"])

        api.subscribe("sensors/x", _on_msg)
        await _until(fake.drained, _hub_task(api))

        # One bad message must not deafen the agent to every later one.
        assert seen == [2]

    async def test_an_await_none_callback_is_tolerated(self, broker: Any, api: AgentAPI) -> None:
        fake = broker(_Message(json.dumps({"n": 1})), _Message(json.dumps({"n": 2})))
        seen: list[int] = []

        async def _on_msg(payload: dict) -> None:
            seen.append(payload["n"])
            await None  # type: ignore[misc]  # a shape LLMs emit regularly

        api.subscribe("sensors/x", _on_msg)
        await _until(fake.drained, _hub_task(api))

        # The body ran; only the bogus await failed, and it is warned about once
        # rather than per message.
        assert seen == [1, 2]

    async def test_a_type_error_that_is_not_await_none_still_propagates(
        self, broker: Any, api: AgentAPI
    ) -> None:
        fake = broker(_Message(json.dumps({"n": 1})), _Message(json.dumps({"n": 2})))
        seen: list[int] = []

        async def _on_msg(payload: dict) -> None:
            seen.append(payload["n"])
            raise TypeError("a genuine bug in the agent")

        api.subscribe("sensors/x", _on_msg)
        await _until(fake.drained, _hub_task(api))

        # Tolerating `await None` must not become tolerating every TypeError:
        # this one is logged as a callback error, and the stream continues.
        assert seen == [1, 2]
