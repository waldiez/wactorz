"""A chat to an agent on a node, when there is no broker to carry it.

The agent is somewhere only the broker reaches. The user is told at once that it
could not be reached, and the log says so as it says every other consequence of
the outage: a warning with the reason. It is not a fault in the server, so there
is no traceback of the code that found the broker gone.
"""

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from aiomqtt import MqttError

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
    main = SimpleNamespace(_mqtt_broker="broker.invalid", _mqtt_port=1883)
    registry = SimpleNamespace(find_by_name=lambda _name: None)
    monkeypatch.setattr(runtime, "registry", registry)
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: main)
    monkeypatch.setattr(chat, "remote_node_for", lambda _name: "edge")
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
