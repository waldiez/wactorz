"""Version bookkeeping across a database upgrade.

Runs against a real on-disk SQLite database, because the questions are about
what the schema actually contains — a fake would answer from whatever it was
told to hold, which is how a migration test proves nothing.

Two numbers are tracked separately and the separation is the point.
`framework_version` records how far the schema got. `migration_history` records
which *state* migrations succeeded. A SQL migration is not idempotent, so its
version must be stamped once applied — and stamping that same number for a
paired state migration that threw is what lets failed state work be recorded as
done and never retried.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from wactorz.core.persistence import WactorzDB
from wactorz.core.persistence.migrations import (
    FRAMEWORK_VERSION,
    _pending_state_versions,
    get_current_version,
    run_migrations,
)
from wactorz.core.persistence.pickle_store import PickleStore


class BareDB:
    """A database with no wactorz schema at all."""

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)


@pytest.fixture(name="fresh")
def fresh_fixture(tmp_path: Path) -> Iterator[WactorzDB]:
    """The base schema, before any migration has run."""
    with WactorzDB(str(tmp_path / "fresh.db")) as database:
        yield database


@pytest.fixture(name="migrated")
def migrated_fixture(tmp_path: Path) -> Iterator[WactorzDB]:
    """A fully upgraded database, the way startup leaves one."""
    with WactorzDB(str(tmp_path / "migrated.db")) as database:
        run_migrations(database, PickleStore(str(tmp_path / "state")))
        yield database


class TestVersionReporting:
    def test_the_base_schema_is_version_one(self, fresh: WactorzDB) -> None:
        """Creating the tables is not the same as being up to date."""
        assert get_current_version(fresh) == 1

    def test_a_database_with_no_schema_at_all_is_version_zero(self, tmp_path: Path) -> None:
        bare = BareDB(str(tmp_path / "bare.db"))
        assert get_current_version(bare) == 0
        bare.conn.close()

    def test_migrating_brings_it_to_the_framework_version(self, migrated: WactorzDB) -> None:
        assert get_current_version(migrated) == FRAMEWORK_VERSION


class TestRunMigrations:
    def test_it_reports_where_it_started_and_finished(self, tmp_path: Path) -> None:
        with WactorzDB(str(tmp_path / "a.db")) as db:
            result = run_migrations(db, PickleStore(str(tmp_path / "state")))
            assert result["from_version"] == 1
            assert result["to_version"] == FRAMEWORK_VERSION
            assert result["sql_migrations"] > 0
            assert not result["errors"]

    def test_the_sql_migrations_add_what_the_bookkeeping_and_chat_log_need(
        self, tmp_path: Path
    ) -> None:
        with WactorzDB(str(tmp_path / "b.db")) as db:
            run_migrations(db, PickleStore(str(tmp_path / "state")))

            def columns(table: str) -> set[str]:
                return {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")}

            assert "framework_version" in columns("schema_version")
            assert "attachments" in columns("chat_log")
            assert columns("migration_history")

    def test_running_it_twice_is_a_no_op(self, tmp_path: Path) -> None:
        """Startup calls this every boot, so a second pass must apply nothing."""
        with WactorzDB(str(tmp_path / "c.db")) as db:
            store = PickleStore(str(tmp_path / "state"))
            run_migrations(db, store)
            second = run_migrations(db, store)

            assert second["from_version"] == FRAMEWORK_VERSION
            assert second["sql_migrations"] == 0
            assert second["state_migrations"] == 0

    def test_a_failed_state_migration_is_reported_and_does_not_abort(self, tmp_path: Path) -> None:
        """Without a pickle store the state pass cannot run. The schema work
        still completes and the failure is returned rather than raised — a boot
        that stopped here would leave the database half-upgraded.
        """
        with WactorzDB(str(tmp_path / "d.db")) as db:
            result = run_migrations(db, pickle_store=None)

            assert result["errors"]
            assert result["to_version"] == FRAMEWORK_VERSION
            assert result["sql_migrations"] > 0

    def test_a_failed_state_migration_stays_pending_so_it_is_retried(self, tmp_path: Path) -> None:
        """The property that matters: only successes are recorded, so the work
        is still owed on the next boot even though the schema is current.
        """
        with WactorzDB(str(tmp_path / "e.db")) as db:
            run_migrations(db, pickle_store=None)

            assert get_current_version(db) == FRAMEWORK_VERSION
            assert _pending_state_versions(db), "failed state work must remain owed"

    def test_a_later_run_with_a_store_clears_the_backlog(self, tmp_path: Path) -> None:
        with WactorzDB(str(tmp_path / "f.db")) as db:
            run_migrations(db, pickle_store=None)
            assert _pending_state_versions(db)

            run_migrations(db, PickleStore(str(tmp_path / "state")))
            assert not _pending_state_versions(db)


class TestPendingStateVersions:
    def test_a_migrated_database_owes_nothing(self, migrated: WactorzDB) -> None:
        assert not _pending_state_versions(migrated)

    def test_pending_versions_come_back_in_order(self, migrated: WactorzDB) -> None:
        migrated.conn.execute("DELETE FROM migration_history")
        migrated.conn.commit()

        pending = _pending_state_versions(migrated)
        assert pending == sorted(pending)
        assert all(v <= FRAMEWORK_VERSION for v in pending)

    def test_a_database_with_no_history_table_owes_everything(self, tmp_path: Path) -> None:
        bare = BareDB(str(tmp_path / "bare.db"))
        assert _pending_state_versions(bare)
        bare.conn.close()
