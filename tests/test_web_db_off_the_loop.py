"""The web routes read and write the database on a thread, never on the event loop.

A database call on the loop holds every actor in the process, and the broker's
keepalive, for as long as the disk takes. Each route here is called with the
database call it makes wrapped in a `LoopWatch`.
"""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tests.loop_watch import LoopWatch
from wactorz.agents.llm import cost as llm_cost
from wactorz.core.persistence import PickleStore
from wactorz.core.persistence.db import WactorzDB
from wactorz.core.persistence.stores import install_stores
from wactorz.web import cost, runtime
from wactorz.web.app import build_app
from wactorz.web.sessions import SessionStore


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WactorzDB:
    db = WactorzDB(str(tmp_path / "wactorz.db"))
    # Where the spend ledger finds it too, as the running server installs it.
    install_stores(db, PickleStore(str(tmp_path / "pickles")))
    monkeypatch.setattr(runtime, "db", db)
    return db


@pytest.fixture(name="client")
async def client_fixture(db: WactorzDB) -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()
    db.close()


def _watched(monkeypatch: pytest.MonkeyPatch, owner: object, name: str) -> LoopWatch:
    """Wrap ``owner.name`` so each call records whether the loop ran meanwhile."""
    watch = LoopWatch()
    real: Callable[..., Any] = getattr(owner, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        watch.during()
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapped)
    return watch


async def _while_watching(watch: LoopWatch, call: Any) -> Any:
    ticker = asyncio.create_task(watch.tick())
    try:
        return await call
    finally:
        ticker.cancel()


@pytest.mark.parametrize(
    ("method", "path", "called"),
    [
        ("GET", "/api/chats", "query_chat_log"),
        ("GET", "/api/feed", "query_chat_log"),
        ("GET", "/api/feed", "kv_values"),
        ("GET", "/api/actors/someone-gone/history", "kv_get"),
        ("POST", "/api/cost/reset", "kv_set"),
        ("GET", "/api/cost", "kv_get"),
    ],
)
async def test_a_route_touches_the_database_off_the_loop(
    client: TestClient,
    db: WactorzDB,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    called: str,
) -> None:
    monkeypatch.setattr(runtime, "registry", None)
    watch = _watched(monkeypatch, db, called)

    response = await _while_watching(watch, client.request(method, path))

    assert response.status == 200, await response.text()
    assert watch.seen and all(watch.seen), f"{called} held the event loop"


