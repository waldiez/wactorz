"""The server's ledger of questions, built from the books agents publish retained.

Fed through ``parse_topic`` as the broker delivers each book, so an agent here
and one on a node read the same; an agent's book leaves with the agent.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.core import outbound
from wactorz.core.questions import (
    ANSWERED_KEPT,
    QUESTIONS_LEAF,
    Question,
    book_frame,
    questions_topic,
)
from wactorz.core.topics import RETAINED_AGENT_LEAVES
from wactorz.web import answers, events, lifecycle, questions, relay, runtime
from wactorz.web.app import build_app


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime,
        "state",
        {"agents": {}, "nodes": {}, "alerts": [], "system_health": {}, "log_feed": []},
    )
    monkeypatch.setattr(runtime, "deleted_agent_ids", [])
    monkeypatch.setattr(questions, "_books", {})


def _question(qid: str, *, asked_at: float, answered_at: float = 0.0) -> Question:
    return Question(
        id=qid,
        text=f"question {qid}?",
        options=("yes", "no"),
        channel="dashboard",
        asked_at=asked_at,
        answer="yes" if answered_at else None,
        answer_index=0 if answered_at else None,
        answer_channel="telegram" if answered_at else "",
        answered_at=answered_at,
    )


def _publish(agent_id: str, agent: str, *asked: Question) -> None:
    """Deliver ``agent_id``'s book as the broker would."""
    book = {q.id: q for q in asked}
    events.parse_topic(questions_topic(agent_id), json.dumps(book_frame(agent, book)))


def _ids(kind: str) -> list[str]:
    return [q["id"] for q in questions.listing()[kind]]


def test_each_question_is_listed_with_the_agent_that_asked_it() -> None:
    _publish("a1", "hvac-watch", _question("q1", asked_at=5.0))

    (listed,) = questions.listing()["open"]

    assert (listed["id"], listed["agent_id"], listed["agent"]) == ("q1", "a1", "hvac-watch")
    assert listed["options"] == ["yes", "no"]


def test_open_questions_come_oldest_first_and_answered_ones_newest_first() -> None:
    _publish("a1", "one", _question("q1", asked_at=3.0), _question("q2", asked_at=1.0))
    _publish(
        "a2",
        "two",
        _question("q3", asked_at=2.0),
        _question("q4", asked_at=0.5, answered_at=8.0),
        _question("q5", asked_at=0.1, answered_at=9.0),
    )

    assert _ids("open") == ["q2", "q3", "q1"]
    assert _ids("answered") == ["q5", "q4"]


def test_a_newer_book_replaces_the_older_one() -> None:
    _publish("a1", "one", _question("q1", asked_at=1.0))
    _publish("a1", "one", _question("q1", asked_at=1.0, answered_at=2.0))

    assert (_ids("open"), _ids("answered")) == ([], ["q1"])


def test_a_cleared_book_is_forgotten_even_for_an_agent_marked_deleted() -> None:
    _publish("a1", "one", _question("q1", asked_at=1.0))
    runtime.mark_deleted("a1")

    events.parse_topic(questions_topic("a1"), "")

    assert "a1" not in questions._books


def test_a_withdrawn_manifest_takes_the_agents_book_with_it() -> None:
    _publish("a1", "one", _question("q1", asked_at=1.0))

    events.parse_topic("agents/a1/manifest", "")

    assert questions.listing() == {"open": [], "answered": []}


def test_a_deleted_agents_book_is_left_out_before_it_is_cleared() -> None:
    _publish("a1", "one", _question("q1", asked_at=1.0))
    _publish("a2", "two", _question("q2", asked_at=1.0))
    runtime.mark_deleted("a1")

    assert _ids("open") == ["q2"]


@pytest.mark.parametrize("payload", ["not json", "[1, 2]", '"text"'])
def test_a_frame_that_is_not_a_book_leaves_the_agent_with_none(payload: str) -> None:
    _publish("a1", "one", _question("q1", asked_at=1.0))

    events.parse_topic(questions_topic("a1"), payload)

    assert _ids("open") == []


