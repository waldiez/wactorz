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
    _upgrade_baselines,
    _upgrade_conversation_history,
    get_current_version,
    migrate_state_2,
    run_migrations,
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
        [{"no": "baselines"}, {"baselines": [1]}, {"baselines": {"s": 3}}],
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

    def test_a_file_that_is_not_a_state_is_moved_aside_not_rewritten(
        self, db: WactorzDB, store: PickleStore, tmp_path: Path
    ) -> None:
        # Read through the store, which treats it as it would at an agent's
        # start: kept under another name, out of the next save's way.
        path = tmp_path / "state" / "anomaly" / "state.pkl"
        path.parent.mkdir(parents=True)
        path.write_bytes(pickle.dumps(["not", "a", "dict"]))

        _upgrade_baselines(db, store)

        (kept,) = path.parent.glob("state.pkl.corrupt.*")
        assert pickle.loads(kept.read_bytes()) == ["not", "a", "dict"]

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


class TestMigrateState2:
    def test_it_runs_every_upgrade(self, db: WactorzDB, store: PickleStore) -> None:
        _raw_kv(db, "anomaly", "baselines", json.dumps({"s": {}}))
        _raw_kv(db, "main", "conversation_history", json.dumps(["junk"]))

        migrate_state_2(db, store)

        assert "ready" in _kv_row(db, "anomaly", "baselines")[0]["s"]
        assert _kv_row(db, "main", "conversation_history")[0] == []


#: Tables earlier releases created and nothing ever wrote. What they were for
#: lives in `kv_store`, under `_spawned_agents`, `_pipeline_rules` and the like.
UNUSED_TABLES = (
    "spawn_registry",
    "pipeline_rules",
    "user_facts",
    "topic_contracts",
    "webhook_urls",
    "plan_cache",
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class TestTablesNothingWrote:
    def test_a_new_database_is_created_without_them(self, db: WactorzDB) -> None:
        assert not _tables(db.conn) & set(UNUSED_TABLES)

    def test_an_older_database_keeps_them_as_they_are(
        self, tmp_path: Path, store: PickleStore
    ) -> None:
        # Empty in every install, but not dropped: nothing destructive runs on
        # a user's database for the sake of tidiness.
        path = tmp_path / "old.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE spawn_registry (name TEXT PRIMARY KEY, config TEXT)")
            conn.execute("INSERT INTO spawn_registry VALUES ('kept', '{}')")
        conn.close()

        with WactorzDB(str(path)) as old:
            result = run_migrations(old, store)

            assert result["errors"] == []
            rows = old.conn.execute("SELECT name FROM spawn_registry").fetchall()
            assert [tuple(r) for r in rows] == [("kept",)]


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
