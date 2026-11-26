# SPDX-License-Identifier: Apache-2.0
"""The server's ledger of questions, built from the books agents publish retained.

Fed through ``parse_topic`` as the broker delivers each book, so an agent here
and one on a node read the same; an agent's book leaves with the agent.
"""

import dataclasses
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wactorz.core import outbound
from wactorz.core.actor import Actor, Message
from wactorz.core.questions import (
    ANSWERED_KEPT,
    CUT_SHORT,
    QUESTIONS_LEAF,
    Question,
    book_frame,
    questions_topic,
)
from wactorz.core.topics import RETAINED_AGENT_LEAVES, agent_topic
from wactorz.web import answers, events, lifecycle, questions, relay, runtime
from wactorz.web import mqtt as web_mqtt
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
    monkeypatch.setattr(questions, "_offered", questions.OrderedDict())


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
def test_a_frame_that_is_not_a_book_leaves_the_last_book_standing(payload: str) -> None:
    _publish("a1", "one", _question("q1", asked_at=1.0))

    events.parse_topic(questions_topic("a1"), payload)

    assert _ids("open") == ["q1"]


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
async def client_fixture() -> AsyncIterator[TestClient[web.Request, web.Application]]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


async def test_the_route_answers_with_the_ledger(
    client: TestClient[web.Request, web.Application],
) -> None:
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
        self, client: TestClient[web.Request, web.Application], sent: _Sent
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
        self,
        client: TestClient[web.Request, web.Application],
        sent: _Sent,
        path: str,
        body: object,
        status: int,
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
        self, client: TestClient[web.Request, web.Application], sent: _Sent
    ) -> None:
        _publish("a1", "one", _question("q1", asked_at=1.0))
        runtime.mark_deleted("a1")

        response = await client.post("/api/questions/a1/q1/answer", json={"option": "yes"})

        assert response.status == 404

    async def test_without_a_broker_the_answer_is_not_lost_silently(
        self, client: TestClient[web.Request, web.Application], monkeypatch: pytest.MonkeyPatch
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
        self, client: TestClient[web.Request, web.Application], sent: _Sent
    ) -> None:
        _publish("a1", "one", _question("q1", asked_at=1.0))

        response = await client.post(
            "/api/questions/a1/q1/answer", json={"option": "yes", "note": "  valve swapped  "}
        )

        assert response.status == 200
        ((_, payload),) = sent.published
        assert payload["note"] == "valve swapped"

    async def test_a_note_that_is_not_text_is_refused(
        self, client: TestClient[web.Request, web.Application], sent: _Sent
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
        _publish("a1", "hvac-watch", _question("q1", asked_at=1.0))
        events.parse_topic(
            "agents/a1/answer",
            json.dumps({"id": "q1", "option": "yes", "channel": "dashboard", "note": "swapped"}),
        )

        assert runtime.state["log_feed"][0]["note"] == "swapped"


class TestAnswersFromTheBroker:
    """An answer frame anyone on the broker may send, as the agent judges it.

    The agent takes only a pick of its own options, so a frame naming anything
    else is shown to nobody: not on the feed, and not to the browsers, which
    close a question's card on any answer they are shown.
    """

    @staticmethod
    def _frame(option: str, qid: str = "q1") -> str:
        return json.dumps({"id": qid, "option": option, "index": 0, "channel": "telegram"})

    @staticmethod
    def _answers_on_the_feed() -> list[str]:
        return [e["option"] for e in runtime.state["log_feed"] if e.get("type") == "answer"]

    @pytest.fixture(autouse=True)
    def _asked(self) -> None:
        _publish("a1", "hvac-watch", _question("q1", asked_at=1.0))

    @pytest.mark.parametrize(
        ("option", "taken"),
        [("yes", True), ("maybe", False), ("x" * 10_000, False), ("YES", False)],
    )
    def test_only_a_pick_it_offered_reaches_the_feed(self, option: str, taken: bool) -> None:
        events.parse_topic(outbound.answer_topic("a1"), self._frame(option))

        assert self._answers_on_the_feed() == ([option] if taken else [])
        assert events.refused_answer(outbound.answer_topic("a1"), self._frame(option)) is not taken

    def test_the_pick_already_recorded_still_reaches_the_feed(self) -> None:
        # The book saying so may arrive before the frame that answered it.
        _publish("a1", "hvac-watch", _question("q1", asked_at=1.0, answered_at=2.0))

        assert not events.refused_answer(outbound.answer_topic("a1"), self._frame("yes"))
        assert events.refused_answer(outbound.answer_topic("a1"), self._frame("no"))

    @staticmethod
    def _ask_in_chat(qid: str, options: list[str]) -> None:
        """An agent's question as its chat message carries it, with no book: `notify_user`'s."""
        frame = {"id": qid, "content": "Which one?", "options": options, "channel": "dashboard"}
        events.parse_topic(agent_topic("a1", "chat"), json.dumps(frame))

    def test_a_question_with_no_book_is_checked_against_its_chat_message(self) -> None:
        self._ask_in_chat("n1", ["left", "right"])

        assert not events.refused_answer(outbound.answer_topic("a1"), self._frame("left", "n1"))
        assert events.refused_answer(outbound.answer_topic("a1"), self._frame("up", "n1"))

    def test_an_answer_before_its_book_arrives_is_checked_all_the_same(self) -> None:
        # `ask_operator` sends the question before it publishes the book with it.
        self._ask_in_chat("q2", ["yes", "no"])

        events.parse_topic(outbound.answer_topic("a1"), self._frame("maybe", "q2"))
        events.parse_topic(outbound.answer_topic("a1"), self._frame("no", "q2"))

        assert self._answers_on_the_feed() == ["no"]

    def test_a_question_this_server_never_saw_asked_is_shown_to_nobody(self) -> None:
        assert events.refused_answer(outbound.answer_topic("a1"), self._frame("anything", "q9"))

    def test_what_is_kept_of_questions_seen_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(questions, "OFFERS_KEPT", 2)
        for qid in ("n1", "n2", "n3"):
            self._ask_in_chat(qid, ["ok"])

        assert list(questions._offered) == [("a1", "n2"), ("a1", "n3")]

    @staticmethod
    def _closing(reason: str, qid: str = "p1") -> str:
        """What `close_permission` publishes: the reason as the option, at `CLOSED_INDEX`."""
        return json.dumps(
            {"id": qid, "option": reason, "index": outbound.CLOSED_INDEX, "channel": "installer"}
        )

    @staticmethod
    def _permission(**answer: Any) -> Question:
        return dataclasses.replace(_question("p1", asked_at=1.0), permission=True, **answer)

    @pytest.mark.parametrize("reason", [CUT_SHORT, "No answer in time", "Allowed"])
    def test_a_permissions_closing_reaches_the_browsers(self, reason: str) -> None:
        # `close_permission` records the closing in the book, then publishes it.
        closed = self._permission(
            answer=reason, answer_index=outbound.CLOSED_INDEX, answered_at=2.0
        )
        _publish("a1", "installer", closed)

        assert not events.refused_answer(outbound.answer_topic("a1"), self._closing(reason))

    @pytest.mark.parametrize("reason", ["Allowed", "anything"])
    def test_a_closing_the_book_has_not_recorded_is_refused(self, reason: str) -> None:
        # Anything on the broker could otherwise put "Allowed" on the feed and
        # close every dashboard's buttons on a permission still waiting.
        _publish("a1", "installer", self._permission())

        assert events.refused_answer(outbound.answer_topic("a1"), self._closing(reason))

    def test_a_closing_other_than_the_one_recorded_is_refused(self) -> None:
        closed = self._permission(
            answer=CUT_SHORT, answer_index=outbound.CLOSED_INDEX, answered_at=2.0
        )
        _publish("a1", "installer", closed)

        assert events.refused_answer(outbound.answer_topic("a1"), self._closing("Allowed"))

    def test_a_pick_recorded_is_not_taken_for_a_closing(self) -> None:
        # Answered "yes" through the dashboard: a closing naming "yes" is not that answer.
        picked = self._permission(answer="yes", answer_index=0, answered_at=2.0)
        _publish("a1", "installer", picked)

        assert events.refused_answer(outbound.answer_topic("a1"), self._closing("yes"))

    def test_a_pick_of_no_option_for_a_permission_is_still_refused(self) -> None:
        _publish("a1", "installer", self._permission())

        assert events.refused_answer(outbound.answer_topic("a1"), self._frame("Allowed", "p1"))

    def test_an_ordinary_question_is_not_closed_that_way(self) -> None:
        closing = json.dumps({"id": "q1", "option": "Allowed", "index": outbound.CLOSED_INDEX})

        assert events.refused_answer(outbound.answer_topic("a1"), closing)

    def test_an_option_longer_than_any_answers_nothing(self) -> None:
        frame = self._frame("x" * 10_000, "q9")
        events.parse_topic(outbound.answer_topic("a1"), frame)

        assert self._answers_on_the_feed() == []
        assert events.refused_answer(outbound.answer_topic("a1"), frame)

    @pytest.mark.parametrize("payload", ["not json", "[]", json.dumps({"id": "q1"})])
    def test_a_frame_that_is_no_answer_is_refused(self, payload: str) -> None:
        assert events.refused_answer(outbound.answer_topic("a1"), payload)

    def test_other_topics_are_not_judged(self) -> None:
        assert not events.refused_answer(questions_topic("a1"), "not json")

    async def test_a_refused_one_is_not_shown_to_the_browsers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shown: list[dict[str, Any]] = []

        async def broadcast(frame: dict[str, Any]) -> None:
            shown.append(frame)

        monkeypatch.setattr(runtime, "hard_resetting", False)
        monkeypatch.setattr(web_mqtt.ws, "broadcast", broadcast)

        await web_mqtt._dispatch(outbound.answer_topic("a1"), self._frame("maybe"))
        await web_mqtt._dispatch(outbound.answer_topic("a1"), self._frame("yes"))

        # The raw frame is what a dashboard closes a card on; a state patch is not.
        relayed = [f["payload"]["option"] for f in shown if f.get("type") == "server_event"]
        assert relayed == ["yes"]


class _Closer(Actor):
    """An actor whose frames are kept in the order it publishes them."""

    published: list[tuple[str, Any]]

    async def handle_message(self, msg: Message) -> None:
        return None


async def test_a_real_closing_is_shown_from_the_frames_in_the_order_they_are_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = _Closer(name="installer", persistence_dir=str(tmp_path))
    actor.published = []

    async def publish(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        actor.published.append((topic, payload))

    monkeypatch.setattr(actor, "_mqtt_publish", publish)
    question_id = await actor.ask_permission("Install acme?", ["Install", "Refuse"])
    await actor.close_permission(question_id, CUT_SHORT)

    shown: list[str] = []
    for topic, payload in actor.published:
        frame = json.dumps(payload)
        if not events.refused_answer(topic, frame):
            shown.append(topic)
        events.parse_topic(topic, frame)

    assert outbound.answer_topic(actor.actor_id) in shown, "the book is published first"
    assert [e["option"] for e in runtime.state["log_feed"] if e.get("type") == "answer"] == [
        CUT_SHORT
    ]
