"""A value the JSON-backed stores cannot hold is refused at the write.

Stored as its ``str()``, a datetime or a numpy float came back as a string after
the next read, and the code reading it failed somewhere far from the line that
wrote it. Refused at the write, the error names the key and what to do instead.
"""

import datetime
from pathlib import Path

import pytest

from wactorz.core.persistence import PersistenceAPI, WactorzDB
from wactorz.core.persistence.json_value import NotJsonError
from wactorz.core.persistence.memory_store import MemoryStore
from wactorz.core.persistence.pickle_store import PickleStore

WHEN = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path):
    with WactorzDB(str(tmp_path / "wactorz.db")) as db:
        yield db


class TestSqlite:
    def test_a_datetime_is_refused_with_the_key_named(self, db: WactorzDB) -> None:
        with pytest.raises(NotJsonError, match=r"main: '_user_facts' is kept as JSON"):
            db.kv_set("main", "_user_facts", {"since": WHEN})

    def test_nothing_is_written_and_the_old_value_stays(self, db: WactorzDB) -> None:
        db.kv_set("main", "_user_facts", {"name": "Ada"})

        with pytest.raises(NotJsonError):
            db.kv_set("main", "_user_facts", {"since": WHEN})

        assert db.kv_get("main", "_user_facts") == {"name": "Ada"}

    def test_it_is_a_type_error(self, db: WactorzDB) -> None:
        # So code that already guards a write with `except TypeError` still does.
        with pytest.raises(TypeError):
            db.kv_set("main", "_user_facts", {1, 2})

    def test_what_json_can_hold_round_trips_as_it_was(self, db: WactorzDB) -> None:
        value = {"when": WHEN.isoformat(), "n": 1.5, "tags": ["a"], "none": None}

        db.kv_set("main", "_user_facts", value)

        assert db.kv_get("main", "_user_facts") == value


class TestMemory:
    def test_a_set_is_refused(self) -> None:
        with pytest.raises(NotJsonError, match="'agent:_agent_metrics'"):
            MemoryStore().set("agent:_agent_metrics", {1, 2})


class TestThroughPersist:
    def test_a_routed_key_refuses_while_any_other_takes_anything(
        self, tmp_path: Path, db: WactorzDB
    ) -> None:
        # Only the keys routed to SQLite are kept as JSON; an agent's own keys
        # are pickled and may hold a datetime.
        api = PersistenceAPI(db, PickleStore(str(tmp_path)), "main")

        with pytest.raises(NotJsonError):
            api.set("_user_facts", {"since": WHEN})
        api.set("last_seen", WHEN)

        assert api.get("last_seen") == WHEN
