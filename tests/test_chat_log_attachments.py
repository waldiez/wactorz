"""A turn's attachments survive a reload.

The chat thread is rebuilt from `chat_log` after a refresh, so an attachment
recorded only in the live message vanishes with the page — the text of the turn
comes back and the file it was about does not.

References are stored on the row rather than resolved from disk on read: the
files are immutable, resolving them would mean one read per attachment per
history request, and a chip should still say what was attached even if the file
behind it is gone.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from wactorz.core.persistence import migrations
from wactorz.core.persistence.db import WactorzDB

ATTACHMENTS = [
    {"id": "a" * 32, "name": "shot.png", "mime": "image/png", "size": 1024},
    {"id": "b" * 32, "name": "notes.pdf", "mime": "application/pdf", "size": 2048},
]


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Any:
    database = WactorzDB(str(tmp_path / "wactorz.db"))
    yield database
    database.conn.close()


class TestStoringThem:
    def test_a_turn_remembers_what_was_attached(self, db: Any) -> None:
        db.write_chat_log(
            ts=1.0, agent_name="main", role="user", content="look", attachments=ATTACHMENTS
        )

        row = db.query_chat_log()[0]
        assert row["attachments"] == ATTACHMENTS

    def test_a_turn_without_any_reads_back_as_none_rather_than_junk(self, db: Any) -> None:
        # Every existing row has no attachments, so this is the common case and
        # the one a consumer written before this change will meet.
        db.write_chat_log(ts=1.0, agent_name="main", role="user", content="hi")

        assert db.query_chat_log()[0]["attachments"] == []

    def test_writing_them_is_optional(self, db: Any) -> None:
        # The call sites that know nothing about attachments must keep working.
        db.write_chat_log(ts=1.0, agent_name="main", role="assistant", content="done")

        assert db.query_chat_log()[0]["content"] == "done"

    def test_a_row_written_before_the_column_existed_still_reads(self, db: Any) -> None:
        # The upgrade path: an older row has NULL here, not '[]'.
        db.write_chat_log(ts=1.0, agent_name="main", role="user", content="old")
        with db.transaction() as conn:
            conn.execute("UPDATE chat_log SET attachments = NULL")

        assert db.query_chat_log()[0]["attachments"] == []

    def test_unreadable_references_do_not_take_the_history_down(self, db: Any) -> None:
        # The column is JSON written by us, but a history request failing wholesale
        # because one row is malformed would lose every other turn with it.
        db.write_chat_log(ts=1.0, agent_name="main", role="user", content="x")
        with db.transaction() as conn:
            conn.execute("UPDATE chat_log SET attachments = 'not json'")

        assert db.query_chat_log()[0]["attachments"] == []


@pytest.fixture(name="sql_only")
def sql_only_fixture() -> Iterator[None]:
    """Run the schema migrations alone.

    The state migrations need a pickle store this test has no use for, and their
    failure is retried on every call — so leaving them in place buries the thing
    under test in unrelated warnings.
    """
    with patch.dict(migrations._STATE_MIGRATIONS, {}, clear=True):
        yield


def _as_version_two(path: str) -> None:
    """A database as it stood before the column existed."""
    database = WactorzDB(path)
    with database.transaction() as conn:
        conn.execute("ALTER TABLE chat_log DROP COLUMN attachments")
        conn.execute("UPDATE schema_version SET framework_version = 2")
    database.conn.close()


class TestTheUpgrade:
    def test_a_database_without_the_column_gains_it(self, tmp_path: Path, sql_only: None) -> None:
        path = str(tmp_path / "old.db")
        WactorzDB(path).conn.close()  # fresh, then wound back
        migrations.run_migrations(WactorzDB(path))
        _as_version_two(path)

        database = WactorzDB(path)
        migrations.run_migrations(database)
        database.write_chat_log(
            ts=1.0, agent_name="main", role="user", content="x", attachments=ATTACHMENTS
        )

        assert database.query_chat_log()[0]["attachments"] == ATTACHMENTS
        database.conn.close()

    def test_running_it_twice_is_harmless(self, tmp_path: Path, sql_only: None) -> None:
        database = WactorzDB(str(tmp_path / "twice.db"))
        migrations.run_migrations(database)
        migrations.run_migrations(database)

        database.write_chat_log(ts=1.0, agent_name="main", role="user", content="x")
        assert database.query_chat_log()[0]["attachments"] == []
        database.conn.close()


class TestWhatIsStored:
    def test_only_the_fields_a_chip_needs(self, db: Any) -> None:
        # Whatever a caller passes is written verbatim, so the shape is pinned
        # here rather than left to whoever calls it next.
        db.write_chat_log(
            ts=1.0, agent_name="main", role="user", content="x", attachments=ATTACHMENTS
        )

        stored = db.query_chat_log()[0]["attachments"][0]
        assert set(stored) == {"id", "name", "mime", "size"}
        assert json.dumps(stored)  # round-trips
