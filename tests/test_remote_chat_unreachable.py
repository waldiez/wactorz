"""A chat to an agent on a node, when there is no broker to carry it.

The agent is somewhere only the broker reaches. The user is told at once that it
could not be reached, and the log says so as it says every other consequence of
the outage: a warning with the reason. It is not a fault in the server, so there
is no traceback of the code that found the broker gone.
"""

import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import aiomqtt
import pytest
from aiomqtt import MqttError

from wactorz.core.addresses import Address, Resolution
from wactorz.web import chat, runtime


class _NoBroker:
    """What stands in for the broker connection: it cannot be opened."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def __aenter__(self) -> Any:
        raise self.failure

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture(name="said")
def said_fixture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Route a chat to `@counter`, which a node called `edge` is running."""
    main = SimpleNamespace(
        _mqtt_broker="broker.invalid",
        _mqtt_port=1883,
        # A node that signs, so the task is sent.
        nodes=SimpleNamespace(refuses_tasks=lambda _node: None),
        # No other node runs the name, so the task can go.
        task_refusal=lambda _address: None,
        task_topic_for=lambda address: f"agents/at/{address.node}/{address.name}/task",
    )
    registry = SimpleNamespace(find_by_name=lambda _name: None)
    monkeypatch.setattr(runtime, "registry", registry)
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: main)
    monkeypatch.setattr(
        chat, "resolution_of", lambda name: Resolution(name, (Address("counter", "edge"),))
    )
    return []


async def _ask(said: list[str]) -> None:
    async def reply(text: str) -> None:
        said.append(text)

    await chat.route_chat("@counter four", reply)


@pytest.mark.parametrize(
    "failure",
    [ConnectionRefusedError(111, "Connection refused"), MqttError("Disconnected")],
    ids=["nothing is listening", "the connection was lost"],
)
async def test_the_user_is_told_and_the_log_has_a_warning_without_a_traceback(
    said: list[str],
    failure: Exception,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(chat, "mqtt_client", lambda *_args, **_kw: _NoBroker(failure))
    caplog.set_level(logging.DEBUG, logger="wactorz.web.chat")

    await _ask(said)

    assert said == [f"[error] Could not reach @counter on edge: {failure}"]
    (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "Could not reach @counter on edge" in record.getMessage()


async def test_a_fault_that_is_not_an_outage_is_still_logged_as_one(
    said: list[str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failure = KeyError("a bug in the routing")
    monkeypatch.setattr(chat, "mqtt_client", lambda *_args, **_kw: _NoBroker(failure))
    caplog.set_level(logging.DEBUG, logger="wactorz.web.chat")

    await _ask(said)

    assert len(said) == 1
    (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


async def test_the_reply_is_read_over_mqtt_5(
    said: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The node's signature travels in user properties, which an MQTT 3.1.1
    # connection never receives: every reply would read as unsigned.
    connected: list[dict[str, Any]] = []

    def _connect(*_args: Any, **kwargs: Any) -> _NoBroker:
        connected.append(kwargs)
        return _NoBroker(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(chat, "mqtt_client", _connect)

    await _ask(said)

    assert connected[0]["protocol"] == aiomqtt.ProtocolVersion.V5


class _QuietBroker:
    """A connection that subscribes and then hears nothing."""

    async def __aenter__(self) -> "_QuietBroker":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, _topic: str, **_kwargs: Any) -> None:
        return None

    @property
    def messages(self) -> AsyncIterator[Any]:
        return self._none()

    async def _none(self) -> AsyncIterator[Any]:
        return
        yield


async def test_the_task_goes_on_the_topic_main_picks_for_the_node(
    said: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[str] = []

    async def _publish(topic: str, _payload: Any, **_kw: Any) -> None:
        published.append(topic)

    main = chat.find_main_actor(None)
    monkeypatch.setattr(main, "_mqtt_publish", _publish, raising=False)
    monkeypatch.setattr(chat, "mqtt_client", lambda *_a, **_k: _QuietBroker())

    await _ask(said)

    assert published == ["agents/at/edge/counter/task"]
