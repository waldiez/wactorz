"""Chat turns must actually reach the ``chat_log`` table.

``_log_chat_turn`` called ``db.log_chat(...)`` — a method that has never existed
on :class:`WactorzDB`; the real name is ``write_chat_log``. Every call raised
``AttributeError`` into an ``except Exception`` that logged at DEBUG, so every
turn routed through ``LLMAgent.chat()`` was dropped from the persistent feed and
nothing surfaced to say so.

Both halves of that defect are pinned here: the rows have to land in the table,
and a genuine write failure has to be logged loudly enough to notice without
breaking the turn.
"""

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.llm_agent import LLMAgent
from wactorz.core.persistence import WactorzDB


class _StubAgent:
    """Minimal stand-in — ``_log_chat_turn`` only reads these two attributes."""

    name = "test-agent"
    actor_id = "test-agent-0001"


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Generator[WactorzDB, Any, None]:
    store = WactorzDB(str(tmp_path / "wactorz.db"))
    yield store
    store.close()


def _log_turn(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    """Invoke the real method with ``get_db()`` pointed at ``db``."""
    monkeypatch.setattr("wactorz.agents.llm_agent.get_db", lambda: db)
    LLMAgent._log_chat_turn(
        _StubAgent(),  # type: ignore[arg-type]
        "what is the temperature?",
        "21.5 degrees",
        ts_user=1000.0,
        ts_reply=1001.0,
    )


class TestChatLogPersistence:
    def test_both_halves_of_the_turn_are_persisted(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _log_turn(monkeypatch, db)

        rows = db.query_chat_log(agent_name="test-agent")
        assert len(rows) == 2, "expected one row for the user turn and one for the reply"

        by_role = {row["role"]: row for row in rows}
        assert by_role["user"]["content"] == "what is the temperature?"
        assert by_role["assistant"]["content"] == "21.5 degrees"

    def test_rows_carry_the_session_and_timestamps(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Arguments must map to the right columns — the old call passed them
        positionally in an order the real signature does not accept."""
        _log_turn(monkeypatch, db)

        by_role = {row["role"]: row for row in db.query_chat_log(agent_name="test-agent")}
        assert by_role["user"]["ts"] == 1000.0
        assert by_role["assistant"]["ts"] == 1001.0
        assert by_role["user"]["session_id"] == "test-agent-0001"
        assert by_role["assistant"]["session_id"] == "test-agent-0001"

    def test_write_failure_warns_and_does_not_break_the_turn(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failing write is logged at WARNING, not DEBUG — logging it at debug
        is what kept the original defect invisible — and never propagates."""

        class _BrokenDB:
            def write_chat_log(self, **_kwargs: Any) -> None:
                raise RuntimeError("disk on fire")

        with caplog.at_level("WARNING"):
            _log_turn(monkeypatch, _BrokenDB())

        assert any(
            record.levelname == "WARNING" and "chat_log SQLite write failed" in record.getMessage()
            for record in caplog.records
        ), "a failed chat_log write must surface at WARNING"