def test_records_that_are_not_questions_are_left_out_and_a_nameless_agent_is_its_id() -> None:
    frame: dict[str, Any] = {"questions": [{"no": "id"}, _question("q1", asked_at=1.0).to_dict()]}

    events.parse_topic(questions_topic("a" * 32), json.dumps(frame))

    (listed,) = questions.listing()["open"]
    assert (listed["id"], listed["agent"]) == ("q1", "a" * 8)


def test_answered_questions_are_as_many_as_one_book_keeps() -> None:
    for agent in ("a1", "a2"):
        _publish(
            agent,
            agent,
            *(
                _question(f"{agent}-{n}", asked_at=0.0, answered_at=float(n))
                for n in range(1, ANSWERED_KEPT + 1)
            ),
        )

    answered = questions.listing()["answered"]

    assert len(answered) == ANSWERED_KEPT
    assert answered[0]["answered_at"] == ANSWERED_KEPT


@pytest.fixture(name="client")
async def client_fixture() -> AsyncIterator[TestClient]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


async def test_the_route_answers_with_the_ledger(client: TestClient) -> None:
    _publish("a1", "one", _question("q1", asked_at=1.0))

    response = await client.get("/api/questions")

    assert response.status == 200
    body = await response.json()
    assert [q["id"] for q in body["open"]] == ["q1"]
    assert body["answered"] == []


@pytest.mark.parametrize(
    ("topic", "relayed"),
    [
        ("agents/a1/questions", True),
        ("models/imu/active", True),
        ("models/imu/shadow", True),
        ("models/imu/canary", False),
        ("models/imu", False),
    ],
)
def test_books_and_pointers_reach_the_browsers(topic: str, relayed: bool) -> None:
    assert relay.is_relayed(topic) is relayed


class _Broker:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    async def publish(self, topic: str, payload: Any, **kwargs: Any) -> None:
        if payload == b"" and kwargs.get("retain"):
            self.cleared.append(topic)


