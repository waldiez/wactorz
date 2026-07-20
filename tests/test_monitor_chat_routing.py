"""Regression tests for monitor_server's legacy MQTT chat fallback."""

from unittest import mock

import pytest

import wactorz.monitor_server as monitor


@pytest.mark.asyncio
async def test_mqtt_fallback_defers_to_running_io_agent(monkeypatch):
    class Registry:
        def find_by_name(self, name):
            return object() if name == "io-agent" else None

    route = mock.AsyncMock()
    monkeypatch.setattr(monitor, "registry", Registry())
    monkeypatch.setattr(monitor, "_route_chat", route)

    await monitor.handle_chat_mqtt({"content": "@reachy-mini hello"})

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

    monkeypatch.setattr(monitor, "registry", Registry())

    await monitor._route_chat("hello", reply)

    assert replies == ["handled: hello"]


def test_voice_transcript_is_forwarded_as_user_chat():
    agent_id = "reachy-id"
    monitor.state["agents"][agent_id] = {"name": "reachy-mini"}
    try:
        event = monitor.parse_topic(
            f"agents/{agent_id}/chat",
            '{"from":"user","to":"reachy-mini","content":"turn on the light",'
            '"source":"voice","surface":"reachy-mini","surface_label":"Reachy",'
            '"brain":"main","timestamp":123.5}',
        )
    finally:
        monitor.state["agents"].pop(agent_id, None)

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
