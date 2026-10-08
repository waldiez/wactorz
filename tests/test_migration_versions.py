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

from tests.old_database import as_first_release
from wactorz.core.persistence import WactorzDB, migrations
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
    def test_a_new_database_is_created_current(self, fresh: WactorzDB) -> None:
        """Today's schema has everything the migrations add, and no data to change."""
        assert get_current_version(fresh) == FRAMEWORK_VERSION
        assert not _pending_state_versions(fresh)

    def test_a_new_database_has_no_upgrade_to_make(self, tmp_path: Path) -> None:
        with WactorzDB(str(tmp_path / "new.db")) as db:
            result = run_migrations(db, PickleStore(str(tmp_path / "state")))

        assert (result["from_version"], result["sql_migrations"]) == (FRAMEWORK_VERSION, 0)
        assert result["state_migrations"] == 0

    def test_an_existing_file_without_a_version_is_not_taken_for_new(self, tmp_path: Path) -> None:
        # Tables there before this schema arrived: an older database, whatever
        # its version row says, and the migrations must run over it.
        path = tmp_path / "old.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE kv_store (agent TEXT, key TEXT, value TEXT)")
        conn.close()

        with WactorzDB(str(path)) as db:
            assert get_current_version(db) < FRAMEWORK_VERSION

    def test_the_first_releases_database_is_version_one(self, tmp_path: Path) -> None:
        with WactorzDB(str(tmp_path / "v1.db")) as db:
            assert get_current_version(as_first_release(db)) == 1

    def test_a_database_with_no_schema_at_all_is_version_zero(self, tmp_path: Path) -> None:
        bare = BareDB(str(tmp_path / "bare.db"))
        assert get_current_version(bare) == 0
        bare.conn.close()

    def test_migrating_brings_it_to_the_framework_version(self, migrated: WactorzDB) -> None:
        assert get_current_version(migrated) == FRAMEWORK_VERSION


def _make_state_migrations_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(_db: object, _store: object) -> None:
        raise RuntimeError("state upgrade failed")

    monkeypatch.setattr(
        migrations, "_STATE_MIGRATIONS", dict.fromkeys(migrations._STATE_MIGRATIONS, _broken)
    )


@pytest.fixture(name="failing_state")
def failing_state_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every state migration raises, as one meeting data it cannot handle would."""
    _make_state_migrations_fail(monkeypatch)


class TestRunMigrations:
    def test_it_reports_where_it_started_and_finished(self, tmp_path: Path) -> None:
        with WactorzDB(str(tmp_path / "a.db")) as fresh_db:
            db = as_first_release(fresh_db)
            result = run_migrations(db, PickleStore(str(tmp_path / "state")))
            assert result["from_version"] == 1
            assert result["to_version"] == FRAMEWORK_VERSION
            assert result["sql_migrations"] > 0
            assert not result["errors"]

    def test_the_sql_migrations_add_what_the_bookkeeping_and_chat_log_need(
        self, tmp_path: Path
    ) -> None:
        with WactorzDB(str(tmp_path / "b.db")) as fresh_db:
            db = as_first_release(fresh_db)
            run_migrations(db, PickleStore(str(tmp_path / "state")))

            def columns(table: str) -> set[str]:
                return {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")}

            assert "framework_version" in columns("schema_version")
            assert "attachments" in columns("chat_log")
            assert columns("migration_history")

    def test_running_it_twice_is_a_no_op(self, tmp_path: Path) -> None:
        """Startup calls this every boot, so a second pass must apply nothing."""
        with WactorzDB(str(tmp_path / "c.db")) as fresh_db:
            db = as_first_release(fresh_db)
            store = PickleStore(str(tmp_path / "state"))
            run_migrations(db, store)
            second = run_migrations(db, store)

            assert second["from_version"] == FRAMEWORK_VERSION
            assert second["sql_migrations"] == 0
            assert second["state_migrations"] == 0

    def test_a_failed_state_migration_is_reported_and_does_not_abort(
        self, tmp_path: Path, failing_state: None
    ) -> None:
        """The schema work still completes and the failure is returned rather
        than raised — a boot that stopped here would leave the database
        half-upgraded.
        """
        with WactorzDB(str(tmp_path / "d.db")) as fresh_db:
            db = as_first_release(fresh_db)
            result = run_migrations(db, PickleStore(str(tmp_path / "state")))

            assert result["errors"]
            assert result["to_version"] == FRAMEWORK_VERSION
            assert result["sql_migrations"] > 0

    def test_a_failed_state_migration_stays_pending_so_it_is_retried(
        self, tmp_path: Path, failing_state: None
    ) -> None:
        """The property that matters: only successes are recorded, so the work
        is still owed on the next boot even though the schema is current.
        """
        with WactorzDB(str(tmp_path / "e.db")) as fresh_db:
            db = as_first_release(fresh_db)
            run_migrations(db, PickleStore(str(tmp_path / "state")))

            assert get_current_version(db) == FRAMEWORK_VERSION
            assert _pending_state_versions(db), "failed state work must remain owed"

    def test_a_later_run_that_succeeds_clears_the_backlog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = PickleStore(str(tmp_path / "state"))
        with WactorzDB(str(tmp_path / "f.db")) as fresh_db:
            db = as_first_release(fresh_db)
            with monkeypatch.context() as failing:
                _make_state_migrations_fail(failing)
                run_migrations(db, store)
            assert _pending_state_versions(db)

            run_migrations(db, store)
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


class TestNodeHistoryReadings:
    """A database made before nodes reported their readings gains the columns."""

    @staticmethod
    def _columns(db: WactorzDB) -> set[str]:
        return {row[1] for row in db.conn.execute("PRAGMA table_info(node_metrics_history)")}

    def test_an_older_table_gains_them(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path / "state"))
        with WactorzDB(str(tmp_path / "old.db")) as db:
            # Up to date, then the table put back as version 3 made it.
            run_migrations(db, store)
            db.conn.executescript(
                "DROP TABLE node_metrics_history;"
                "CREATE TABLE node_metrics_history (ts REAL NOT NULL, node TEXT NOT NULL,"
                " online INTEGER NOT NULL, cpu_pct REAL, mem_used_mb REAL, mem_free_mb REAL,"
                " agents INTEGER);"
            )
            db.conn.execute("UPDATE schema_version SET framework_version = 3")
            db.conn.commit()

            run_migrations(db, store)

            assert {name for name, _ in migrations.NODE_HISTORY_READINGS} <= self._columns(db)
            # And a sample with them is taken, where it was refused before.
            db.write_metrics_history(
                [], [{"ts": 1.0, "node": "rpi", "online": 1, "disk_free_mb": 3000.0}]
            )
            assert db.query_node_history("rpi", 0)[0]["disk_free_mb"] == 3000.0

    def test_a_new_table_already_has_them(self, migrated: WactorzDB) -> None:
        assert {name for name, _ in migrations.NODE_HISTORY_READINGS} <= self._columns(migrated)
