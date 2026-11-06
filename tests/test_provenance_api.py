# SPDX-License-Identifier: Apache-2.0
"""The provenance routes: the graph read back as it is.

``GET /api/provenance`` lists nodes by kind, source, actor, status and
event-time window, with their edges when asked; ``GET /api/provenance/{id}``
is one node with its edges and a walk down or up from it. Against a real
database and the real app, with the graph a drift alarm, a retrain and a
promotion leave behind.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.core.persistence import WactorzDB
from wactorz.core.provenance import (
    ACTION,
    DECISION,
    DONE,
    LED_TO,
    MODEL,
    OBSERVATION,
    PRODUCED,
    Provenance,
)
from wactorz.web import runtime
from wactorz.web.app import build_app

T0 = 1_760_000_000.0
HOUR = 3600.0


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    with WactorzDB(tmp_path / "state" / "wactorz.db") as database:
        monkeypatch.setattr(runtime, "db", database)
        yield database


@pytest.fixture(name="client")
async def client_fixture(db: WactorzDB) -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


def a_retrain(db: WactorzDB) -> dict[str, str]:
    """An alarm at 14:02 led to a run that produced a version; a decision promoted it later."""
    graph = Provenance(db)
    alarm = graph.add_node(
        OBSERVATION,
        ts=T0,
        ts_end=T0 + HOUR,
        source="sensors/imu",
        actor="agent:drift",
        payload={"what": "drift", "test": "psi", "column": "ay"},
        recorded=T0 + HOUR,
    )
    run = graph.add_node(
        ACTION,
        ts=T0 + HOUR,
        ts_end=T0 + HOUR + 30,
        status=DONE,
        source="model:imu",
        actor="agent:trainer",
        payload={"what": "train"},
        recorded=T0 + HOUR + 30,
    )
    version = graph.add_node(
        MODEL,
        ts=T0 + HOUR + 30,
        ts_end=T0 + HOUR + 30,
        source="model:imu",
        actor="agent:trainer",
        payload={"name": "imu", "version": 3},
        recorded=T0 + HOUR + 31,
    )
    decision = graph.add_node(
        DECISION,
        ts=T0 + 2 * HOUR,
        ts_end=T0 + 2 * HOUR,
        source="model:imu",
        actor="policy",
        payload={"met": True},
        recorded=T0 + 2 * HOUR,
    )
    graph.add_edge(alarm.id, run.id, LED_TO, recorded=T0 + HOUR)
    graph.add_edge(run.id, version.id, PRODUCED, recorded=T0 + HOUR + 31)
    return {"alarm": alarm.id, "run": run.id, "version": version.id, "decision": decision.id}


class TestListing:
    async def test_every_node_oldest_recorded_first(
        self, client: TestClient, db: WactorzDB
    ) -> None:
        ids = a_retrain(db)

        response = await client.get("/api/provenance")

        assert response.status == 200
        body = await response.json()
        assert [n["id"] for n in body["nodes"]] == [
            ids["alarm"],
            ids["run"],
            ids["version"],
            ids["decision"],
        ]
        assert "edges" not in body

    async def test_by_kind_source_actor_status_and_window(
        self, client: TestClient, db: WactorzDB
    ) -> None:
        ids = a_retrain(db)

        async def listed(**params: str) -> list[str]:
            response = await client.get("/api/provenance", params=params)
            assert response.status == 200
            return [n["id"] for n in (await response.json())["nodes"]]

        assert await listed(kind=ACTION) == [ids["run"]]
        assert await listed(source="model:imu", actor="agent:trainer") == [
            ids["run"],
            ids["version"],
        ]
        assert await listed(status="done") == [ids["run"]]
        assert await listed(since=str(T0 + HOUR + 1), until=str(T0 + HOUR + 60)) == [
            ids["run"],
            ids["version"],
        ]
        assert await listed(until=str(T0 + 1)) == [ids["alarm"]]
        assert await listed(limit="2") == [ids["alarm"], ids["run"]]

    async def test_the_edges_among_the_listed_come_when_asked(
        self, client: TestClient, db: WactorzDB
    ) -> None:
        ids = a_retrain(db)

        response = await client.get("/api/provenance", params={"kind": ACTION, "edges": "1"})

        body = await response.json()
        assert [(e["src"], e["dst"], e["relation"]) for e in body["edges"]] == [
            (ids["alarm"], ids["run"], LED_TO),
            (ids["run"], ids["version"], PRODUCED),
        ]

    @pytest.mark.parametrize(
        ("params", "why"),
        [
            ({"kind": "wish"}, "'kind' is one of"),
            ({"since": "soon"}, "Unix seconds"),
            ({"since": "10", "until": "5"}, "comes after"),
            ({"limit": "0"}, "whole number"),
        ],
    )
    async def test_a_bad_query_is_a_400_saying_what(
        self, client: TestClient, db: WactorzDB, params: dict[str, str], why: str
    ) -> None:
        response = await client.get("/api/provenance", params=params)

        assert response.status == 400
        assert why in (await response.json())["error"]

    async def test_without_persistence_it_is_a_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "db", None)
        monkeypatch.setattr("wactorz.web.api_provenance.get_db", lambda: None)

        listing = await client.get("/api/provenance")
        one = await client.get("/api/provenance/nothing")

        assert (listing.status, one.status) == (503, 503)


class TestOneNode:
    async def test_the_node_and_its_edges(self, client: TestClient, db: WactorzDB) -> None:
        ids = a_retrain(db)

        response = await client.get(f"/api/provenance/{ids['run']}")

        assert response.status == 200
        body = await response.json()
        assert body["node"]["id"] == ids["run"] and body["node"]["payload"] == {"what": "train"}
        assert [(e["src"], e["dst"]) for e in body["edges"]] == [
            (ids["alarm"], ids["run"]),
            (ids["run"], ids["version"]),
        ]
        assert "walk" not in body

    async def test_a_walk_down_is_what_followed_and_up_is_what_led(
        self, client: TestClient, db: WactorzDB
    ) -> None:
        ids = a_retrain(db)

        down = await (
            await client.get(f"/api/provenance/{ids['alarm']}", params={"walk": "down"})
        ).json()
        up = await (
            await client.get(f"/api/provenance/{ids['version']}", params={"walk": "up"})
        ).json()
        near = await (
            await client.get(
                f"/api/provenance/{ids['alarm']}", params={"walk": "down", "depth": "1"}
            )
        ).json()

        assert down["walk"] == "down"
        assert [n["id"] for n in down["nodes"]] == [ids["run"], ids["version"]]
        assert [(e["src"], e["dst"]) for e in down["walk_edges"]] == [
            (ids["alarm"], ids["run"]),
            (ids["run"], ids["version"]),
        ]
        assert [n["id"] for n in up["nodes"]] == [ids["alarm"], ids["run"]]
        assert [n["id"] for n in near["nodes"]] == [ids["run"]]

    async def test_a_node_that_is_not_there_is_a_404_and_a_bad_walk_a_400(
        self, client: TestClient, db: WactorzDB
    ) -> None:
        ids = a_retrain(db)

        missing = await client.get("/api/provenance/nobody")
        sideways = await client.get(f"/api/provenance/{ids['alarm']}", params={"walk": "sideways"})

        assert missing.status == 404
        assert sideways.status == 400
        assert "'walk' is one of" in (await sideways.json())["error"]


class TestEdgesAmong:
    def test_each_edge_once_whichever_end_was_named(self, db: WactorzDB) -> None:
        ids = a_retrain(db)
        graph = Provenance(db)

        among = graph.edges_among([ids["alarm"], ids["run"], ids["run"]])
        none: list[Any] = graph.edges_among([])

        assert [(e.src, e.dst) for e in among] == [
            (ids["alarm"], ids["run"]),
            (ids["run"], ids["version"]),
        ]
        assert none == []

    def test_more_ids_than_one_query_takes_are_read_in_turns(self, db: WactorzDB) -> None:
        graph = Provenance(db)
        first = graph.add_node(OBSERVATION, ts=T0, actor="a", recorded=T0)
        many = [
            graph.add_node(ACTION, ts=T0 + i, actor="a", recorded=T0 + i) for i in range(1, 1001)
        ]
        for action in many:
            graph.add_edge(first.id, action.id, LED_TO, recorded=T0)

        found = graph.edges_among([n.id for n in many])

        assert len(found) == 1000 and {e.src for e in found} == {first.id}