async def test_a_live_agents_history_is_read_off_the_loop(
    client: TestClient, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Agent:
        actor_id = "live"
        name = "live"

        def recall(self, key: str, default: Any = None) -> Any:
            return db.kv_get(self.name, key, default)

    class _Registry:
        def get(self, actor_id: str) -> Any:
            return _Agent() if actor_id == "live" else None

        def find_by_name(self, _name: str) -> None:
            return None

    db.kv_set("live", "conversation_history", [{"role": "user", "content": "hi"}])
    monkeypatch.setattr(runtime, "registry", _Registry())
    watch = _watched(monkeypatch, db, "kv_get")

    response = await _while_watching(watch, client.get("/api/actors/live/history"))

    assert await response.json() == [{"role": "user", "content": "hi"}]
    assert watch.seen == [True], "the live agent's history was read on the loop"


async def test_setting_the_spend_limit_writes_off_the_loop(
    client: TestClient, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch = _watched(monkeypatch, db, "kv_set")

    response = await _while_watching(
        watch, client.post("/api/cost/limit", json={"limit_usd": 5, "period": "daily"})
    )

    assert response.status == 200, await response.text()
    assert watch.seen and all(watch.seen), "kv_set held the event loop"


async def test_a_reset_deletes_the_ledger_with_nothing_landing_in_between(
    client: TestClient, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ledger's memory is cleared and its row deleted in one step of the loop:
    # a metrics write in between would be deleted from disk and kept in memory.
    monkeypatch.setattr(cost, "lifetime_cost", {"agent": 1.0})
    cleared_when_deleted: list[bool] = []
    real = db.kv_delete

    def kv_delete(agent: str, key: str) -> None:
        if key == cost.LIFETIME_LEDGER_KEY:
            cleared_when_deleted.append(threading.current_thread() is threading.main_thread())
        real(agent, key)

    monkeypatch.setattr(db, "kv_delete", kv_delete)

    response = await client.post("/api/cost/reset")

    assert response.status == 200, await response.text()
    assert cleared_when_deleted == [True], "the ledger was deleted off the loop, after an await"
    assert cost.lifetime_cost == {}


def test_the_spend_seed_reads_final_costs_through_the_lock(db: WactorzDB) -> None:
    db.kv_set("gone", "_final_cost", {"name": "gone", "cost_usd": 0.25})
    seen: list[str] = []
    real = db.kv_values

    def kv_values(key: str) -> list[tuple[str, Any]]:
        seen.append(key)
        return real(key)

    db.kv_values = kv_values  # pyright: ignore[reportAttributeAccessIssue]

    assert llm_cost._known_persisted_cost_total(db) == 0.25  # pyright: ignore[reportPrivateUsage]
    assert seen == ["_final_cost"]


async def test_the_agent_list_reads_stored_costs_once_off_the_loop(
    client: TestClient, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Registry:
        def all_actors(self) -> list[Any]:
            return [_Actor("gone-but-billed"), _Actor("also")]

    class _Actor:
        def __init__(self, name: str) -> None:
            self.actor_id = name
            self.name = name

    db.kv_set("gone-but-billed", "_final_cost", {"name": "gone-but-billed", "cost_usd": 0.5})
    monkeypatch.setattr(runtime, "registry", _Registry())
    watch = _watched(monkeypatch, db, "kv_values")

    response = await _while_watching(watch, client.get("/api/actors"))

    cards = {card["name"]: card["costUsd"] for card in await response.json()}
    assert cards == {"gone-but-billed": 0.5, "also": None}
    assert watch.seen == [True], "one read, off the loop, for the whole list"


def test_reading_a_key_across_agents_waits_for_a_write_in_progress(db: WactorzDB) -> None:
    # One connection is shared, so a read that skipped the lock would see, or
    # trip over, another thread's transaction half done.
    inside, finish = threading.Event(), threading.Event()

    def write() -> None:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO kv_store (agent, key, value, updated) VALUES ('a', 'k', '1', 0)"
            )
            inside.set()
            finish.wait(timeout=10)

    writer = threading.Thread(target=write)
    writer.start()
    assert inside.wait(timeout=10)
    seen: list[list[tuple[str, Any]]] = []
    reader = threading.Thread(target=lambda: seen.append(db.kv_values("k")))
    reader.start()

    reader.join(timeout=0.5)
    waited = reader.is_alive()
    finish.set()
    writer.join(timeout=10)
    reader.join(timeout=10)

    assert waited, "the read went ahead of the open transaction"
    assert seen == [[("a", 1)]]


class TestSessions:
    async def test_signing_in_and_out_writes_off_the_loop_and_survives_a_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = SessionStore()
        store.bind(tmp_path, "a-key")
        watch = _watched(monkeypatch, store, "_write")

        kept = await _while_watching(watch, store.started())
        gone = await _while_watching(watch, store.started())
        await _while_watching(watch, store.ended(gone))

        again = SessionStore()
        again.bind(tmp_path, "a-key")
        assert again.is_valid(kept) and not again.is_valid(gone)
        assert watch.seen == [True, True, True]

    async def test_ending_every_session_writes_off_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = SessionStore()
        store.bind(tmp_path, "a-key")
        await store.started()
        watch = _watched(monkeypatch, store, "_write")

        assert await _while_watching(watch, store.all_ended()) == 1
        assert watch.seen == [True]

    def test_an_older_copy_finishing_last_leaves_the_newer_on_disk(self, tmp_path: Path) -> None:
        store = SessionStore()
        store.bind(tmp_path, "a-key")
        first = store.create()
        older = store._copy()  # pyright: ignore[reportPrivateUsage]
        second = store.create()
        newer = store._copy()  # pyright: ignore[reportPrivateUsage]

        store._write_copy(*newer)  # pyright: ignore[reportPrivateUsage]
        store._write_copy(*older)  # pyright: ignore[reportPrivateUsage]

        again = SessionStore()
        again.bind(tmp_path, "a-key")
        assert again.is_valid(first) and again.is_valid(second)
