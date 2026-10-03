"""What the state migration does to the data it finds, and what it leaves alone.

`tests/test_migration_versions.py` covers the version bookkeeping. This covers
the upgrades themselves, against a real on-disk database and real pickle files,
because the question each one answers is what is actually stored afterwards.

Every upgrade here runs on a user's existing data at startup, so each must be
safe on data it did not expect: a malformed row is skipped rather than aborting
the pass, and a row that needs nothing is not rewritten.
"""

import json
import pickle
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wactorz.core.persistence import WactorzDB, migrations
from wactorz.core.persistence.migrations import (
    FRAMEWORK_VERSION,
    _stamp_spawn_registry,
    _upgrade_baselines,
    _upgrade_conversation_history,
    _upgrade_topic_contracts,
    auto_fix_spawn_registry,
    get_current_version,
    migrate_state_2,
    run_migrations,
    validate_spawn_registry,
)
from wactorz.core.persistence.pickle_store import PickleStore


class BareDB:
    """A database with no wactorz schema at all."""

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> PickleStore:
    return PickleStore(str(tmp_path / "state"))


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, store: PickleStore) -> Iterator[WactorzDB]:
    """A database upgraded the way startup leaves it."""
    with WactorzDB(str(tmp_path / "wactorz.db")) as database:
        run_migrations(database, store)
        yield database


@pytest.fixture(name="bare")
def bare_fixture(tmp_path: Path) -> Iterator[BareDB]:
    bare = BareDB(str(tmp_path / "bare.db"))
    yield bare
    bare.conn.close()


def _raw_kv(db: WactorzDB, agent: str, key: str, value: str, updated: float = 1.0) -> None:
    db.conn.execute(
        "INSERT OR REPLACE INTO kv_store (agent, key, value, updated) VALUES (?,?,?,?)",
        (agent, key, value, updated),
    )
    db.conn.commit()


def _kv_row(db: WactorzDB, agent: str, key: str) -> tuple[Any, float]:
    value, updated = db.conn.execute(
        "SELECT value, updated FROM kv_store WHERE agent=? AND key=?", (agent, key)
    ).fetchone()
    return json.loads(value), updated


def _add_spawn(db: WactorzDB, name: str, config: object, version: int | None = None) -> None:
    body = config if isinstance(config, str) else json.dumps(config)
    db.conn.execute(
        "INSERT OR REPLACE INTO spawn_registry (name, config, framework_version) VALUES (?,?,?)",
        (name, body, version),
    )
    db.conn.commit()


def _dynamic(code: str, **extra: Any) -> dict[str, Any]:
    return {"type": "dynamic", "code": code, **extra}


class TestBaselinesInTheDatabase:
    def test_missing_fields_are_filled_with_defaults(
        self, db: WactorzDB, store: PickleStore
    ) -> None:
        _raw_kv(db, "anomaly", "baselines", json.dumps({"sensor.a": {"mean": 3.0}}))

        _upgrade_baselines(db, store)

        baselines, updated = _kv_row(db, "anomaly", "baselines")
        assert baselines["sensor.a"]["mean"] == 3.0
        assert baselines["sensor.a"]["is_binary"] is False
        assert baselines["sensor.a"]["hourly_count"] == [0] * 24
        assert updated > 1.0

    def test_a_complete_baseline_is_not_rewritten(self, db: WactorzDB, store: PickleStore) -> None:
        _raw_kv(db, "anomaly", "baselines", json.dumps({}))
        _upgrade_baselines(db, store)
        full = {
            "is_binary": True,
            "transition_freq": 1.0,
            "ready": True,
            "hourly_count": [1] * 24,
            "max_rate": 2.0,
            "mean_interval": 3.0,
            "p1": 0.1,
            "p99": 9.9,
        }
        _raw_kv(db, "anomaly", "baselines", json.dumps({"sensor.a": full}))

        _upgrade_baselines(db, store)

        assert _kv_row(db, "anomaly", "baselines") == ({"sensor.a": full}, 1.0)

    @pytest.mark.parametrize("stored", ["[1, 2]", '{"sensor.a": 5}', "not json"])
    def test_an_unexpected_shape_is_left_alone(
        self, db: WactorzDB, store: PickleStore, stored: str
    ) -> None:
        _raw_kv(db, "anomaly", "baselines", stored)

        _upgrade_baselines(db, store)

        value = db.conn.execute(
            "SELECT value FROM kv_store WHERE agent='anomaly' AND key='baselines'"
        ).fetchone()[0]
        assert value == stored

    def test_one_bad_row_does_not_stop_the_others(self, db: WactorzDB, store: PickleStore) -> None:
        _raw_kv(db, "broken", "baselines", "not json")
        _raw_kv(db, "fine", "baselines", json.dumps({"s": {}}))

        _upgrade_baselines(db, store)

        assert "ready" in _kv_row(db, "fine", "baselines")[0]["s"]


