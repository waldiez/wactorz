"""Connection lifecycle, write durability, and state-directory containment.

Three defects are pinned here:

* connections were opened and never closed, so a wipe left the file handle (and
  WAL's ``-wal``/``-shm`` companions) open — which is why temp-directory cleanup
  failed on Windows;
* ``write_sensor`` executed its INSERT without committing, masked by a batch
  caller that commits, so any other caller lost the row;
* a pickle path was sanitised by replacing separators, which ``..`` survives —
  and these files are unpickled, so a name that escapes the state directory is a
  code-execution primitive rather than a tidiness problem.
"""

import sqlite3
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from wactorz.core.persistence import PickleStore, WactorzDB


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Generator[WactorzDB, Any, None]:
    store = WactorzDB(str(tmp_path / "wactorz.db"))
    yield store
    store.close()


class TestConnectionLifecycle:
    def test_close_releases_the_connection(self, tmp_path: Path) -> None:
        store = WactorzDB(str(tmp_path / "w.db"))
        store.close()
        with pytest.raises(RuntimeError):
            _ = store.conn

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        store = WactorzDB(str(tmp_path / "w.db"))
        store.close()
        store.close()

    def test_context_manager_closes(self, tmp_path: Path) -> None:
        with WactorzDB(str(tmp_path / "w.db")) as store:
            store.kv_set("a", "k", 1)
        with pytest.raises(RuntimeError):
            _ = store.conn

    def test_context_manager_closes_on_error(self, tmp_path: Path) -> None:
        store = WactorzDB(str(tmp_path / "w.db"))
        with pytest.raises(ValueError), store:
            raise ValueError("boom")
        with pytest.raises(RuntimeError):
            _ = store.conn

    def test_file_is_released(self, tmp_path: Path) -> None:
        """The point of closing: nothing holds the file afterwards."""
        path = tmp_path / "w.db"
        store = WactorzDB(str(path))
        store.kv_set("a", "k", 1)
        store.close()
        path.unlink()
        assert not path.exists()


class TestTransaction:
    def test_commits_on_success(self, db: WactorzDB) -> None:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO kv_store (agent, key, value, updated) VALUES (?,?,?,?)",
                ("a", "k", '"v"', 0.0),
            )
        assert db.kv_get("a", "k") == "v"

    def test_rolls_back_on_failure(self, db: WactorzDB) -> None:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute(
                "INSERT INTO kv_store (agent, key, value, updated) VALUES (?,?,?,?)",
                ("a", "k", '"v"', 0.0),
            )
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (None,))
        assert db.kv_get("a", "k") is None

    def test_is_reentrant(self, db: WactorzDB) -> None:
        """A method holding the lock may call another that takes it."""
        with db.transaction():
            db.kv_set("a", "k", 1)
        assert db.kv_get("a", "k") == 1

    def test_serialises_concurrent_writers(self, db: WactorzDB) -> None:
        """Interleaved transactions on one connection can commit each other's work."""
        errors: list[Exception] = []

        def writer(n: int) -> None:
            try:
                for i in range(20):
                    with db.transaction() as conn:
                        conn.execute(
                            "INSERT INTO kv_store (agent, key, value, updated) VALUES (?,?,?,?)",
                            (f"agent{n}", f"k{i}", "1", 0.0),
                        )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"concurrent writers failed: {errors}"
        for n in range(4):
            assert len(db.kv_all(f"agent{n}")) == 20


class TestSensorWritesAreDurable:
    def test_write_sensor_commits(self, tmp_path: Path) -> None:
        """Read the row back through a second connection — an uncommitted
        INSERT is invisible there even though the writer can still see it."""
        path = tmp_path / "w.db"
        store = WactorzDB(str(path))
        try:
            store.write_sensor(1.0, "t", "e", "temp", 21.5)
        finally:
            pass
        other = sqlite3.connect(str(path))
        try:
            rows = other.execute("SELECT value FROM sensor_readings").fetchall()
        finally:
            other.close()
            store.close()
        assert rows == [(21.5,)]


