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

from wactorz.agents.llm_agent import LLMAgent
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
