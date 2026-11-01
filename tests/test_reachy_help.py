"""Reachy help is discoverable, local, and usable without a robot connection."""

from __future__ import annotations

import pytest

from wactorz.catalogue_agents.reachy_mini_agent import AGENT_CODE

NS: dict = {}
exec(compile(AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), NS)


class FakeAgent:
    name = "reachy-mini"

    def __init__(self) -> None:
        self.state = {"mini": None}
        self.events = []

    async def publish(self, topic, payload) -> None:
        self.events.append((topic, payload))


@pytest.mark.parametrize(
    "text",
    [
        "help",
        "Reachy help",
        "what can you do?",
        "show me the commands",
        "how do I use you?",
    ],
)
def test_generic_help_phrases_are_local(text: str) -> None:
    assert NS["_embodied_command_for_text"](text) == {"cmd": "help"}


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("help movement", "movement"),
        ("how do I use the camera?", "camera"),
        ("how can I start a conversation?", "voice"),
        ("help me connect", "connection"),
        ("help with the volume", "volume"),
        ("how do I control a Home Assistant light?", "home"),
    ],
)
def test_help_can_be_scoped_by_natural_topic(text: str, topic: str) -> None:
    assert NS["_embodied_command_for_text"](text) == {"cmd": "help", "topic": topic}


def test_normal_commands_are_not_mistaken_for_help() -> None:
    assert NS["_help_command_for_text"]("take a photo") is None
    assert NS["_help_command_for_text"]("start conversation") is None


def test_generic_help_exposes_categories_examples_and_keywords() -> None:
    result = NS["_help"](FakeAgent(), {})

    assert result["topic"] == "all"
    assert set(result["topics"]) == {
        "movement",
        "voice",
        "camera",
        "connection",
        "home",
        "volume",
    }
    assert "wake up" in result["result"]
    assert "help camera" in result["result"]
    assert len(result["spoken_result"]) < len(result["result"])


@pytest.mark.asyncio
async def test_help_works_while_reachy_is_disconnected() -> None:
    agent = FakeAgent()

    result = await NS["handle_task"](agent, {"text": "help camera"})

    assert result["ok"] is True
    assert result["cmd"] == "help"
    assert result["topic"] == "camera"
    assert "take a photo" in result["result"].lower()


@pytest.mark.asyncio
async def test_conversation_speaks_a_summary_and_shows_the_full_guide(monkeypatch) -> None:
    agent = FakeAgent()
    shown = []
    spoken = []

    async def before_speak(text) -> None:
        shown.append(text)

    async def speak_reply(_agent, text, **_kwargs):
        spoken.append(text)
        return {
            "spoke": True,
            "interrupted": False,
            "stopped": False,
            "spoken_result": text,
        }

    monkeypatch.setitem(NS, "_speak_reply", speak_reply)
    result = await NS["_conversation_embodied_bridge"](
        agent,
        "help",
        {"cmd": "help"},
        "session:1",
        before_speak,
        {},
    )

    assert result["ok"] is True
    assert result["spoke"] is True
    assert "Reachy help" in shown[0]
    assert "The full guide is in chat" in spoken[0]
    assert len(spoken[0]) < len(shown[0])