class TestPickleStoreContainment:
    @pytest.mark.parametrize("name", ["..", ".", "", "   ", "../../etc/passwd", "..\\..\\win"])
    def test_rejects_names_that_name_no_directory(self, tmp_path: Path, name: str) -> None:
        """Empty, ``.`` and anything leading with ``..`` name no directory —
        they are the base itself or an attempt to leave it. Separators elsewhere
        are flattened rather than rejected, so a merely odd name still gets a
        home instead of crashing its agent."""
        store = PickleStore(str(tmp_path))
        with pytest.raises(ValueError):
            store._path(name)

    @pytest.mark.parametrize("name", ["a/../../b", "x/y", "a\\b"])
    def test_escaping_names_stay_inside_the_base(self, tmp_path, name: str) -> None:
        store = PickleStore(str(tmp_path))
        assert tmp_path.resolve() in store._path(name).parents

    def test_ordinary_name_still_works(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        path = store._path("temp-monitor")
        assert path == tmp_path.resolve() / "temp-monitor" / "state.pkl"

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        store.save("agent", {"a": 1})
        assert store.load("agent") == {"a": 1}

    def test_base_honours_the_shared_resolver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank env value counts as unset everywhere, or agent state lands
        somewhere the rest of the system never looks."""
        monkeypatch.setenv("WACTORZ_STATE_DIR", "   ")
        monkeypatch.chdir(tmp_path)
        store = PickleStore()
        assert store._base == (tmp_path / "state").resolve()


class TestCloseAllStores:
    def test_close_persistence_releases_the_db(self, tmp_path: Path) -> None:
        from wactorz.core import persistence

        db, _pickle = persistence.init_persistence(state_dir=str(tmp_path), run_migration=False)
        persistence.close_persistence()
        with pytest.raises(RuntimeError):
            _ = db.conn
        assert persistence.get_db() is None

    def test_close_persistence_is_idempotent(self, tmp_path: Path) -> None:
        from wactorz.core import persistence

        persistence.init_persistence(state_dir=str(tmp_path), run_migration=False)
        persistence.close_persistence()
        persistence.close_persistence()

    def test_close_before_init_is_harmless(self) -> None:
        from wactorz.core import persistence

        persistence.close_persistence()

    def test_close_persistence_clears_ephemeral_values(self, tmp_path) -> None:
        """Closing the layer means all of it.

        Ephemeral values are namespaced by agent name, so a value surviving a
        close would be recalled by the next agent spawned under the same name —
        stale metrics and a heartbeat state that no longer matches reality. In
        tests it is the shape that produces order-dependent failures.
        """
        from wactorz.core import persistence

        persistence.init_persistence(state_dir=str(tmp_path), run_migration=False)
        persistence.get_memory_store().set("agent:_agent_metrics", {"messages": 7})
        assert persistence.get_memory_store().get("agent:_agent_metrics")

        persistence.close_persistence()

        assert persistence.get_memory_store().get("agent:_agent_metrics") is None


class TestWritesAreSerialisedAgainstTransactions:
    """The trap the lock exists to close.

    One connection means ``COMMIT`` is global. A helper that executes and
    commits without holding the lock will commit *across* another thread's
    open transaction, publishing half-finished work. The failure needs two
    threads and an open transaction, so nothing in ordinary use reveals it.
    """

    def test_a_helper_cannot_commit_inside_an_open_transaction(self, tmp_path) -> None:
        db = WactorzDB(str(tmp_path / "w.db"))
        inside = threading.Event()
        helper_done = threading.Event()

        def helper() -> None:
            inside.wait(timeout=5)
            db.kv_set("other", "k", "written-by-helper")
            helper_done.set()

        t = threading.Thread(target=helper)
        t.start()
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO kv_store (agent, key, value, updated) VALUES (?,?,?,?)",
                    ("txn", "k", '"pending"', 0.0),
                )
                inside.set()
                # The helper must block here rather than commit our pending row.
                assert not helper_done.wait(timeout=0.5), (
                    "a write helper committed while a transaction was open"
                )
                raise RuntimeError("roll back")
        except RuntimeError:
            pass
        finally:
            t.join(timeout=5)
            rolled_back = db.kv_get("txn", "k")
            helper_value = db.kv_get("other", "k")
            db.close()

        assert rolled_back is None, "the transaction's row survived a rollback"
        assert helper_value == "written-by-helper", "the helper's own write was lost"


class TestHelpersComposeInsideTransactions:
    """A helper called inside ``transaction()`` must not commit it early.

    The lock stops *other* threads committing through an open transaction, but
    it is reentrant, so the same thread can defeat the block from the inside.
    Both failure modes are invisible to a single-connection read: the writer
    still sees its own uncommitted rows.
    """

    def test_helper_write_lands_when_the_block_ends(self, tmp_path) -> None:
        path = tmp_path / "w.db"
        with WactorzDB(str(path)) as db, db.transaction():
            db.kv_set("a", "k", "inside")
        with WactorzDB(str(path)) as reopened:
            assert reopened.kv_get("a", "k") == "inside"

    def test_helper_write_rolls_back_with_the_block(self, tmp_path) -> None:
        path = tmp_path / "w.db"
        db = WactorzDB(str(path))
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.kv_set("a", "k", "doomed")
                raise RuntimeError("abandon")
        db.close()
        with WactorzDB(str(path)) as reopened:
            assert reopened.kv_get("a", "k") is None

    def test_nested_blocks_commit_once_at_the_outermost(self, tmp_path) -> None:
        path = tmp_path / "w.db"
        db = WactorzDB(str(path))
        with db.transaction():
            db.kv_set("a", "outer", 1)
            with db.transaction():
                db.kv_set("a", "inner", 2)
            # Still open: the inner block must not have committed.
            assert db._depth == 1
        db.close()
        with WactorzDB(str(path)) as reopened:
            assert reopened.kv_get("a", "outer") == 1
            assert reopened.kv_get("a", "inner") == 2
