"""Regression tests for the monitor's legacy MQTT chat fallback.

The monitor was split into :mod:`wactorz.web.*`; the chat routing that used to
live in ``monitor_server`` now lives in ``wactorz.web.chat`` (``route_chat`` /
``handle_chat_mqtt``), ``wactorz.web.events`` (``parse_topic``), and
``wactorz.web.runtime`` (shared ``registry`` / ``state``).
"""

from unittest import mock

import pytest

import wactorz.web.chat as chat
import wactorz.web.events as events
import wactorz.web.runtime as runtime


@pytest.mark.asyncio
async def test_mqtt_fallback_defers_to_running_io_agent(monkeypatch):
    class Registry:
        def find_by_name(self, name):
            return object() if name == "io-agent" else None

    route = mock.AsyncMock()
    monkeypatch.setattr(runtime, "registry", Registry())
    monkeypatch.setattr(chat, "route_chat", route)

    await chat.handle_chat_mqtt({"content": "@reachy-mini hello"})

    route.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_chat_without_stream_end_callback(monkeypatch):
    class Target:
        name = "target"

        async def process_user_input(self, text):
            return f"handled: {text}"

    class Registry:
        def find_by_name(self, name):
            return Target()

    replies = []

    async def reply(text):
        replies.append(text)

    monkeypatch.setattr(runtime, "registry", Registry())

    await chat.route_chat("hello", reply)

    assert replies == ["handled: hello"]


def test_voice_transcript_is_forwarded_as_user_chat():
    agent_id = "reachy-id"
    runtime.state["agents"][agent_id] = {"name": "reachy-mini"}
    try:
        event = events.parse_topic(
            f"agents/{agent_id}/chat",
            '{"from":"user","to":"reachy-mini","content":"turn on the light",'
            '"source":"voice","surface":"reachy-mini","surface_label":"Reachy",'
            '"brain":"main","timestamp":123.5}',
        )
    finally:
        runtime.state["agents"].pop(agent_id, None)

    assert event["_push_chat"] == {
        "type": "chat",
        "from": "user",
        "to": "reachy-mini",
        "content": "turn on the light",
        "source": "voice",
        "surface": "reachy-mini",
        "surface_label": "Reachy",
        "brain": "main",
        "timestamp": 123.5,
    }
