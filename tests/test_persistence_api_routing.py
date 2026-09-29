"""Each key persisted through an actor lands in the store that suits it.

Agent code calls `persist`/`recall` without knowing where a value lives, so the
routing is the whole contract: a durable key that fell through to process memory
would vanish on restart, and an ephemeral one sent to SQLite would add a write
per heartbeat. These pin the routing, the bulk load that migration relies on and
the purge that deletion relies on — including that one failing backend does not
stop the others.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wactorz.core.persistence import (
    EPHEMERAL_KEYS,
    SQLITE_KEYS,
    PersistenceAPI,
    PickleStore,
    WactorzDB,
)
from wactorz.core.persistence.stores import get_memory_store

DURABLE = "_user_facts"
EPHEMERAL = "_agent_metrics"
ARBITRARY = "model_weights"


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Iterator[WactorzDB]:
    store = WactorzDB(str(tmp_path / "wactorz.db"))
    yield store
    store.close()


@pytest.fixture(name="pickles")
def pickles_fixture(tmp_path: Path) -> PickleStore:
    return PickleStore(str(tmp_path / "state"))


@pytest.fixture(name="api")
def api_fixture(db: WactorzDB, pickles: PickleStore) -> PersistenceAPI:
    return PersistenceAPI(db, pickles, "sensor")


class _Failing:
    """Stands in for a store whose every call raises."""

    def __getattr__(self, name: str) -> Any:
        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(f"{name} unavailable")

        return _raise


def test_the_example_keys_are_routed_the_way_these_tests_assume() -> None:
    assert DURABLE in SQLITE_KEYS
    assert EPHEMERAL in EPHEMERAL_KEYS
    assert ARBITRARY not in SQLITE_KEYS | EPHEMERAL_KEYS


class TestRouting:
    def test_a_durable_key_goes_to_sqlite(
        self, api: PersistenceAPI, db: WactorzDB, pickles: PickleStore
    ) -> None:
        api.set(DURABLE, {"name": "Ada"})

        assert db.kv_get("sensor", DURABLE) == {"name": "Ada"}
        assert pickles.load("sensor") == {}

    def test_an_ephemeral_key_goes_to_process_memory(
        self, api: PersistenceAPI, db: WactorzDB
    ) -> None:
        api.set(EPHEMERAL, {"cpu": 3})

        assert get_memory_store().get(f"sensor:{EPHEMERAL}") == {"cpu": 3}
        assert db.kv_all("sensor") == {}

    def test_anything_else_goes_to_the_pickle(
        self, api: PersistenceAPI, db: WactorzDB, pickles: PickleStore
    ) -> None:
        api.set(ARBITRARY, [1, 2, 3])

        assert pickles.load("sensor") == {ARBITRARY: [1, 2, 3]}
        assert db.kv_all("sensor") == {}

    def test_pickled_keys_are_merged_not_replaced(
        self, api: PersistenceAPI, pickles: PickleStore
    ) -> None:
        api.set("a", 1)
        api.set("b", 2)

        assert pickles.load("sensor") == {"a": 1, "b": 2}

    @pytest.mark.parametrize("key", [DURABLE, EPHEMERAL, ARBITRARY])
    def test_what_is_set_is_what_is_got(self, api: PersistenceAPI, key: str) -> None:
        api.set(key, {"v": 1})

        assert api.get(key) == {"v": 1}

    @pytest.mark.parametrize("key", [DURABLE, EPHEMERAL, ARBITRARY])
    def test_a_missing_key_gives_the_default(self, api: PersistenceAPI, key: str) -> None:
        assert api.get(key, "fallback") == "fallback"

    @pytest.mark.parametrize("key", [DURABLE, EPHEMERAL, ARBITRARY])
    def test_a_deleted_key_is_gone(self, api: PersistenceAPI, key: str) -> None:
        api.set(key, 1)

        api.delete(key)

        assert api.get(key) is None

    def test_deleting_a_pickled_key_keeps_the_rest(self, api: PersistenceAPI) -> None:
        api.set("a", 1)
        api.set("b", 2)

        api.delete("a")

        assert api.all() == {"b": 2}

    def test_agents_do_not_see_each_others_values(
        self, db: WactorzDB, pickles: PickleStore
    ) -> None:
        one = PersistenceAPI(db, pickles, "one")
        two = PersistenceAPI(db, pickles, "two")

        for key in (DURABLE, EPHEMERAL, ARBITRARY):
            one.set(key, "mine")

        assert two.all() == {}


class TestAll:
    def test_it_gathers_every_backend(self, api: PersistenceAPI) -> None:
        api.set(DURABLE, "d")
        api.set(EPHEMERAL, "e")
        api.set(ARBITRARY, "p")

        assert api.all() == {DURABLE: "d", EPHEMERAL: "e", ARBITRARY: "p"}

    def test_an_empty_agent_has_nothing(self, api: PersistenceAPI) -> None:
        assert api.all() == {}


class TestLoadSnapshot:
    def test_the_snapshot_round_trips_through_all(self, api: PersistenceAPI) -> None:
        snapshot = {DURABLE: "d", EPHEMERAL: "e", ARBITRARY: "p", "other": 2}

        applied = api.load_snapshot(snapshot)

        assert applied == {"sqlite": 1, "memory": 1, "pickle": 2}
        assert api.all() == snapshot

    def test_it_replaces_what_was_there_by_default(self, api: PersistenceAPI) -> None:
        api.set(DURABLE, "stale")
        api.set("leftover", 1)

        api.load_snapshot({ARBITRARY: "fresh"})

        assert api.all() == {ARBITRARY: "fresh"}

    def test_merge_keeps_durable_values_not_in_the_snapshot(self, api: PersistenceAPI) -> None:
        api.set(DURABLE, "kept")

        api.load_snapshot({ARBITRARY: "new"}, replace=False)

        assert api.all() == {DURABLE: "kept", ARBITRARY: "new"}

    def test_something_other_than_a_mapping_loads_nothing(self, api: PersistenceAPI) -> None:
        api.set(DURABLE, "stale")

        applied = api.load_snapshot(["not", "a", "dict"])  # pyright: ignore[reportArgumentType]

        assert applied == {"sqlite": 0, "memory": 0, "pickle": 0}
        assert api.all() == {}

    def test_an_unwritable_key_does_not_stop_the_rest(
        self, api: PersistenceAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_set = api.db.kv_set

        def _refuse_facts(agent: str, key: str, value: Any) -> None:
            if key == DURABLE:
                raise TypeError("not serialisable")
            real_set(agent, key, value)

        monkeypatch.setattr(api.db, "kv_set", _refuse_facts)

        applied = api.load_snapshot({DURABLE: "bad", "_pipeline_rules": {}, ARBITRARY: 1})

        assert applied == {"sqlite": 1, "memory": 0, "pickle": 1}
        assert api.get("_pipeline_rules") == {}

    def test_a_failing_pickle_write_is_reported_as_nothing_applied(
        self, db: WactorzDB, pickles: PickleStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = PersistenceAPI(db, pickles, "sensor")

        def _refuse(*_args: Any) -> bool:
            raise OSError("disk full")

        monkeypatch.setattr(pickles, "save", _refuse)

        applied = api.load_snapshot({DURABLE: "d", ARBITRARY: 1}, replace=False)

        assert applied == {"sqlite": 1, "memory": 0, "pickle": 0}


class TestPurge:
    def test_every_backend_is_emptied(self, api: PersistenceAPI, pickles: PickleStore) -> None:
        api.set(DURABLE, "d")
        api.set("_pipeline_rules", {})
        api.set(EPHEMERAL, "e")
        api.set(ARBITRARY, "p")

        summary = api.purge()

        assert summary == {
            "sqlite_rows": 2,
            "memory_keys": len(EPHEMERAL_KEYS),
            "pickle_deleted": True,
        }
        assert api.all() == {}

    def test_other_agents_are_untouched(self, db: WactorzDB, pickles: PickleStore) -> None:
        doomed = PersistenceAPI(db, pickles, "doomed")
        kept = PersistenceAPI(db, pickles, "kept")
        for api in (doomed, kept):
            api.set(DURABLE, "d")
            api.set(EPHEMERAL, "e")
            api.set(ARBITRARY, "p")

        doomed.purge()

        assert kept.all() == {DURABLE: "d", EPHEMERAL: "e", ARBITRARY: "p"}

    def test_a_failing_database_does_not_stop_the_other_stores(
        self, db: WactorzDB, pickles: PickleStore
    ) -> None:
        PersistenceAPI(db, pickles, "sensor").set(ARBITRARY, "p")
        api = PersistenceAPI(_Failing(), pickles, "sensor")  # pyright: ignore[reportArgumentType]

        summary = api.purge()

        assert summary["sqlite_rows"] == 0
        assert summary["pickle_deleted"] is True
        assert pickles.load("sensor") == {}

    def test_a_failing_pickle_store_is_reported(self, db: WactorzDB) -> None:
        api = PersistenceAPI(db, _Failing(), "sensor")  # pyright: ignore[reportArgumentType]
        api.set(DURABLE, "d")

        summary = api.purge()

        assert summary["sqlite_rows"] == 1
        assert summary["pickle_deleted"] is False

    def test_a_failing_memory_delete_is_not_counted(
        self, api: PersistenceAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api, "memory", _Failing())

        summary = api.purge()

        assert summary["memory_keys"] == 0
