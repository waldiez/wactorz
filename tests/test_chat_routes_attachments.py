"""Where a turn's attachments go once routing picks a destination.

`route_chat` reaches four differently-shaped entry points and two routes that
cannot carry files at all. Only the LLM ones grew a parameter for this, so the
rule being pinned is that a file is either sent or said out loud — never
silently dropped, and never a TypeError on an agent that never wanted one.

The records are turned into blocks here rather than by the agent: the files are
the web layer's, and only this side knows how to reach them.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from wactorz.core.actor import Actor, ActorState, Message
from wactorz.web import chat, runtime, uploads

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


class _Streamer(Actor):
    """An agent whose streaming entry point takes attachments."""

    def __init__(self, name: str = "vision-agent") -> None:
        super().__init__(name=name)
        self.state = ActorState.RUNNING
        self.got: list[Any] = []

    async def chat_stream(self, user_message: str, attachments: list[dict] | None = None):
        self.got.append(attachments)
        yield "described"

    async def handle_message(self, message: Message) -> None:
        return None


class _TextOnly(Actor):
    """An agent that overrides chat_stream with routing of its own, as the
    Gmail, Calendar and Home Assistant agents do — no attachments parameter."""

    def __init__(self, name: str = "calendar-agent") -> None:
        super().__init__(name=name)
        self.state = ActorState.RUNNING
        self.asked: list[str] = []

    async def chat_stream(self, user_message: str):
        self.asked.append(user_message)
        yield "booked"

    async def handle_message(self, message: Message) -> None:
        return None


class _Registry:
    def __init__(self, actor: Actor) -> None:
        self._actor = actor

    def get(self, actor_id: str) -> Actor | None:
        return self._actor if self._actor.actor_id == actor_id else None

    def find_by_name(self, name: str) -> Actor | None:
        return self._actor if self._actor.name == name else None


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> Iterator[Path]:
    """One stored file, under a temporary state directory."""
    directory = tmp_path / "uploads"
    directory.mkdir(parents=True)
    (directory / ("a" * 32)).write_bytes(PNG)
    with patch.object(uploads, "resolve_state_dir", return_value=str(tmp_path)):
        yield directory


@pytest.fixture(autouse=True)
def _restore_runtime() -> Iterator[None]:
    registry = runtime.registry
    yield
    runtime.registry = registry


def _record(name: str = "shot.png", mime: str = "image/png") -> dict[str, Any]:
    return {"id": "a" * 32, "name": name, "mime": mime, "size": len(PNG)}


async def _say(text: str, files: list[dict[str, Any]] | None = None) -> list[str]:
    replies: list[str] = []

    async def _reply(message: str) -> None:
        replies.append(message)

    await chat.route_chat(text, _reply, attachments=files)
    return replies


class TestAnAgentThatTakesThem:
    async def test_the_records_arrive_as_blocks(self, store: Path) -> None:
        agent = _Streamer()
        runtime.registry = _Registry(agent)

        await _say("@vision-agent what is this", [_record()])

        blocks = agent.got[0]
        assert [b["type"] for b in blocks] == ["text", "image"]
        assert "shot.png" in blocks[0]["text"]

    async def test_a_turn_without_files_passes_none(self, store: Path) -> None:
        agent = _Streamer()
        runtime.registry = _Registry(agent)

        await _say("@vision-agent hello")

        assert not agent.got[0]

    async def test_a_reference_whose_file_is_gone_is_named(self, store: Path) -> None:
        agent = _Streamer()
        runtime.registry = _Registry(agent)

        await _say("@vision-agent what is this", [_record() | {"id": "b" * 32}])

        # The turn still goes through — losing the text over a missing file
        # would be worse than telling the model the file is not there.
        assert "no longer available" in agent.got[0][0]["text"]


class TestAnAgentThatCannotTakeThem:
    async def test_the_turn_still_reaches_it(self, store: Path) -> None:
        agent = _TextOnly()
        runtime.registry = _Registry(agent)

        replies = await _say("@calendar-agent book friday", [_record()])

        # ⚠ Passing blocks to an entry point that never grew a parameter would
        # be a TypeError, and the user would lose the message with it.
        assert agent.asked == ["book friday"]
        assert "booked" in replies

    async def test_the_user_is_told_the_file_was_not_sent(self, store: Path) -> None:
        agent = _TextOnly()
        runtime.registry = _Registry(agent)

        replies = await _say("@calendar-agent book friday", [_record()])

        assert "shot.png" in replies[0]
        assert "not sent" in replies[0]


class TestRoutesThatCarryNothing:
    async def test_a_slash_command_says_so(self, store: Path) -> None:
        runtime.registry = _Registry(_Streamer())

        replies = await _say("/help", [_record()])

        assert "not sent" in replies[0]

    async def test_an_unknown_agent_is_still_an_unknown_agent(self, store: Path) -> None:
        runtime.registry = _Registry(_Streamer())

        replies = await _say("@nobody hello", [_record()])

        assert "not found" in replies[-1]
