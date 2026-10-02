"""Moving state out of the pre-SQLite pickle files, once, without clobbering.

It runs on every start, so it must be a no-op the second time: a key SQLite
already holds is newer than the pickle's copy and is left alone. Only the keys
routed to SQLite or process memory move; everything else stays in the pickle,
which is still where it belongs.
"""

import pickle
from collections.abc import Iterator
from pathlib import Path

import pytest

from wactorz.core.persistence import WactorzDB
from wactorz.core.persistence.legacy_pickle import migrate_from_pickle
from wactorz.core.persistence.stores import get_memory_store


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path) -> Iterator[WactorzDB]:
    with WactorzDB(str(tmp_path / "wactorz.db")) as db:
        yield db


def _state(base: Path, agent: str, content: object) -> None:
    (base / agent).mkdir(parents=True)
    (base / agent / "state.pkl").write_bytes(pickle.dumps(content))


def test_a_missing_state_directory_is_nothing_to_do(tmp_path: Path, db: WactorzDB) -> None:
    migrate_from_pickle(str(tmp_path / "absent"), db)

    assert db.kv_all("main") == {}


def test_durable_and_ephemeral_keys_move_and_the_rest_stays(tmp_path: Path, db: WactorzDB) -> None:
    base = tmp_path / "state"
    _state(
        base,
        "main",
        {"_user_facts": {"pref_name": "Ada"}, "_agent_metrics": {"cpu": 1}, "model": b"bytes"},
    )

    migrate_from_pickle(str(base), db)

    assert db.kv_get("main", "_user_facts") == {"pref_name": "Ada"}
    assert get_memory_store().get("main:_agent_metrics") == {"cpu": 1}
    assert db.kv_get("main", "model") is None


def test_a_key_sqlite_already_holds_is_not_overwritten(tmp_path: Path, db: WactorzDB) -> None:
    base = tmp_path / "state"
    db.kv_set("main", "_user_facts", {"pref_name": "newer"})
    _state(base, "main", {"_user_facts": {"pref_name": "stale"}})

    migrate_from_pickle(str(base), db)

    assert db.kv_get("main", "_user_facts") == {"pref_name": "newer"}


def test_unreadable_and_unexpected_files_are_skipped(tmp_path: Path, db: WactorzDB) -> None:
    base = tmp_path / "state"
    base.mkdir()
    (base / "stray.txt").write_text("not a directory", encoding="utf-8")
    (base / "empty").mkdir()
    (base / "broken").mkdir()
    (base / "broken" / "state.pkl").write_bytes(b"not a pickle")
    _state(base, "listy", ["not", "a", "dict"])
    _state(base, "fine", {"_pipeline_rules": {"r1": {}}})

    migrate_from_pickle(str(base), db)

    assert db.kv_get("fine", "_pipeline_rules") == {"r1": {}}
