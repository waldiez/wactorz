"""The dashboard's REST handlers for actors, cost, chat history and the feed.

Each handler answers a malformed request with a 4xx in its own words, a missing
dependency (no registry, no database) with an empty or 503 answer, and a failure
underneath with a message that does not leak the exception. Lifecycle commands
reach remote agents by the id their topic is keyed on, even when asked by name,
and say 503 rather than 200 when there was no way to deliver them.

Driven through the real monitor app on a test server.
"""

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.core.persistence import PickleStore, WactorzDB
from wactorz.core.persistence.stores import install_stores
from wactorz.web import api_actors, chat, cost, lifecycle, runtime, ws
from wactorz.web.app import build_app


class _Metrics:
    messages_processed = 5


class _Actor:
    def __init__(self, actor_id: str, name: str, protected: bool = False) -> None:
        self.actor_id = actor_id
        self.name = name
        self.protected = protected
        self.metrics = _Metrics()
        self.total_cost_usd = 0.5
        self.history: list[dict[str, Any]] = []

    def recall(self, key: str, default: Any = None) -> Any:
        return self.history if key == "conversation_history" else default


class _Registry:
    def __init__(self, *actors: _Actor) -> None:
        self._actors = {a.actor_id: a for a in actors}

    def get(self, actor_id: str) -> _Actor | None:
        return self._actors.get(actor_id)

    def find_by_name(self, name: str) -> _Actor | None:
        return next((a for a in self._actors.values() if a.name == name), None)

    def all_actors(self) -> list[_Actor]:
        return list(self._actors.values())


@pytest.fixture(autouse=True)
def _isolated_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "registry", None)
    monkeypatch.setattr(runtime, "db", None)
    monkeypatch.setitem(runtime.state, "agents", {})
    monkeypatch.setattr(runtime, "deleted_agent_ids", [])


@pytest.fixture(name="client")
async def client_fixture() -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WactorzDB:
    db = WactorzDB(str(tmp_path / "wactorz.db"))
    install_stores(db, PickleStore(str(tmp_path / "pickles")))
    monkeypatch.setattr(runtime, "db", db)
    return db