class TestBaselinesInPickles:
    def test_missing_fields_are_filled_in_the_state_file(
        self, db: WactorzDB, store: PickleStore
    ) -> None:
        store.save("anomaly", {"baselines": {"s": {"mean": 1}}, "other": "kept"})

        _upgrade_baselines(db, store)

        state = store.load("anomaly")
        assert state["baselines"]["s"]["p99"] == 0.0
        assert state["baselines"]["s"]["mean"] == 1
        assert state["other"] == "kept"

    @pytest.mark.parametrize(
        "state",
        [{"no": "baselines"}, {"baselines": [1]}, {"baselines": {"s": 3}}, ["not", "a", "dict"]],
    )
    def test_an_unexpected_shape_is_left_alone(
        self, db: WactorzDB, store: PickleStore, tmp_path: Path, state: Any
    ) -> None:
        path = tmp_path / "state" / "anomaly" / "state.pkl"
        path.parent.mkdir(parents=True)
        path.write_bytes(pickle.dumps(state))
        before = path.stat().st_mtime_ns

        _upgrade_baselines(db, store)

        assert path.stat().st_mtime_ns == before

    def test_an_unreadable_file_does_not_stop_the_others(
        self, db: WactorzDB, store: PickleStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        broken = tmp_path / "state" / "aaa-broken" / "state.pkl"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"not a pickle")
        store.save("zzz-fine", {"baselines": {"s": {}}})

        _upgrade_baselines(db, store)

        assert "ready" in store.load("zzz-fine")["baselines"]["s"]
        assert "aaa-broken" in caplog.text

    def test_stray_files_and_empty_directories_are_ignored(
        self, db: WactorzDB, store: PickleStore, tmp_path: Path
    ) -> None:
        (tmp_path / "state" / "notes.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "state" / "empty").mkdir()

        _upgrade_baselines(db, store)

        assert (tmp_path / "state" / "notes.txt").read_text(encoding="utf-8") == "hello"

    def test_a_database_without_the_table_still_upgrades_pickles(
        self, bare: BareDB, store: PickleStore
    ) -> None:
        store.save("anomaly", {"baselines": {"s": {}}})

        _upgrade_baselines(bare, store)

        assert "ready" in store.load("anomaly")["baselines"]["s"]


class TestConversationHistory:
    def test_corrupt_entries_are_removed_and_the_rest_normalised(self, db: WactorzDB) -> None:
        history = [
            {"role": "user", "content": "hello", "ts": 10},
            "not a dict",
            {"role": "system", "content": "internal"},
            {"role": "assistant", "content": "   "},
            {"role": "assistant", "content": 42, "ts": "yesterday"},
            {"content": "no role"},
        ]
        _raw_kv(db, "main", "conversation_history", json.dumps(history))

        _upgrade_conversation_history(db, None)

        clean, _ = _kv_row(db, "main", "conversation_history")
        assert clean == [
            {"role": "user", "content": "hello", "ts": 10},
            {"role": "assistant", "content": "42"},
        ]

    def test_a_clean_history_is_not_rewritten(self, db: WactorzDB) -> None:
        history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
        _raw_kv(db, "main", "conversation_history", json.dumps(history))

        _upgrade_conversation_history(db, None)

        assert _kv_row(db, "main", "conversation_history") == (history, 1.0)

    @pytest.mark.parametrize("stored", ['{"not": "a list"}', "not json"])
    def test_an_unexpected_shape_is_left_alone(self, db: WactorzDB, stored: str) -> None:
        _raw_kv(db, "main", "conversation_history", stored)

        _upgrade_conversation_history(db, None)

        value = db.conn.execute(
            "SELECT value FROM kv_store WHERE key='conversation_history'"
        ).fetchone()[0]
        assert value == stored

    def test_a_database_without_the_table_is_not_an_error(self, bare: BareDB) -> None:
        _upgrade_conversation_history(bare, None)


class TestTopicContracts:
    @staticmethod
    def _contract(db: WactorzDB, name: str, samples: str | None) -> None:
        db.conn.execute(
            "INSERT INTO topic_contracts (name, observed_samples) VALUES (?, ?)", (name, samples)
        )
        db.conn.commit()

    @staticmethod
    def _samples(db: WactorzDB, name: str) -> str:
        return db.conn.execute(
            "SELECT observed_samples FROM topic_contracts WHERE name=?", (name,)
        ).fetchone()[0]

    @pytest.mark.parametrize("stored", ["[1, 2]", "not json"])
    def test_samples_that_are_not_a_dict_are_reset(self, db: WactorzDB, stored: str) -> None:
        self._contract(db, "sensor", stored)

        _upgrade_topic_contracts(db)

        assert self._samples(db, "sensor") == "{}"

    def test_valid_samples_are_kept(self, db: WactorzDB) -> None:
        samples = json.dumps({"t": {"fields": {"temp": "float"}}})
        self._contract(db, "sensor", samples)

        _upgrade_topic_contracts(db)

        assert self._samples(db, "sensor") == samples

    def test_empty_samples_are_kept(self, db: WactorzDB) -> None:
        self._contract(db, "sensor", "")

        _upgrade_topic_contracts(db)

        assert self._samples(db, "sensor") == ""

    def test_a_database_without_the_table_is_not_an_error(self, bare: BareDB) -> None:
        _upgrade_topic_contracts(bare)


class TestStampSpawnRegistry:
    def test_older_and_unversioned_entries_are_stamped(self, db: WactorzDB) -> None:
        _add_spawn(db, "old", _dynamic("x"), version=1)
        _add_spawn(db, "unknown", _dynamic("x"), version=None)

        _stamp_spawn_registry(db)

        versions = dict(db.conn.execute("SELECT name, framework_version FROM spawn_registry"))
        assert versions == {"old": FRAMEWORK_VERSION, "unknown": FRAMEWORK_VERSION}

    def test_a_registry_without_the_column_is_not_an_error(self, tmp_path: Path) -> None:
        with WactorzDB(str(tmp_path / "unmigrated.db")) as unmigrated:
            _stamp_spawn_registry(unmigrated)


class TestMigrateState2:
    def test_it_runs_every_upgrade(self, db: WactorzDB, store: PickleStore) -> None:
        _raw_kv(db, "anomaly", "baselines", json.dumps({"s": {}}))
        _raw_kv(db, "main", "conversation_history", json.dumps(["junk"]))
        _add_spawn(db, "old", _dynamic("x"), version=1)

        migrate_state_2(db, store)

        assert "ready" in _kv_row(db, "anomaly", "baselines")[0]["s"]
        assert _kv_row(db, "main", "conversation_history")[0] == []
        version = db.conn.execute("SELECT framework_version FROM spawn_registry").fetchone()[0]
        assert version == FRAMEWORK_VERSION


@pytest.fixture(name="api_changes")
def api_changes_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A pretend API change at the current version, so version-gap checks have work."""
    changes: dict[str, Any] = {
        "removed_methods": ["gone"],
        "renamed_methods": {"old_name": "new_name"},
    }
    monkeypatch.setitem(migrations._API_CHANGES, FRAMEWORK_VERSION, changes)
    return changes


class TestValidateAgainstApiChanges:
    def test_a_removed_method_is_an_error(self, db: WactorzDB, api_changes: Any) -> None:
        _add_spawn(db, "a", _dynamic("await agent.gone()"), version=1)

        issues = validate_spawn_registry(db)

        errors = [i for i in issues if i["severity"] == "error"]
        assert len(errors) == 1
        assert "agent.gone()" in errors[0]["message"]
        assert errors[0]["action"] == "needs_respawn"

    def test_a_renamed_method_is_an_error_naming_the_new_one(
        self, db: WactorzDB, api_changes: Any
    ) -> None:
        _add_spawn(db, "a", _dynamic("agent.old_name(1)"), version=1)

        issues = validate_spawn_registry(db)

        (error,) = [i for i in issues if i["severity"] == "error"]
        assert "agent.new_name()" in error["message"]

    def test_an_old_agent_also_gets_a_version_gap_warning(
        self, db: WactorzDB, api_changes: Any
    ) -> None:
        _add_spawn(db, "a", _dynamic("pass"), version=None)

        (warning,) = validate_spawn_registry(db)

        assert warning["severity"] == "warning"
        assert warning["version_gap"] == f"v1 → v{FRAMEWORK_VERSION}"

    @pytest.mark.parametrize(
        "config",
        [
            {"type": "llm", "code": "agent.gone()"},
            {"type": "dynamic", "code": ""},
            _dynamic("agent.gone()", trusted=True),
        ],
    )
    def test_agents_without_checkable_code_are_skipped(
        self, db: WactorzDB, api_changes: Any, config: dict[str, Any]
    ) -> None:
        _add_spawn(db, "a", config, version=1)

        assert validate_spawn_registry(db) == []


class TestAutoFix:
    def test_a_renamed_method_is_rewritten_and_the_entry_stamped(
        self, db: WactorzDB, api_changes: Any
    ) -> None:
        _add_spawn(db, "a", _dynamic("x = agent.old_name(1)\ny = agent.old_name(2)"), version=1)

        fixes = auto_fix_spawn_registry(db)

        assert fixes == ["'a': renamed agent.old_name → agent.new_name"]
        config, version = db.conn.execute(
            "SELECT config, framework_version FROM spawn_registry WHERE name='a'"
        ).fetchone()
        assert json.loads(config)["code"] == "x = agent.new_name(1)\ny = agent.new_name(2)"
        assert json.loads(config)["framework_version"] == FRAMEWORK_VERSION
        assert version == FRAMEWORK_VERSION

    @pytest.mark.parametrize(
        "config",
        [
            "not json",
            {"type": "llm", "code": "agent.old_name()"},
            _dynamic("agent.old_name()", trusted=True),
            _dynamic("agent.other()"),
        ],
    )
    def test_nothing_else_is_touched(self, db: WactorzDB, api_changes: Any, config: object) -> None:
        _add_spawn(db, "a", config, version=None)

        assert auto_fix_spawn_registry(db) == []

    def test_a_database_without_the_table_has_nothing_to_fix(self, bare: BareDB) -> None:
        assert auto_fix_spawn_registry(bare) == []


def _refuse(conn: sqlite3.Connection) -> None:
    raise sqlite3.OperationalError("disk I/O error")


class TestRunMigrationsFailures:
    def test_a_failed_sql_migration_stops_the_upgrade_where_it_got_to(
        self, tmp_path: Path, store: PickleStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sql = dict(migrations._SQL_MIGRATIONS)
        sql[FRAMEWORK_VERSION] = _refuse
        monkeypatch.setattr(migrations, "_SQL_MIGRATIONS", sql)

        with WactorzDB(str(tmp_path / "w.db")) as db:
            result = run_migrations(db, store)

            assert result["errors"] == [
                f"SQL migration v{FRAMEWORK_VERSION} failed: disk I/O error"
            ]
            assert get_current_version(db) == FRAMEWORK_VERSION - 1

    def test_a_version_with_no_sql_migration_still_counts_as_reached(
        self, tmp_path: Path, store: PickleStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sql = dict(migrations._SQL_MIGRATIONS)
        del sql[FRAMEWORK_VERSION]
        monkeypatch.setattr(migrations, "_SQL_MIGRATIONS", sql)

        with WactorzDB(str(tmp_path / "w.db")) as db:
            result = run_migrations(db, store)

            assert result["errors"] == []
            assert get_current_version(db) == FRAMEWORK_VERSION

    def test_a_failing_auto_fix_does_not_abort_the_run(
        self, tmp_path: Path, store: PickleStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken(_db: Any) -> list[str]:
            raise RuntimeError("corrupt registry")

        monkeypatch.setattr(migrations, "auto_fix_spawn_registry", _broken)

        with WactorzDB(str(tmp_path / "w.db")) as db:
            result = run_migrations(db, store)

            assert result["spawn_fixes"] == []
            assert get_current_version(db) == FRAMEWORK_VERSION

    def test_the_run_reports_fixes_and_issues_found_on_the_way(
        self, tmp_path: Path, store: PickleStore, api_changes: Any
    ) -> None:
        with WactorzDB(str(tmp_path / "w.db")) as db:
            run_migrations(db, store)
            db.conn.execute(
                "UPDATE schema_version SET framework_version = ?", (FRAMEWORK_VERSION - 1,)
            )
            db.conn.commit()
            _add_spawn(db, "renamed", _dynamic("agent.old_name()"), version=1)
            _add_spawn(db, "removed", _dynamic("agent.gone()"), version=1)

            result = run_migrations(db, store)

            assert result["spawn_fixes"] == ["'renamed': renamed agent.old_name → agent.new_name"]
            by_agent: dict[str, set[str]] = {}
            for issue in result["spawn_issues"]:
                by_agent.setdefault(issue["agent"], set()).add(issue["severity"])
            assert by_agent["removed"] == {"error", "warning"}
            # Fixed and stamped before validation runs, so nothing is left to report.
            assert "renamed" not in by_agent
