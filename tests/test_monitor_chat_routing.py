"""Regression tests for the monitor's chat routing.

The monitor was split into :mod:`wactorz.web.*`; the chat routing that used to
live in ``monitor_server`` now lives in ``wactorz.web.chat`` (``route_chat``),
``wactorz.web.events`` (``parse_topic``), and ``wactorz.web.runtime`` (shared
``registry`` / ``state``).
"""

import pytest

import wactorz.web.chat as chat
import wactorz.web.events as events
import wactorz.web.runtime as runtime
from wactorz.core.actor import ActorState


@pytest.mark.asyncio
async def test_route_chat_without_stream_end_callback(monkeypatch):
    class Target:
        name = "target"
        state = ActorState.RUNNING

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

    assert event is not None
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
        "thread_id": agent_id,
    }


def test_a_turn_relayed_to_another_agent_is_kept_in_that_agents_thread():
    # A voice surface hands the user's words to the agent that answers them.
    runtime.state["agents"].update(
        {"reachy-id": {"name": "reachy-mini"}, "weather-id": {"name": "weather"}}
    )
    try:
        event = events.parse_topic(
            "agents/reachy-id/chat", '{"from":"user","to":"weather","content":"rain today?"}'
        )
    finally:
        runtime.state["agents"].pop("reachy-id", None)
        runtime.state["agents"].pop("weather-id", None)

    assert event is not None
    assert event["_push_chat"]["thread_id"] == "weather-id"
