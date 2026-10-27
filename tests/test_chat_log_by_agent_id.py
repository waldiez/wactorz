"""Chat history is kept by the agent's id, which a name no longer decides.

Two agents may share a name, so a turn is filed under the id of the agent whose
thread it is in. A database from before this keeps every row: each belonged to
the only agent its name could then mean, whose id was derived from the name.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from wactorz.core.identity import derive_actor_id
from wactorz.core.persistence import migrations
from wactorz.core.persistence.db import WactorzDB


@pytest.fixture(name="sql_only")
def sql_only_fixture() -> Iterator[None]:
    """The schema migrations alone; the state ones need a pickle store."""
    with patch.dict(migrations._STATE_MIGRATIONS, {}, clear=True):
        yield


def _as_version_five(path: str, *names: str) -> None:
    """A database as it stood before the column existed, holding a turn per name."""
    database = WactorzDB(path)
    with database.transaction() as conn:
        conn.execute("DROP INDEX idx_chatlog_agent_id_ts")
        conn.execute("ALTER TABLE chat_log DROP COLUMN agent_id")
        conn.executemany(
            "INSERT INTO chat_log (ts, agent_name, role, content) VALUES (1.0, ?, 'user', 'hi')",
            [(name,) for name in names],
        )
        conn.execute("UPDATE schema_version SET framework_version = 5")
    database.conn.close()


def _upgraded(tmp_path: Path, *names: str) -> WactorzDB:
    path = str(tmp_path / "old.db")
    with WactorzDB(path) as fresh:
        migrations.run_migrations(fresh)
    _as_version_five(path, *names)
    database = WactorzDB(path)
    migrations.run_migrations(database)
    return database


class TestTheUpgrade:
    def test_each_row_gets_the_id_its_agent_had(self, tmp_path: Path, sql_only: None) -> None:
        database = _upgraded(tmp_path, "weather", "main")

        rows = {r["agent_name"]: r["agent_id"] for r in database.query_chat_log()}

        assert rows == {"weather": derive_actor_id("weather"), "main": derive_actor_id("main")}
        database.conn.close()

    def test_running_it_again_leaves_a_given_id_alone(self, tmp_path: Path, sql_only: None) -> None:
        database = _upgraded(tmp_path, "weather")
        minted = str(uuid.uuid4())
        database.write_chat_log(2.0, "weather", "assistant", "sunny", agent_id=minted)

        migrations.migrate_sql_6(database.conn)

        assert {r["agent_id"] for r in database.query_chat_log(role="assistant")} == {minted}
        database.conn.close()

    def test_a_history_is_found_by_id_once_upgraded(self, tmp_path: Path, sql_only: None) -> None:
        database = _upgraded(tmp_path, "weather", "main")

        (row,) = database.query_chat_log(agent_id=derive_actor_id("weather"))

        assert row["agent_name"] == "weather"
        database.conn.close()


def _indexes(database: WactorzDB) -> set[str]:
    rows = database.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    return {row[0] for row in rows}


class TestTheIndex:
    def test_a_new_database_has_it(self, tmp_path: Path) -> None:
        database = WactorzDB(str(tmp_path / "new.db"))

        assert "idx_chatlog_agent_id_ts" in _indexes(database)
        database.conn.close()

    def test_an_upgraded_one_gains_it(self, tmp_path: Path, sql_only: None) -> None:
        database = _upgraded(tmp_path, "weather")

        assert "idx_chatlog_agent_id_ts" in _indexes(database)
        database.conn.close()


class TestTwoAgentsOfOneName:
    def test_their_histories_are_apart(self, tmp_path: Path) -> None:
        database = WactorzDB(str(tmp_path / "wactorz.db"))
        here, there = str(uuid.uuid4()), str(uuid.uuid4())
        database.write_chat_log(1.0, "counter", "user", "on main", agent_id=here)
        database.write_chat_log(2.0, "counter", "user", "on the pi", agent_id=there)

        assert [r["content"] for r in database.query_chat_log(agent_id=here)] == ["on main"]
        assert len(database.query_chat_log(agent_name="counter")) == 2
        database.conn.close()
