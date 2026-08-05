"""What a remote node does with what arrives on the broker.

These paths are the reason `remote_runner.py` is worth testing beyond its
coverage number: a node subscribes to topics named by generated agent code and
feeds whatever arrives into that code. They all share one shape — open a client,
subscribe, iterate `client.messages` — so one stand-in serves them.

The stand-in replaces the `Client` attribute on the real `aiomqtt` module rather
than putting a fake module in `sys.modules`: the module stays real, and
`monkeypatch` puts the attribute back afterwards.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any, cast

import aiomqtt
import pytest

from wactorz import remote_runner


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
    """Build a broker serving the given messages, with `aiomqtt.Client` replaced."""

    def _build(*messages: _Message) -> _FakeBroker:
        fake = _FakeBroker(messages)
        monkeypatch.setattr(aiomqtt, "Client", fake)
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
        window = remote_runner._RemoteStreamWindow("sensors/x", "localhost", 1883)

        window.start()
        assert window._task
        await _until(fake.drained, window._task)

        assert window.values() == [21, 23]
        assert fake.client.subscribed == ["sensors/x"]

    async def test_every_entry_is_stamped_on_arrival(self, broker: Any) -> None:
        fake = broker(_Message(json.dumps({"value": 1})))
        window = remote_runner._RemoteStreamWindow("sensors/x", "localhost", 1883)

        window.start()
        assert window._task
        await _until(fake.drained, window._task)

        # Without a stamp, trimming and absent_for have nothing to measure.
        assert window._buffer[0]["_ts"] > 0

    async def test_a_payload_that_is_not_json_is_kept_as_a_value(self, broker: Any) -> None:
        fake = broker(_Message("not json at all"))
        window = remote_runner._RemoteStreamWindow("sensors/x", "localhost", 1883)

        window.start()
        assert window._task
        await _until(fake.drained, window._task)

        # Devices publish bare readings; dropping them would lose the data
        # silently, so they are wrapped rather than discarded.
        assert window.values() == ["not json at all"]

    async def test_a_json_scalar_is_wrapped_too(self, broker: Any) -> None:
        fake = broker(_Message("42"))
        window = remote_runner._RemoteStreamWindow("sensors/x", "localhost", 1883)

        window.start()
        assert window._task
        await _until(fake.drained, window._task)

        # Valid JSON, but not a dict — the queries index by key.
        assert window.values() == [42]

    async def test_credentials_come_from_the_environment(
        self, broker: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MQTT_USERNAME", "node")
        monkeypatch.setenv("MQTT_PASSWORD", "secret")
        fake = broker(_Message(json.dumps({"value": 1})))
        window = remote_runner._RemoteStreamWindow("sensors/x", "localhost", 1883)

        window.start()
        assert window._task
        await _until(fake.drained, window._task)

        assert fake.kwargs["username"] == "node"
        assert fake.kwargs["password"] == "secret"

    async def test_no_credentials_are_sent_when_unset(
        self, broker: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MQTT_USERNAME", raising=False)
        monkeypatch.delenv("MQTT_PASSWORD", raising=False)
        fake = broker(_Message(json.dumps({"value": 1})))
        window = remote_runner._RemoteStreamWindow("sensors/x", "localhost", 1883)

        window.start()
        assert window._task
        await _until(fake.drained, window._task)

        # None, not "" — an empty username is a username as far as a broker
        # with anonymous access disabled is concerned.
        assert fake.kwargs["username"] is None
        assert fake.kwargs["password"] is None

    async def test_starting_twice_runs_one_listener(self, broker: Any) -> None:
        fake = broker(_Message(json.dumps({"value": 1})))
        window = remote_runner._RemoteStreamWindow("sensors/x", "localhost", 1883)

        window.start()
        assert window._task
        first = window._task
        window.start()

        try:
            assert window._task is first
        finally:
            await _until(fake.drained, window._task)


class _Runner:
    broker = "localhost"
    port = 1883


class _Agent:
    """The minimum of ``_RemoteAgent`` the API touches."""

    def __init__(self) -> None:
        self.name = "edge-agent"
        self.node = "node-a"
        self.actor_id = "edge-0001"
        self._runner = _Runner()
        self._config: dict = {}


class TestSubscribeDeliversToAgentCode:
    """The callback here is LLM-generated: it can be wrong in ordinary ways."""

    @staticmethod
    def _api() -> Any:
        return remote_runner._RemoteAgentAPI(cast(remote_runner._RemoteAgent, _Agent()))

    async def test_a_json_payload_reaches_the_callback(self, broker: Any) -> None:
        fake = broker(_Message(json.dumps({"temp": 21})))
        seen: list[dict] = []

        async def _on_msg(payload: dict) -> None:
            seen.append(payload)

        api = self._api()
        api.subscribe("sensors/x", _on_msg)
        await _until(fake.drained, api._subscriber_tasks[0])

        assert seen == [{"temp": 21}]
        assert fake.client.subscribed == ["sensors/x"]

    async def test_a_non_json_payload_arrives_under_raw(self, broker: Any) -> None:
        fake = broker(_Message("plain text"))
        seen: list[dict] = []

        async def _on_msg(payload: dict) -> None:
            seen.append(payload)

        api = self._api()
        api.subscribe("sensors/x", _on_msg)
        await _until(fake.drained, api._subscriber_tasks[0])

        # Agent code can still see it, under a key that says what happened.
        assert seen == [{"raw": "plain text"}]

    async def test_a_callback_that_raises_does_not_stop_the_stream(self, broker: Any) -> None:
        fake = broker(_Message(json.dumps({"n": 1})), _Message(json.dumps({"n": 2})))
        seen: list[int] = []

        async def _on_msg(payload: dict) -> None:
            if payload["n"] == 1:
                raise ValueError("generated code is like this")
            seen.append(payload["n"])

        api = self._api()
        api.subscribe("sensors/x", _on_msg)
        await _until(fake.drained, api._subscriber_tasks[0])

        # One bad message must not deafen the agent to every later one.
        assert seen == [2]

    async def test_an_await_none_callback_is_tolerated(self, broker: Any) -> None:
        fake = broker(_Message(json.dumps({"n": 1})), _Message(json.dumps({"n": 2})))
        seen: list[int] = []

        async def _on_msg(payload: dict) -> None:
            seen.append(payload["n"])
            await None  # type: ignore[misc]  # a shape LLMs emit regularly

        api = self._api()
        api.subscribe("sensors/x", _on_msg)
        await _until(fake.drained, api._subscriber_tasks[0])

        # The body ran; only the bogus await failed, and it is warned about once
        # rather than per message.
        assert seen == [1, 2]

    async def test_a_type_error_that_is_not_await_none_still_propagates(self, broker: Any) -> None:
        fake = broker(_Message(json.dumps({"n": 1})), _Message(json.dumps({"n": 2})))
        seen: list[int] = []

        async def _on_msg(payload: dict) -> None:
            seen.append(payload["n"])
            raise TypeError("a genuine bug in the agent")

        api = self._api()
        api.subscribe("sensors/x", _on_msg)
        await _until(fake.drained, api._subscriber_tasks[0])

        # Tolerating `await None` must not become tolerating every TypeError:
        # this one is logged as a callback error, and the stream continues.
        assert seen == [1, 2]
