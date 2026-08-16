"""A state migration that failed must be retried, not stamped as done.

``framework_version`` was a single number standing for two independent facts:
the SQL schema is at version N, and the state data has been migrated to N. When
a SQL migration succeeded and its paired state migration threw, the version was
stamped anyway — reasonably, since re-running a non-idempotent ``ALTER TABLE``
would fail. But the next startup then saw ``current >= FRAMEWORK_VERSION``,
returned early, and never retried the state migration. The warning scrolled past
once and the data stayed half-migrated for good.

State work is now derived from ``migration_history``, which only ever records
successes, so the two versions can disagree until the state side catches up.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from wactorz.core.persistence import migrations
from wactorz.core.persistence.db import WactorzDB


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Any:
    """A fresh database, which is at v1 — the framework_version column itself
    is what the v1→v2 SQL migration adds, so migrations have work to do."""
    database = WactorzDB(str(tmp_path / "wactorz.db"))
    yield database
    database.conn.close()


def _state_calls(recorder: list[int], fail_on: set[int]) -> Any:
    """A state migration that records every attempt and fails on demand."""

    def _migrate(_db: Any, _pickle_store: Any, version: int = 2) -> None:
        recorder.append(version)
        if version in fail_on:
            raise RuntimeError("state migration exploded")

    return _migrate


class TestAFailedStateMigrationIsRetried:
    def test_the_next_run_attempts_it_again(self, db: Any) -> None:
        attempts: list[int] = []
        with patch.dict(migrations._STATE_MIGRATIONS, {2: _state_calls(attempts, {2})}):
            migrations.run_migrations(db)
            assert attempts == [2]
            # Second startup: the schema is already at FRAMEWORK_VERSION, which is
            # exactly the case that used to return early.
            migrations.run_migrations(db)

        assert attempts == [2, 2]

    def test_it_stops_being_retried_once_it_succeeds(self, db: Any) -> None:
        attempts: list[int] = []
        with patch.dict(migrations._STATE_MIGRATIONS, {2: _state_calls(attempts, {2})}):
            migrations.run_migrations(db)
        with patch.dict(migrations._STATE_MIGRATIONS, {2: _state_calls(attempts, set())}):
            migrations.run_migrations(db)
            migrations.run_migrations(db)

        # failed, retried and succeeded, then left alone
        assert attempts == [2, 2]

    def test_a_state_failure_is_reported_every_time(self, db: Any) -> None:
        # The failure used to be visible once and then never again.
        attempts: list[int] = []
        with patch.dict(migrations._STATE_MIGRATIONS, {2: _state_calls(attempts, {2})}):
            first = migrations.run_migrations(db)
            second = migrations.run_migrations(db)

        assert first["errors"] and second["errors"]

    def test_the_schema_version_is_still_stamped(self, db: Any) -> None:
        # The SQL side genuinely is at the new version, and its migrations are
        # not idempotent — re-running them is what the stamp prevents.
        attempts: list[int] = []
        with patch.dict(migrations._STATE_MIGRATIONS, {2: _state_calls(attempts, {2})}):
            migrations.run_migrations(db)

        assert migrations.get_current_version(db) == migrations.FRAMEWORK_VERSION

    def test_a_successful_run_leaves_nothing_pending(self, db: Any) -> None:
        attempts: list[int] = []
        with patch.dict(migrations._STATE_MIGRATIONS, {2: _state_calls(attempts, set())}):
            migrations.run_migrations(db)
            migrations.run_migrations(db)

        assert attempts == [2]
