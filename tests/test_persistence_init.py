"""``init_persistence`` actually runs the framework migrations.

The regression this pins: every other test calls ``init_persistence`` with
``run_migration=False``, so the migration branch had no coverage at all. A wrong
relative import inside it — resolving ``.migrations`` to a sibling module rather
than the migration runner — raised ImportError, which the code caught and logged
at DEBUG. Schema upgrades, state upgrades and spawn validation stopped running
on every startup, and the full suite stayed green.

Two things follow, and both are asserted below: the runner must be reached, and
a missing runner must not be silently swallowed.
"""

import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from wactorz.core import persistence


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None, Any, None]:
    """Point the stores at a temp directory, and close them afterwards.

    ``init_persistence`` installs process-wide stores, so a test that left one
    open would leak a database handle into every test that followed — and on
    Windows, into the temp-directory cleanup.
    """
    monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
    yield
    persistence.close_persistence()


class TestMigrationsAreReached:
    def test_run_migrations_is_called(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[Any, ...]] = []

        def fake_run_migrations(
            db: persistence.WactorzDB, pickle_store: str | None = None
        ) -> dict[str, list[Any]]:
            calls.append((db, pickle_store))
            return {"spawn_issues": []}

        monkeypatch.setattr(
            "wactorz.core.persistence.lifecycle.run_migrations", fake_run_migrations
        )
        persistence.init_persistence(state_dir=str(tmp_path), run_migration=True)
        assert len(calls) == 1, "framework migrations did not run"

    def test_not_called_when_disabled(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            "wactorz.core.persistence.lifecycle.run_migrations",
            lambda *a, **k: calls.append(a) or {"spawn_issues": []},
        )
        persistence.init_persistence(state_dir=str(tmp_path), run_migration=False)
        assert calls == []

    def test_real_runner_resolves(self, tmp_path) -> None:
        """No monkeypatching: the real import must succeed from this package."""
        from wactorz.core.persistence.migrations import run_migrations

        assert callable(run_migrations)
        persistence.init_persistence(state_dir=str(tmp_path), run_migration=True)


class TestMigrationFailuresDoNotStopStartup:
    def test_failing_migration_is_logged_and_startup_continues(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("migration blew up")

        monkeypatch.setattr("wactorz.core.persistence.lifecycle.run_migrations", boom)
        with caplog.at_level(logging.WARNING):
            db, _pickle = persistence.init_persistence(state_dir=str(tmp_path), run_migration=True)
        assert db is not None, "startup must survive a failing migration"
        assert any("Migration runner failed" in r.message for r in caplog.records)

    def test_spawn_issues_are_surfaced(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            "wactorz.core.persistence.lifecycle.run_migrations",
            lambda *a, **k: {
                "spawn_issues": [{"severity": "error", "agent": "broken", "message": "bad recipe"}]
            },
        )
        with caplog.at_level(logging.WARNING):
            persistence.init_persistence(state_dir=str(tmp_path), run_migration=True)
        assert any("broken" in r.message for r in caplog.records)