class TestCost:
    async def test_a_limit_is_validated_then_stored(
        self, client: TestClient, db: WactorzDB
    ) -> None:
        bad_period = await client.post("/api/cost/limit", json={"limit_usd": 5, "period": "hourly"})
        bad_body = await client.post("/api/cost/limit", data="not json")
        ok = await client.post("/api/cost/limit", json={"limit_usd": "5", "period": "daily"})

        assert bad_period.status == 400
        assert bad_body.status == 400
        assert (await bad_body.json())["error"].startswith("limit_usd must be a number")
        assert await ok.json() == {"ok": True, "limit_usd": 5.0, "period": "daily"}

    async def test_a_reset_clears_the_ledger(
        self, client: TestClient, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cost.lifetime_cost["x"] = 3.0
        db.kv_set("_system", cost.LIFETIME_LEDGER_KEY, {"x": 3.0})

        resp = await client.post("/api/cost/reset")

        assert (await resp.json())["ok"] is True
        assert cost.lifetime_cost == {}
        assert db.kv_get("_system", cost.LIFETIME_LEDGER_KEY) is None

    async def test_a_ledger_that_cannot_be_cleared_does_not_fail_the_reset(
        self, client: TestClient, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _refuse(*_args: Any) -> None:
            raise OSError("locked")

        monkeypatch.setattr(db, "kv_delete", _refuse)

        assert (await client.post("/api/cost/reset")).status == 200

    async def test_a_reset_without_a_database_is_a_500_without_details(
        self, client: TestClient
    ) -> None:
        resp = await client.post("/api/cost/reset")

        assert resp.status == 500
        assert await resp.json() == {"error": "Could not reset the cost ledger"}


class TestChatLogAndFeed:
    async def test_without_a_database_both_are_empty(self, client: TestClient) -> None:
        assert await (await client.get("/api/chats")).json() == []
        assert await (await client.get("/api/feed")).json() == []

    async def test_the_chat_log_is_filtered(self, client: TestClient, db: WactorzDB) -> None:
        db.write_chat_log(time.time() - 10, "main", "user", "old")
        db.write_chat_log(time.time(), "main", "assistant", "new")

        rows = await (
            await client.get(
                "/api/chats", params={"role": "assistant", "since": "0", "limit": "5000"}
            )
        ).json()

        assert [r["content"] for r in rows] == ["new"]

    async def test_a_bad_query_is_a_500_without_details(
        self, client: TestClient, db: WactorzDB
    ) -> None:
        resp = await client.get("/api/chats", params={"since": "yesterday"})

        assert resp.status == 500
        assert await resp.json() == {"error": "Could not read the chat log"}

    async def test_the_feed_is_the_chat_log_oldest_first(
        self, client: TestClient, db: WactorzDB
    ) -> None:
        db.write_chat_log(100.0, "main", "user", "first")
        db.write_chat_log(200.0, "main", "assistant", "second")

        items = await (await client.get("/api/feed")).json()

        assert [(i["label"], i["timestamp"]) for i in items] == [
            ("first", 100.0),
            ("second", 200.0),
        ]

    async def test_an_old_database_feeds_from_conversation_history(
        self, client: TestClient, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db.kv_set(
            "main",
            "conversation_history",
            [
                {"role": "user", "content": "hi"},
                {"role": "system", "content": "x"},
                {"role": "assistant", "content": "yo"},
            ],
        )
        db.conn.execute(
            "INSERT INTO kv_store (agent, key, value, updated) VALUES ('bad', 'conversation_history', 'not json', 0)"
        )
        db.conn.commit()

        def _broken(**_kwargs: Any) -> Any:
            raise RuntimeError("no chat_log table")

        monkeypatch.setattr(db, "query_chat_log", _broken)

        items = await (await client.get("/api/feed")).json()

        assert [i["label"] for i in items] == ["hi", "yo"]
        assert items[0]["timestamp"] < items[1]["timestamp"]

    async def test_a_feed_that_fails_entirely_is_empty(
        self, client: TestClient, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db, "query_chat_log", lambda **_kw: [])
        monkeypatch.setattr(db, "_conn", None)

        assert await (await client.get("/api/feed")).json() == []


class TestSendMessage:
    async def test_every_refusal_has_its_status(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (await client.post("/api/actors/a1/message", json={"content": "x"})).status == 503

        monkeypatch.setattr(runtime, "registry", _Registry(_Actor("a1", "weather")))
        assert (await client.post("/api/actors/a1/message", data="nope")).status == 400
        assert (await client.post("/api/actors/a1/message", json={"content": "  "})).status == 400
        assert (await client.post("/api/actors/zz/message", json={"content": "x"})).status == 404

    async def test_a_message_is_routed_to_the_named_actor(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "registry", _Registry(_Actor("a1", "weather")))
        routed: list[str] = []

        async def _route(text: str, reply: Any) -> None:
            routed.append(text)

        monkeypatch.setattr(chat, "route_chat", _route)

        await client.post("/api/actors/a1/message", json={"content": "rain?"})
        await client.post("/api/actors/weather/message", json={"content": "/help"})

        assert routed == ["@weather rain?", "/help"]


class TestDelete:
    @pytest.fixture(autouse=True)
    def _quiet(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        deleted: list[str] = []

        async def _delete(agent_id: str) -> str:
            deleted.append(agent_id)
            return "local"

        async def _broadcast(_msg: dict[str, Any]) -> None:
            return None

        monkeypatch.setattr(lifecycle, "delete_agent", _delete)
        monkeypatch.setattr(ws, "broadcast", _broadcast)
        self.deleted = deleted
        return deleted

    async def test_a_protected_record_or_actor_is_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime.state["agents"]["r1"] = {"name": "remote", "protected": True}
        monkeypatch.setattr(runtime, "registry", _Registry(_Actor("a1", "main", protected=True)))

        assert (await client.delete("/api/actors/r1")).status == 403
        assert (await client.delete("/api/actors/main")).status == 403
        assert (await client.delete("/api/actors/ghost")).status == 404
        assert self.deleted == []

    async def test_without_a_registry_an_unknown_id_is_not_found(self, client: TestClient) -> None:
        assert (await client.delete("/api/actors/ghost")).status == 404

    async def test_an_actor_named_is_deleted_by_its_id(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "registry", _Registry(_Actor("a1", "weather")))

        resp = await client.delete("/api/actors/weather")

        assert await resp.text() == "stopping (local)"
        assert self.deleted == ["a1"]


class TestLifecycle:
    async def test_a_remote_agent_is_reached_by_id_even_when_named(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "registry", _Registry())
        runtime.state["agents"]["r1"] = {"name": "cam"}
        commands: list[tuple[str, str]] = []

        async def _run(agent_id: str, command: str, sender: str) -> str:
            commands.append((agent_id, command))
            return {"start": "mqtt", "stop": ""}[command]

        monkeypatch.setattr(lifecycle, "run_command", _run)

        started = await client.post("/api/actors/cam/start")
        stopped = await client.post("/api/actors/r1/stop")

        assert await started.json() == {"status": "starting"}
        assert stopped.status == 503
        assert commands == [("r1", "start"), ("r1", "stop")]

    async def test_unknown_unavailable_and_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (await client.post("/api/actors/a1/start")).status == 503

        monkeypatch.setattr(runtime, "registry", _Registry(_Actor("a1", "weather")))

        async def _refused(*_args: Any) -> str:
            return "refused"

        monkeypatch.setattr(lifecycle, "run_command", _refused)

        assert (await client.post("/api/actors/ghost/start")).status == 404
        assert (await client.post("/api/actors/a1/start")).status == 409


class TestReads:
    async def test_metrics_come_from_the_actor_or_its_record(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "registry", _Registry(_Actor("a1", "weather")))
        runtime.state["agents"]["r1"] = {"messages_processed": 3, "cpu": 1.5, "cost_usd": 0.2}

        local = await (await client.get("/api/actors/a1/metrics")).json()
        remote = await (await client.get("/api/actors/r1/metrics")).json()

        assert (local["messages_processed"], local["cost_usd"]) == (5, 0.5)
        assert (remote["messages_processed"], remote["cpu"], remote["cost_usd"]) == (3, 1.5, 0.2)
        assert (await client.get("/api/actors/zz/metrics")).status == 404

    async def test_without_a_registry_the_list_comes_from_reported_state(
        self, client: TestClient
    ) -> None:
        runtime.state["agents"]["r1"] = {
            "agent_id": "r1",
            "name": "cam",
            "state": "running",
            "cost_usd": 1.0,
        }

        (listed,) = await (await client.get("/api/actors")).json()
        one = await (await client.get("/api/actors/r1")).json()

        assert listed == one
        assert (listed["name"], listed["costUsd"]) == ("cam", 1.0)
        assert (await client.get("/api/actors/zz")).status == 404

    async def test_a_deleted_actor_is_left_out_of_the_list(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            runtime, "registry", _Registry(_Actor("a1", "weather"), _Actor("a2", "gone"))
        )
        runtime.mark_deleted("a2")

        listed = await (await client.get("/api/actors")).json()

        assert [a["name"] for a in listed] == ["weather"]
        assert listed[0]["messagesProcessed"] == 5

    async def test_history_from_the_actor_the_database_or_nowhere(
        self, client: TestClient, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        actor = _Actor("a1", "weather")
        actor.history = [{"role": "user", "content": "hi"}, {"role": "tool", "content": "x"}]
        monkeypatch.setattr(runtime, "registry", _Registry(actor))
        db.kv_set(
            "old-agent", "conversation_history", [{"role": "assistant", "content": "archived"}]
        )

        live = await (await client.get("/api/actors/weather/history")).json()
        archived = await (await client.get("/api/actors/old-agent/history")).json()
        missing = await (await client.get("/api/actors/never/history")).json()

        assert live == [{"role": "user", "content": "hi"}]
        assert archived == [{"role": "assistant", "content": "archived"}]
        assert missing == []

    async def test_history_survives_a_broken_database_and_no_database(
        self, client: TestClient, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db.conn.execute(
            "INSERT INTO kv_store (agent, key, value, updated) VALUES ('x', 'conversation_history', 'not json', 0)"
        )
        db.conn.commit()

        assert await (await client.get("/api/actors/x/history")).json() == []
        monkeypatch.setattr(runtime, "db", None)
        assert await (await client.get("/api/actors/x/history")).json() == []

    def test_the_card_payload_has_defaults(self) -> None:
        assert api_actors._actor_payload({})["state"] == "unknown"
