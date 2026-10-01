"""An agent's pickled state lives in memory, and its file is written after.

`persist()` and `recall()` are synchronous and called from the event loop. A
key that is not kept in SQLite used to cost, on every call, the whole state
file read and unpickled -- and on a persist, pickled, forced to disk and
renamed as well, with every actor in the process waiting. So the state is read
once and kept: a recall is a lookup, a persist changes the copy in memory, and
the file is written a moment later from a thread.

The copy in memory is then the state, which is what the rest of this file is
about: everything that changes or removes a state file in a running server has
to do it through that copy, or the next write puts back what it removed.
"""

import asyncio
import pickle
from pathlib import Path
from typing import Any

import pytest

from wactorz import reset
from wactorz.core import deferred_write
from wactorz.core.persistence import PersistenceAPI, WactorzDB, stores
from wactorz.core.persistence.pickle_store import PickleStore

#: The delay these tests give the store's writer, in place of the real one.
DELAY_S = 0.1


def _on_disk(tmp_path: Path, agent: str) -> dict[str, Any]:
    """What a restart would read for ``agent``."""
    return pickle.loads((tmp_path / agent / "state.pkl").read_bytes())


def _file(tmp_path: Path, agent: str) -> Path:
    return tmp_path / agent / "state.pkl"


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> PickleStore:
    store = PickleStore(str(tmp_path))
    store._writer._delay = DELAY_S
    return store


@pytest.fixture(name="api")
def api_fixture(tmp_path: Path, store: PickleStore) -> PersistenceAPI:
    return PersistenceAPI(WactorzDB(tmp_path / "wactorz.db"), store, "worker")


async def _after_the_delay() -> None:
    await asyncio.sleep(DELAY_S * 4)


class TestPersistingUnderARunningLoop:
    async def test_the_value_is_there_at_once_and_the_file_a_moment_later(
        self, api: PersistenceAPI, tmp_path: Path
    ) -> None:
        api.set("calibration", {"offset": 3})

        assert api.get("calibration") == {"offset": 3}
        assert not _file(tmp_path, "worker").exists()
        await _after_the_delay()
        assert _on_disk(tmp_path, "worker") == {"calibration": {"offset": 3}}

    async def test_a_value_persisted_every_tick_is_written_once(
        self, api: PersistenceAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        written: list[Path] = []
        real = deferred_write.write_bytes

        def _counting(path: Path, data: bytes) -> None:
            written.append(path)
            real(path, data)

        monkeypatch.setattr(deferred_write, "write_bytes", _counting)

        for tick in range(200):
            api.set("ticks", tick)
        await _after_the_delay()

        assert len(written) == 1
        assert _on_disk(tmp_path, "worker") == {"ticks": 199}

    async def test_removing_a_key_reaches_the_file(
        self, api: PersistenceAPI, tmp_path: Path
    ) -> None:
        api.set("keep", 1)
        api.set("drop", 2)

        api.delete("drop")
        await _after_the_delay()

        assert api.get("drop") is None
        assert _on_disk(tmp_path, "worker") == {"keep": 1}

    async def test_a_persist_from_a_thread_lands(self, api: PersistenceAPI, tmp_path: Path) -> None:
        # Agent code is told to run blocking work on threads, and persists there.
        api.set("from_the_loop", 1)

        await asyncio.to_thread(api.set, "from_a_thread", 2)
        await _after_the_delay()

        assert _on_disk(tmp_path, "worker") == {"from_the_loop": 1, "from_a_thread": 2}


class TestRecalling:
    def test_the_file_is_read_once(
        self, api: PersistenceAPI, store: PickleStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api.set("calibration", 3)
        reads: list[str] = []
        real = store._read

        def _counting(agent_name: str) -> dict[str, Any]:
            reads.append(agent_name)
            return real(agent_name)

        monkeypatch.setattr(store, "_read", _counting)

        for _ in range(100):
            assert api.get("calibration") == 3

        assert reads == []

    def test_a_restart_reads_what_was_written(self, tmp_path: Path) -> None:
        first = PickleStore(str(tmp_path))
        first.update("worker", "calibration", 3)
        first.flush()
        del first

        # Nothing holds the directory's states any more, as after a restart.
        again = PickleStore(str(tmp_path))

        assert again.load("worker") == {"calibration": 3}


class TestTwoStoresOverOneDirectory:
    def test_they_see_one_state(self, tmp_path: Path) -> None:
        # A reset builds a store of its own. One with its own copy would write
        # back, later, the state the other had changed.
        one, other = PickleStore(str(tmp_path)), PickleStore(str(tmp_path))

        one.update("worker", "count", 1)

        assert other.load("worker") == {"count": 1}

    def test_stores_over_different_directories_do_not(self, tmp_path: Path) -> None:
        one, other = PickleStore(str(tmp_path / "a")), PickleStore(str(tmp_path / "b"))

        one.update("worker", "count", 1)

        assert other.load("worker") == {}


class TestDeleting:
    async def test_a_state_waiting_to_be_written_does_not_come_back(
        self, store: PickleStore, tmp_path: Path
    ) -> None:
        store.update("worker", "count", 1)

        store.delete("worker")
        await _after_the_delay()

        assert not _file(tmp_path, "worker").exists()
        assert store.load("worker") == {}

    async def test_an_agent_spawned_again_under_the_name_starts_clean(
        self, store: PickleStore, tmp_path: Path
    ) -> None:
        store.update("worker", "old", 1)
        store.delete("worker")

        store.update("worker", "new", 2)
        await _after_the_delay()

        assert _on_disk(tmp_path, "worker") == {"new": 2}


class TestAResetInARunningServer:
    """`/api/reset` runs these in the server's own process, beside the store it uses."""

    async def test_a_cleared_conversation_stays_cleared_after_the_next_persist(
        self, store: PickleStore, tmp_path: Path
    ) -> None:
        store.save("main", {"conversation_history": [{"role": "user"}], "notes": 1})
        store.flush()

        reset._strip_chat_from_pickles(None, str(tmp_path))
        store.update("main", "notes", 2)
        await _after_the_delay()

        assert _on_disk(tmp_path, "main") == {"notes": 2}

    async def test_deleted_state_stays_deleted_after_the_next_persist(
        self, store: PickleStore, tmp_path: Path
    ) -> None:
        store.save("main", {"notes": 1})
        store.save("worker", {"count": 1})
        store.flush()

        reset._reset_all_pickles(str(tmp_path))
        store.update("worker", "count", 2)
        await _after_the_delay()

        assert not _file(tmp_path, "main").exists()
        assert _on_disk(tmp_path, "worker") == {"count": 2}


class TestShuttingDown:
    async def test_closing_the_stores_writes_what_is_waiting(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        stores.install_stores(WactorzDB(tmp_path / "wactorz.db"), store)
        try:
            store.update("worker", "count", 1)
            assert not _file(tmp_path, "worker").exists()
        finally:
            stores.close_stores()

        assert _on_disk(tmp_path, "worker") == {"count": 1}
