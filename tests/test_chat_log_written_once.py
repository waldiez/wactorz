"""A chat turn is stored once, by whichever side carried it.

The dashboard's WebSocket writes both halves of every turn it routes, and
`LLMAgent._log_chat_turn` writes them too. The agent side has to stay — for a
turn that arrives by REST, or any other way that bypasses the WebSocket, it is
the only record — so the WebSocket marks its turns instead, and the agent skips
what is already stored.

The shape that exposed it is an agent whose `chat_stream` is just
`yield await self.chat(...)`, where `chat` is what logs. The Home Assistant,
Gmail and Google Calendar agents all look like that, so a dashboard turn to any
of them was stored twice — the second copy without the redaction the WebSocket
applies.
"""

import asyncio
from collections.abc import AsyncIterator, Generator
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.llm_agent import LLMAgent, LLMProvider
from wactorz.core.persistence import WactorzDB, chat_turn_recorded


class _Delegating:
    """The shape that duplicated: chat_stream delegates to chat, and chat logs."""

    name = "home-assistant-agent"
    actor_id = "home-assistant-agent-0001"
    _log_chat_turn = LLMAgent._log_chat_turn

    async def chat(self, user_message: str) -> str:
        reply = f"done: {user_message}"
        self._log_chat_turn(user_message, reply, ts_user=1.0, ts_reply=2.0)
        return reply

    async def chat_stream(self, user_message: str) -> AsyncIterator[Any]:
        yield await self.chat(user_message)
        yield {}


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[WactorzDB, Any, None]:
    store = WactorzDB(str(tmp_path / "wactorz.db"))
    monkeypatch.setattr("wactorz.agents.llm_agent.get_db", lambda: store)
    yield store
    store.close()


async def _drain(agent: _Delegating, text: str) -> None:
    async for _chunk in agent.chat_stream(text):
        pass


async def _dashboard_turn(agent: _Delegating, text: str) -> None:
    """What the WebSocket does: mark the turn in its own task, then stream it."""

    async def turn() -> None:
        chat_turn_recorded.set(True)
        await _drain(agent, text)

    await asyncio.create_task(turn())


class TestADashboardTurn:
    async def test_is_not_stored_again_by_the_agent(self, db: WactorzDB) -> None:
        await _dashboard_turn(_Delegating(), "take a snapshot")

        assert db.query_chat_log() == []

    async def test_marking_it_does_not_silence_a_turn_arriving_another_way(
        self, db: WactorzDB
    ) -> None:
        # The mark belongs to the task that set it. A REST turn to the same
        # agent, outside that task, is still the agent's to store.
        agent = _Delegating()
        await _dashboard_turn(agent, "from the dashboard")
        await _drain(agent, "from rest")

        assert {row["content"] for row in db.query_chat_log()} == {"from rest", "done: from rest"}


class TestATurnFromAnywhereElse:
    async def test_is_still_stored_by_the_agent(self, db: WactorzDB) -> None:
        # REST stores nothing of its own, so this is the only record of the turn.
        await _drain(_Delegating(), "from rest")

        assert len(db.query_chat_log()) == 2

    def test_is_redacted_like_the_dashboard_copy(self, db: WactorzDB) -> None:
        # The table outlives the conversation and is readable through the API.
        LLMAgent._log_chat_turn(
            _Delegating(),  # type: ignore[arg-type]
            "use password=hunter2 please",
            "noted, password=hunter2",
            ts_user=1.0,
            ts_reply=2.0,
        )

        rows = db.query_chat_log()
        assert len(rows) == 2
        assert all("hunter2" not in row["content"] for row in rows)


class _Streams(LLMProvider):  # pylint: disable=abstract-method
    """A provider that really streams."""

    async def _stream(self, messages: list[dict], system: str = "", **kwargs: Any) -> Any:
        yield "a"
        yield "b"


class _AnswersWhole(LLMProvider):  # pylint: disable=abstract-method
    """A provider that only answers in one piece, so chat_stream falls back to chat()."""

    async def _complete(self, messages: list[dict], system: str = "", **kwargs: Any) -> Any:
        return "whole", {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}


class _BreaksMidStream(LLMProvider):  # pylint: disable=abstract-method
    """Streams one chunk, then fails — an answer interrupted part-way."""

    async def _stream(self, messages: list[dict], system: str = "", **kwargs: Any) -> Any:
        yield "half"
        raise ValueError("connection dropped")


def _agent(provider: LLMProvider, tmp_path: Path) -> LLMAgent:
    return LLMAgent(llm_provider=provider, name="plain-agent", persistence_dir=str(tmp_path))


async def _stream_turn(agent: LLMAgent, text: str) -> None:
    async for _chunk in agent.chat_stream(text):
        pass


class TestAStreamedTurn:
    """Recorded whether or not the provider streams — the record used to depend on it."""

    async def test_is_recorded_like_a_whole_one(self, db: WactorzDB, tmp_path: Path) -> None:
        await _stream_turn(_agent(_Streams(), tmp_path), "hello")

        rows = sorted(db.query_chat_log(), key=lambda row: row["ts"])
        assert [(row["role"], row["content"]) for row in rows] == [
            ("user", "hello"),
            ("assistant", "ab"),
        ]

    async def test_a_whole_answer_is_still_recorded_once(
        self, db: WactorzDB, tmp_path: Path
    ) -> None:
        # The fallback goes through chat(), which records; the streaming path
        # must not add a second pair on top of it.
        await _stream_turn(_agent(_AnswersWhole(), tmp_path), "hello")

        assert len(db.query_chat_log()) == 2

    async def test_is_not_recorded_again_on_the_dashboard(
        self, db: WactorzDB, tmp_path: Path
    ) -> None:
        agent = _agent(_Streams(), tmp_path)

        async def turn() -> None:
            chat_turn_recorded.set(True)
            await _stream_turn(agent, "hello")

        await asyncio.create_task(turn())

        assert db.query_chat_log() == []

    async def test_an_interrupted_answer_is_not_recorded(
        self, db: WactorzDB, tmp_path: Path
    ) -> None:
        # As chat(): only a turn that finished is written, even though the
        # agent's own memory keeps the partial reply.
        with pytest.raises(ValueError, match="connection dropped"):
            await _stream_turn(_agent(_BreaksMidStream(), tmp_path), "hello")

        assert db.query_chat_log() == []
