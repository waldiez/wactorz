"""The label routes, and the dashboard vouching for the answers it sends.

``GET /api/labels`` lists labels by source and event-time window, superseded
ones only when asked; ``GET /api/labels/effective`` resolves one source into
what held when. An answer the dashboard sends is vouched for, so the label it
makes is marked verified. Against a real database and the real app.
"""

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.core import labels as core_labels
from wactorz.core.labels import OPERATOR, Labels
from wactorz.core.persistence import WactorzDB
from wactorz.core.persistence.stores import Stores
from wactorz.core.questions import Question, book_frame, questions_topic
from wactorz.web import events, questions, runtime
from wactorz.web.app import build_app

T0 = 1_760_000_000.0
HOUR = 3600.0
SOURCE = "sensors/ahu-1"


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


def corrected(db: WactorzDB) -> None:
    """Maintenance from 14:02, then a fault from 15:02 said an hour later."""
    labels = Labels(db)
    labels.write(source=SOURCE, start=T0, value="maintenance", actor=OPERATOR, recorded=T0 + 60)
    labels.write(
        source=SOURCE, start=T0 + HOUR, value="fault", actor=OPERATOR, recorded=T0 + 2 * HOUR
    )
    labels.write(source="sensors/ahu-2", start=T0, value="normal", actor="agent:hvac")


class TestListing:
    async def test_labels_are_listed_by_source(self, client: TestClient, db: WactorzDB) -> None:
        corrected(db)

        response = await client.get("/api/labels", params={"source": SOURCE})

        assert response.status == 200
        body = await response.json()
        assert [(lab["value"], lab["status"]) for lab in body["labels"]] == [
            ("maintenance", "partly_superseded"),
            ("fault", "valid"),
        ]

    async def test_a_window_keeps_the_labels_that_touch_it(
        self, client: TestClient, db: WactorzDB
    ) -> None:
        corrected(db)

        response = await client.get(
            "/api/labels", params={"source": SOURCE, "since": str(T0 + 2 * HOUR)}
        )

        body = await response.json()
        assert [lab["value"] for lab in body["labels"]] == ["maintenance", "fault"]

    async def test_superseded_labels_are_listed_only_when_asked(
        self, client: TestClient, db: WactorzDB
    ) -> None:
        labels = Labels(db)
        labels.write(source=SOURCE, start=T0, value="maintenance", actor=OPERATOR, recorded=T0)
        labels.write(source=SOURCE, start=T0, value="fault", actor=OPERATOR, recorded=T0 + 60)

        standing = await (await client.get("/api/labels", params={"source": SOURCE})).json()
        everything = await (
            await client.get("/api/labels", params={"source": SOURCE, "all": "1"})
        ).json()

        assert [lab["value"] for lab in standing["labels"]] == ["fault"]
        assert [lab["value"] for lab in everything["labels"]] == ["maintenance", "fault"]

    @pytest.mark.parametrize(
        "params",
        [
            {"since": "yesterday"},
            {"until": "-5"},
            {"limit": "0"},
            {"since": str(T0 + HOUR), "until": str(T0)},
        ],
    )
    async def test_a_query_that_is_not_one_is_refused(
        self, client: TestClient, params: dict[str, str]
    ) -> None:
        response = await client.get("/api/labels", params=params)

        assert response.status == 400


class TestWhatHeld:
    async def test_one_source_resolves_into_what_held_when(
        self, client: TestClient, db: WactorzDB
    ) -> None:
        corrected(db)

        response = await client.get("/api/labels/effective", params={"source": SOURCE})

        assert response.status == 200
        body = await response.json()
        assert [(i["start"], i["end"], i["value"]) for i in body["intervals"]] == [
            (T0, T0 + HOUR, "maintenance"),
            (T0 + HOUR, None, "fault"),
        ]

    async def test_the_source_is_required(self, client: TestClient) -> None:
        response = await client.get("/api/labels/effective")

        assert response.status == 400


async def test_without_persistence_the_routes_say_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "db", None)
    monkeypatch.setattr(Stores, "db", None)
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    try:
        assert (await client.get("/api/labels")).status == 503
        assert (await client.get("/api/labels/effective", params={"source": SOURCE})).status == 503
    finally:
        await client.close()


class _Sent:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, payload: str, **_kwargs: Any) -> None:
        self.published.append((topic, json.loads(payload)))


class _Refusing:
    async def publish(self, topic: str, payload: str, **_kwargs: Any) -> None:
        raise ConnectionError("broker gone")


async def test_an_answer_that_could_not_be_sent_is_not_vouched_for(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_labels, "_vouched", {})
    monkeypatch.setattr(questions, "_books", {})
    monkeypatch.setattr(runtime, "mqtt_client_ref", _Refusing())
    monkeypatch.setattr(runtime, "registry", None)
    question = Question(
        id="q1",
        text="Changed?",
        options=("Maintenance", "Real problem"),
        channel="telegram",
        asked_at=T0,
    )
    events.parse_topic(
        questions_topic("a1"), json.dumps(book_frame("hvac-watch", {"q1": question}))
    )

    response = await client.post("/api/questions/a1/q1/answer", json={"option": "Maintenance"})

    assert response.status == 503
    assert not core_labels.vouched("a1", "q1", "Maintenance")


async def test_an_answer_the_dashboard_sends_is_vouched_for(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_labels, "_vouched", {})
    monkeypatch.setattr(questions, "_books", {})
    monkeypatch.setattr(runtime, "mqtt_client_ref", _Sent())
    monkeypatch.setattr(runtime, "registry", None)
    question = Question(
        id="q1",
        text="Did something change on AHU-1?",
        options=("Maintenance", "Real problem"),
        channel="telegram",
        asked_at=T0,
        label={"source": SOURCE, "start": T0, "values": {"Maintenance": "maintenance"}},
    )
    events.parse_topic(
        questions_topic("a1"), json.dumps(book_frame("hvac-watch", {"q1": question}))
    )

    response = await client.post("/api/questions/a1/q1/answer", json={"option": "Maintenance"})

    assert response.status == 200
    assert core_labels.vouched("a1", "q1", "Maintenance")
    assert not core_labels.vouched("a1", "q1", "Real problem")