async def test_deleting_an_agent_clears_its_book(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _Broker()
    monkeypatch.setattr(runtime, "mqtt_client_ref", broker)

    await lifecycle.purge_agent_retained("a1")

    assert questions_topic("a1") in broker.cleared


def test_the_book_is_among_the_topics_every_delete_clears() -> None:
    """Main, the server and a node all clear `RETAINED_AGENT_LEAVES`."""
    assert QUESTIONS_LEAF in RETAINED_AGENT_LEAVES


class _Sent:
    """A broker that keeps what is published."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, payload: str, **_kwargs: Any) -> None:
        self.published.append((topic, json.loads(payload)))


@pytest.fixture(name="sent")
def sent_fixture(monkeypatch: pytest.MonkeyPatch) -> _Sent:
    sent = _Sent()
    monkeypatch.setattr(runtime, "mqtt_client_ref", sent)
    monkeypatch.setattr(runtime, "registry", None)
    return sent


class TestAnswering:
    async def test_a_pick_goes_to_the_agent_with_the_options_index(
        self, client: TestClient, sent: _Sent
    ) -> None:
        _publish("a1", "one", _question("q1", asked_at=1.0))

        response = await client.post("/api/questions/a1/q1/answer", json={"option": "no"})

        assert response.status == 200
        ((topic, payload),) = sent.published
        assert topic == "agents/a1/answer"
        assert (payload["id"], payload["option"], payload["index"], payload["channel"]) == (
            "q1",
            "no",
            1,
            "dashboard",
        )

    @pytest.mark.parametrize(
        ("path", "body", "status"),
        [
            ("/api/questions/a1/nope/answer", {"option": "yes"}, 404),
            ("/api/questions/a2/q1/answer", {"option": "yes"}, 404),
            ("/api/questions/a1/q2/answer", {"option": "yes"}, 409),
            ("/api/questions/a1/q1/answer", {"option": "maybe"}, 400),
            ("/api/questions/a1/q1/answer", {}, 400),
            ("/api/questions/a1/q1/answer", ["yes"], 400),
        ],
    )
    async def test_an_answer_the_ledger_will_not_send_is_refused(
        self, client: TestClient, sent: _Sent, path: str, body: object, status: int
    ) -> None:
        _publish(
            "a1",
            "one",
            _question("q1", asked_at=1.0),
            _question("q2", asked_at=1.0, answered_at=2.0),
        )

        response = await client.post(path, json=body)

        assert response.status == status
        assert (await response.json())["error"]
        assert sent.published == []

    async def test_a_deleted_agents_question_is_not_answered(
        self, client: TestClient, sent: _Sent
    ) -> None:
        _publish("a1", "one", _question("q1", asked_at=1.0))
        runtime.mark_deleted("a1")

        response = await client.post("/api/questions/a1/q1/answer", json={"option": "yes"})

        assert response.status == 404

    async def test_without_a_broker_the_answer_is_not_lost_silently(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "mqtt_client_ref", None)
        monkeypatch.setattr(runtime, "registry", None)
        _publish("a1", "one", _question("q1", asked_at=1.0))

        response = await client.post("/api/questions/a1/q1/answer", json={"option": "yes"})

        assert response.status == 503

    async def test_the_button_is_checked_against_a_question_the_ledger_knows(
        self, sent: _Sent
    ) -> None:
        _publish("a1", "one", _question("q1", asked_at=1.0))

        problem = await answers.answer_from_dashboard(
            {"agent_id": "a1", "id": "q1", "option": "maybe"}
        )

        assert problem is not None
        assert "is not one of" in problem
        assert sent.published == []

    async def test_the_button_for_a_question_the_ledger_does_not_know_goes_out(
        self, sent: _Sent
    ) -> None:
        """A message sent with `notify_user` alone keeps no book: the agent judges the pick."""
        problem = await answers.answer_from_dashboard(
            {"agent_id": "a1", "id": "m1", "option": "maybe", "index": 2}
        )

        assert problem is None
        ((_, payload),) = sent.published
        assert (payload["option"], payload["index"]) == ("maybe", 2)


class TestTheNote:
    """What a person adds to a pick, in their own words, where the channel lets them."""

    async def test_the_route_sends_the_note_with_the_pick(
        self, client: TestClient, sent: _Sent
    ) -> None:
        _publish("a1", "one", _question("q1", asked_at=1.0))

        response = await client.post(
            "/api/questions/a1/q1/answer", json={"option": "yes", "note": "  valve swapped  "}
        )

        assert response.status == 200
        ((_, payload),) = sent.published
        assert payload["note"] == "valve swapped"

    async def test_a_note_that_is_not_text_is_refused(
        self, client: TestClient, sent: _Sent
    ) -> None:
        _publish("a1", "one", _question("q1", asked_at=1.0))

        response = await client.post(
            "/api/questions/a1/q1/answer", json={"option": "yes", "note": ["x"]}
        )

        assert response.status == 400
        assert sent.published == []

    def test_a_long_note_is_cut_and_no_note_leaves_the_frame_as_it_was(self) -> None:
        long = outbound.Answer.from_payload("a1", {"id": "q1", "option": "yes", "note": "x" * 900})
        plain = outbound.Answer.from_payload("a1", {"id": "q1", "option": "yes", "note": 7})

        assert long is not None
        assert len(long.note) == outbound.MAX_NOTE_CHARS
        assert plain is not None
        assert "note" not in plain.to_payload()

    def test_the_feed_shows_the_note(self) -> None:
        events.parse_topic(
            "agents/a1/answer",
            json.dumps({"id": "q1", "option": "yes", "channel": "dashboard", "note": "swapped"}),
        )

        assert runtime.state["log_feed"][0]["note"] == "swapped"
