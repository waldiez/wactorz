# SPDX-License-Identifier: Apache-2.0
"""An agent asks the operator something and gets the pick back.

``ask_operator`` sends the question as ``notify_user`` does and waits for the
answer frame on the agent's own topic. The question is kept in the agent's
state until it is answered, so a restart in between loses nothing, and a pick
that arrives while nothing waits for it reaches ``on_operator_answer``. A
question sent to a chat channel that nobody answers in time is shown again on
the dashboard before the wait is given up.

No broker: the frames the agent publishes are recorded, and the answer is
delivered by calling the subscription's callback the way the hub would.
"""

import asyncio
import dataclasses
import json
import logging
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

from tests.waiting import PATIENCE_S, until
from wactorz.agents.dynamic import DynamicAgent
from wactorz.agents.function_agent import agent, spec_of
from wactorz.core.actor import Actor, NoAnswerError
from wactorz.core.outbound import Answer, answer_topic
from wactorz.core.questions import (
    ANSWERED_KEPT,
    CUT_SHORT,
    MAX_CONTEXT_CHARS,
    MAX_ID_CHARS,
    QUESTIONS_KEY,
    Question,
    QuestionIdError,
    answered,
    book_of,
    questions_in,
    questions_in_frame,
    questions_topic,
)


@agent(name="hvac-watch")
def hvac_watch(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


class Wired:
    """An actor with its broker link replaced: frames out are kept, answers in are delivered by hand."""

    def __init__(self, actor: Actor, monkeypatch: pytest.MonkeyPatch) -> None:
        self.actor = actor
        self.frames: list[tuple[str, dict[str, Any]]] = []
        #: What was published retained, by topic: the last frame each one holds.
        self.retained: dict[str, Any] = {}
        self.subscribed: list[str] = []
        self._callbacks: dict[str, Any] = {}

        async def _publish(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
            self.frames.append((topic, payload))
            if retain:
                self.retained[topic] = payload

        def _subscribe(topic: str, callback: Any, *, concurrency: int = 1) -> None:
            self.subscribed.append(topic)
            self._callbacks[topic] = callback

        monkeypatch.setattr(actor, "_mqtt_publish", _publish)
        monkeypatch.setattr(actor, "subscribe", _subscribe)

    @property
    def chat_frames(self) -> list[dict[str, Any]]:
        return [p for t, p in self.frames if t.endswith("/chat")]

    @property
    def book(self) -> list[Question]:
        """The questions in the book the actor last published, retained."""
        return questions_in_frame(self.retained.get(questions_topic(self.actor.actor_id)))

    async def answer(
        self, option: str, index: int = 0, channel: str = "telegram", **over: Any
    ) -> None:
        """Deliver the operator's pick for the latest question, as the broker would."""
        payload = {
            "id": over.pop("id", None) or self.chat_frames[-1]["id"],
            "option": option,
            "index": index,
            "channel": channel,
            "user": "42",
            "timestamp": time.time(),
        }
        payload.update(over)
        await self._callbacks[answer_topic(self.actor.actor_id)](payload)

    async def settle(self) -> None:
        """Let the asking task get as far as publishing the question."""
        for _ in range(50):
            if self.chat_frames:
                return
            await asyncio.sleep(0)


@pytest.fixture(name="wired")
def wired_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wired:
    spec = spec_of(hvac_watch)
    assert spec is not None
    return Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)


class TestAskingAndAnswering:
    async def test_the_pick_comes_back(self, wired: Wired) -> None:
        asking = asyncio.create_task(
            wired.actor.ask_operator(
                "Changed?", options=["maintenance", "real problem"], channel="telegram", user="42"
            )
        )
        await wired.settle()
        (frame,) = wired.chat_frames
        assert frame["options"] == ["maintenance", "real problem"]
        assert frame["channel"] == "telegram"
        assert wired.subscribed == [answer_topic(wired.actor.actor_id)]

        await wired.answer("real problem", index=1)

        assert await asking == "real problem"
        (question,) = wired.actor.questions().values()
        assert (question.answer, question.answer_index, question.answer_channel) == (
            "real problem",
            1,
            "telegram",
        )
        assert wired.actor.open_questions() == []

    async def test_no_options_is_no_question(self, wired: Wired) -> None:
        with pytest.raises(ValueError, match="options"):
            await wired.actor.ask_operator("Changed?", options=[])
        assert wired.chat_frames == []

    async def test_the_first_answer_wins(self, wired: Wired) -> None:
        asking = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()
        await wired.answer("yes", index=0, channel="dashboard")
        await wired.answer("no", index=1, channel="telegram")
        assert await asking == "yes"
        (question,) = wired.actor.questions().values()
        assert question.answer == "yes"

    async def test_frames_that_answer_nothing_asked_are_ignored(
        self, wired: Wired, caplog: pytest.LogCaptureFixture
    ) -> None:
        asking = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()
        with caplog.at_level(logging.DEBUG):
            await wired.answer("yes", id="someone-elses")
            await wired._callbacks[answer_topic(wired.actor.actor_id)]("not even a dict")
        assert not asking.done()
        await wired.answer("no", index=1)
        assert await asking == "no"

    @pytest.mark.parametrize(
        "pick",
        [
            pytest.param("maybe", id="not an option"),
            pytest.param("x" * 10_000, id="far longer than an option may be"),
            pytest.param("YES", id="an option in another case"),
        ],
    )
    async def test_a_pick_it_did_not_offer_answers_nothing(
        self, wired: Wired, caplog: pytest.LogCaptureFixture, pick: str
    ) -> None:
        # Whatever may write the answer topic may send anything: a node's account
        # may, and on a broker with no node deployed every client may.
        asking = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()

        with caplog.at_level(logging.DEBUG):
            await wired.answer(pick, index=0)

        assert not asking.done()
        (question,) = wired.actor.questions().values()
        assert question.open, "the question waits for a pick it offered"
        await wired.answer("no", index=1)
        assert await asking == "no"

    async def test_the_index_is_the_options_own_not_the_frames(self, wired: Wired) -> None:
        asking = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()

        await wired.answer("no", index=7)

        assert await asking == "no"
        (question,) = wired.actor.questions().values()
        assert question.answer_index == 1

    async def test_what_a_frame_names_is_bounded(self, wired: Wired) -> None:
        asking = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()

        await wired.answer("yes", channel="c" * 10_000, user="u" * 10_000, note="n" * 10_000)

        assert await asking == "yes"
        (question,) = wired.actor.questions().values()
        assert len(question.answer_channel) <= 128
        assert len(question.answer_note) <= 500

    async def test_a_later_pick_it_did_not_offer_does_not_reach_on_operator_answer(
        self, wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing waits once the wait has timed out; a pick then goes to the hook.
        heard: list[str] = []

        async def on_operator_answer(question: Question, option: str) -> None:
            heard.append(option)

        monkeypatch.setattr(wired.actor, "on_operator_answer", on_operator_answer)
        with pytest.raises(NoAnswerError):
            await wired.actor.ask_operator("Changed?", options=["yes", "no"], timeout=0.01)

        await wired.answer("anything at all")
        await wired.answer("yes")

        assert heard == ["yes"]

    async def test_listening_starts_once(self, wired: Wired) -> None:
        first = asyncio.create_task(wired.actor.ask_operator("One?", options=["a", "b"]))
        await wired.settle()
        await wired.answer("a")
        await first
        second = asyncio.create_task(wired.actor.ask_operator("Two?", options=["a", "b"]))
        await until(lambda: len(wired.chat_frames) == 2, what="the second question sent")
        await wired.answer("b", index=1)
        await second
        assert wired.subscribed == [answer_topic(wired.actor.actor_id)]


class TestWaitingAgain:
    async def test_an_answered_question_is_returned_at_once(self, wired: Wired) -> None:
        asking = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()
        await wired.answer("yes")
        await asking
        question_id = wired.chat_frames[-1]["id"]
        assert await wired.actor.wait_for_answer(question_id, timeout=0.01) == "yes"

    async def test_a_question_never_asked_here_is_an_error(self, wired: Wired) -> None:
        with pytest.raises(LookupError):
            await wired.actor.wait_for_answer("nope")

    async def test_two_waiters_get_the_same_answer(self, wired: Wired) -> None:
        asking = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()
        question_id = wired.chat_frames[-1]["id"]
        waiting = asyncio.create_task(wired.actor.wait_for_answer(question_id))
        await asyncio.sleep(0)
        await wired.answer("no", index=1)
        assert await asking == "no"
        assert await waiting == "no"


class TestWhenNobodyAnswers:
    async def test_a_chat_channel_question_is_shown_again_on_the_dashboard(
        self, wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the chat channel's wait gives up, and at once: the dashboard's then
        # waits for as long as the answer takes, so a slow runner cannot make it late.
        real_wait = wired.actor.wait_for_answer
        waits: list[float | None] = []

        async def _wait(question_id: str, *, timeout: float | None = None) -> str:
            waits.append(timeout)
            if len(waits) == 1:
                raise TimeoutError
            return await real_wait(question_id, timeout=None)

        monkeypatch.setattr(wired.actor, "wait_for_answer", _wait)
        asking = asyncio.create_task(
            wired.actor.ask_operator(
                "Changed?", options=["yes", "no"], channel="telegram", user="42", timeout=60
            )
        )
        await until(lambda: len(wired.chat_frames) == 2, "the question shown again")
        first, again = wired.chat_frames
        assert again["id"] == first["id"]
        assert again["content"].startswith("Still waiting for an answer (asked on telegram)")
        assert again["options"] == ["yes", "no"]
        assert "channel" not in again
        (question,) = wired.actor.questions().values()
        assert question.fallen_back

        await wired.answer("yes", channel="dashboard")
        assert await asyncio.wait_for(asking, timeout=PATIENCE_S) == "yes"
        # Both waits were for the time the caller gave.
        assert waits == [60, 60]

    async def test_then_the_wait_is_given_up_and_the_question_stays_open(
        self, wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        heard: list[tuple[str, str]] = []

        async def _on_operator_answer(question: Question, option: str) -> None:
            heard.append((question.text, option))

        monkeypatch.setattr(wired.actor, "on_operator_answer", _on_operator_answer)

        with pytest.raises(asyncio.TimeoutError, match="Changed"):
            await wired.actor.ask_operator(
                "Changed?", options=["yes", "no"], channel="telegram", user="42", timeout=0.02
            )

        assert len(wired.chat_frames) == 2
        (question,) = wired.actor.open_questions()
        assert question.text == "Changed?"

        # A late pick is not lost: it goes to the hook, since nothing waits.
        await wired.answer("no", index=1, channel="dashboard")
        assert heard == [("Changed?", "no")]
        assert wired.actor.open_questions() == []

    async def test_a_dashboard_question_is_not_shown_twice(self, wired: Wired) -> None:
        with pytest.raises(asyncio.TimeoutError):
            await wired.actor.ask_operator("Changed?", options=["yes", "no"], timeout=0.02)
        assert len(wired.chat_frames) == 1

    async def test_without_a_timeout_there_is_no_giving_up(self, wired: Wired) -> None:
        asking = asyncio.create_task(
            wired.actor.ask_operator(
                "Changed?", options=["yes", "no"], channel="telegram", user="42"
            )
        )
        await wired.settle()
        await asyncio.sleep(0.05)
        assert not asking.done()
        assert len(wired.chat_frames) == 1
        await wired.answer("yes")
        assert await asking == "yes"


class TestAcrossARestart:
    async def test_the_question_is_still_open_and_the_answer_still_arrives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = spec_of(hvac_watch)
        assert spec is not None
        before = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        asking = asyncio.create_task(
            before.actor.ask_operator(
                "Changed?", options=["yes", "no"], channel="telegram", user="42"
            )
        )
        await before.settle()
        question_id = before.chat_frames[-1]["id"]
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking

        # A new instance, as the supervisor builds one, over the same state.
        after = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        heard: list[str] = []

        async def _on_operator_answer(question: Question, option: str) -> None:
            heard.append(f"{question.id}:{option}")

        monkeypatch.setattr(after.actor, "on_operator_answer", _on_operator_answer)

        try:
            await after.actor.start()
            # Starting reads the state back, and is what makes it listen
            # again, with nothing waiting.
            (still_open,) = after.actor.open_questions()
            assert still_open.id == question_id
            assert after.subscribed == [answer_topic(after.actor.actor_id)]
            await after.answer("yes", id=question_id)
        finally:
            await after.actor.stop()

        assert heard == [f"{question_id}:yes"]
        assert after.actor.open_questions() == []

    async def test_an_agent_with_nothing_open_does_not_listen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = spec_of(hvac_watch)
        assert spec is not None
        wired = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        try:
            await wired.actor.start()
        finally:
            await wired.actor.stop()
        assert wired.subscribed == []


class TestTheNote:
    async def test_a_note_given_with_the_pick_is_kept_with_the_answer(self, wired: Wired) -> None:
        asking = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()

        await wired.answer("yes", note="valve swapped at 14:02")
        await asking

        (question,) = wired.actor.questions().values()
        assert question.answer_note == "valve swapped at 14:02"
        (published,) = wired.book
        assert published.answer_note == "valve swapped at 14:02"


class TestTheContext:
    async def test_it_is_kept_in_the_book_and_not_sent_to_the_channels(self, wired: Wired) -> None:
        asking = asyncio.create_task(
            wired.actor.ask_operator(
                "Changed?", options=["yes", "no"], context="Supply air rose at 14:02."
            )
        )
        await wired.settle()
        await until(lambda: wired.book, what="the question in the published book")

        (published,) = wired.book
        assert published.context == "Supply air rose at 14:02."
        assert "Supply air" not in json.dumps(wired.chat_frames)
        await wired.answer("yes")
        await asking

    async def test_it_is_redacted_and_cut_before_it_is_kept(self, wired: Wired) -> None:
        asking = asyncio.create_task(
            wired.actor.ask_operator(
                "Changed?",
                options=["yes"],
                context=f"  password=hunter2 {'x' * MAX_CONTEXT_CHARS}",
            )
        )
        await wired.settle()
        await until(lambda: wired.book, what="the question in the published book")

        (question,) = wired.actor.questions().values()
        assert "hunter2" not in question.context
        assert len(question.context) == MAX_CONTEXT_CHARS
        await wired.answer("yes")
        await asking

    def test_a_record_written_before_it_existed_reads_as_none(self) -> None:
        record = Question(id="q", text="t", options=("a",), channel="dashboard").to_dict()
        del record["context"]
        question = Question.from_dict(record)
        assert question is not None
        assert question.context == ""


class TestThePublishedBook:
    """The book, retained on the agent's questions topic, for whoever shows what is waiting."""

    async def test_asking_and_answering_each_publish_the_book(self, wired: Wired) -> None:
        asking = asyncio.create_task(
            wired.actor.ask_operator("Changed?", options=["yes", "no"], channel="telegram")
        )
        await wired.settle()
        (asked,) = wired.book
        assert (asked.text, asked.options, asked.channel, asked.open) == (
            "Changed?",
            ("yes", "no"),
            "telegram",
            True,
        )
        frame = wired.retained[questions_topic(wired.actor.actor_id)]
        assert frame["agent"] == "hvac-watch"

        await wired.answer("no", index=1)
        await asking

        (settled,) = wired.book
        assert (settled.id, settled.answer, settled.answer_channel) == (asked.id, "no", "telegram")

    async def test_a_restarted_agent_says_again_what_its_book_holds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = spec_of(hvac_watch)
        assert spec is not None
        before = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        asking = asyncio.create_task(before.actor.ask_operator("Changed?", options=["yes", "no"]))
        await before.settle()
        question_id = before.chat_frames[-1]["id"]
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking

        # A reader that was down when it was asked: the new instance says it again.
        after = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        try:
            await after.actor.start()
        finally:
            await after.actor.stop()

        (republished,) = after.book
        assert (republished.id, republished.open) == (question_id, True)

    async def test_an_agent_that_never_asked_publishes_no_book(self, wired: Wired) -> None:
        try:
            await wired.actor.start()
        finally:
            await wired.actor.stop()

        assert questions_topic(wired.actor.actor_id) not in wired.retained


class TestPermissions:
    """A question whose answer lets something happen: never settled from the broker."""

    async def test_it_is_in_the_book_and_nothing_listens_for_its_answer(self, wired: Wired) -> None:
        question_id = await wired.actor.ask_permission("Install it?", ["Install", "Refuse"])

        (kept,) = wired.book
        assert (kept.id, kept.permission, kept.open) == (question_id, True, True)
        assert kept.options == ("Install", "Refuse")
        assert wired.subscribed == []

    async def test_a_broker_answer_changes_nothing_for_an_agent_that_listens_there(
        self, wired: Wired
    ) -> None:
        # Main listens on its answer topic for its ordinary questions, so the
        # same topic reaches it for a permission, from anything on the broker.
        ordinary = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()
        ordinary_id = wired.chat_frames[-1]["id"]
        permission_id = await wired.actor.ask_permission("Install it?", ["Install", "Refuse"])

        await wired.answer("Install", id=permission_id)

        assert wired.actor.questions()[permission_id].open
        await wired.answer("yes", id=ordinary_id)
        assert await ordinary == "yes"

    async def test_a_pick_handed_over_is_recorded_once_with_its_note(self, wired: Wired) -> None:
        question_id = await wired.actor.ask_permission("Install it?", ["Install", "Refuse"])
        pick = Answer(
            id=question_id,
            agent_id=wired.actor.actor_id,
            option="Install",
            index=0,
            channel="dashboard",
            note="checked the name",
        )

        await wired.actor.permission_answered(pick)
        await wired.actor.permission_answered(
            Answer(
                id=question_id,
                agent_id=pick.agent_id,
                option="Refuse",
                index=1,
                channel="dashboard",
            )
        )

        (kept,) = wired.book
        assert (kept.answer, kept.answer_channel, kept.answer_note) == (
            "Install",
            "dashboard",
            "checked the name",
        )

    async def test_closing_marks_the_book_and_every_dashboard(self, wired: Wired) -> None:
        question_id = await wired.actor.ask_permission("Install it?", ["Install", "Refuse"])

        await wired.actor.close_permission(question_id, "No answer in time")

        (kept,) = wired.book
        assert (kept.answer, kept.open) == ("No answer in time", False)
        ((_topic, frame),) = [f for f in wired.frames if f[0] == answer_topic(wired.actor.actor_id)]
        assert (frame["id"], frame["option"]) == (question_id, "No answer in time")

    async def test_a_restart_closes_it_and_does_not_start_listening(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = spec_of(hvac_watch)
        assert spec is not None
        before = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        question_id = await before.actor.ask_permission("Install it?", ["Install", "Refuse"])

        after = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        try:
            await after.actor.start()
        finally:
            await after.actor.stop()

        (kept,) = after.book
        assert (kept.id, kept.answer) == (question_id, CUT_SHORT)
        assert after.subscribed == []

    async def test_a_restart_still_listens_for_the_ordinary_question_beside_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = spec_of(hvac_watch)
        assert spec is not None
        before = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        asking = asyncio.create_task(before.actor.ask_operator("Changed?", options=["yes", "no"]))
        await before.settle()
        ordinary_id = before.chat_frames[-1]["id"]
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking
        await before.actor.ask_permission("Install it?", ["Install", "Refuse"])

        after = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        try:
            await after.actor.start()
        finally:
            await after.actor.stop()

        assert [q.id for q in after.actor.open_questions()] == [ordinary_id]
        assert after.subscribed == [answer_topic(after.actor.actor_id)]


class TestTheBook:
    def test_answered_questions_beyond_the_kept_number_go_oldest_first(self) -> None:
        questions = [
            answered(
                Question(id=f"q{i}", text="x", options=("a",), channel="dashboard"),
                "a",
                0,
                "dashboard",
            )
            for i in range(ANSWERED_KEPT + 3)
        ]
        for i, q in enumerate(questions):
            q.answered_at = float(i)
        questions.append(
            Question(id="open", text="y", options=("a",), channel="dashboard", asked_at=0.0)
        )
        book = book_of(questions)
        assert "open" in book
        assert len(book) == ANSWERED_KEPT + 1
        assert "q0" not in book
        assert f"q{ANSWERED_KEPT + 2}" in book

    def test_records_that_are_not_questions_are_left_out(self) -> None:
        book = {
            "q1": Question(id="q1", text="x", options=("a",), channel="dashboard").to_dict(),
            "bad": 3,
            "worse": {"text": "no id"},
        }
        assert list(questions_in(book)) == ["q1"]
        assert questions_in("not a book") == {}

    def test_an_answer_frame_round_trips_through_the_record(self) -> None:
        question = Question(id="q1", text="x", options=("a", "b"), channel="telegram", user="42")
        settled = answered(question, "b", 1, "dashboard")
        assert Question.from_dict(settled.to_dict()) == settled
        assert settled.answered_at > 0
        assert not settled.open

    async def test_the_book_is_persisted_as_plain_data(self, wired: Wired) -> None:
        """A node stores state as JSON, so the record must be nothing but plain values."""
        asking = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()
        await wired.answer("no", index=1)
        await asking
        book = wired.actor.recall(QUESTIONS_KEY, {})
        assert json.loads(json.dumps(book)) == book
        (record,) = book.values()
        assert record["options"] == ["yes", "no"]
        assert record["answer"] == "no"


class TestAQuestionsOwnId:
    """``id=`` names the question, so an agent that asks again never asks twice.

    A repeat that asks the same thing is given the answer, or waits for it; one
    that asks something else under the same id is refused before anything goes.
    """

    #: What one asking of the question says, as `ask_operator` takes it.
    ASKED: ClassVar[dict[str, Any]] = {
        "options": ["yes", "no"],
        "channel": "telegram",
        "user": "42",
        "context": "Flow rose after the valve was last reported closed.",
        "label": {
            "source": "sensors/ahu-1",
            "start": 1_700_000_000.0,
            "values": {"yes": "closed", "no": "open"},
        },
    }

    async def _ask(self, wired: Wired, **over: Any) -> "asyncio.Task[str]":
        asking = asyncio.create_task(
            wired.actor.ask_operator(
                "Is the valve closed?", **{**self.ASKED, "id": "run-7-step-3", **over}
            )
        )
        await wired.settle()
        return asking

    async def test_the_question_goes_out_under_the_id_given(self, wired: Wired) -> None:
        asking = await self._ask(wired)

        (frame,) = wired.chat_frames
        assert frame["id"] == "run-7-step-3"
        (asked,) = wired.book
        assert asked.id == "run-7-step-3"
        await wired.answer("yes")
        assert await asking == "yes"

    @pytest.mark.parametrize("given", [None, "run-7-step-3"])
    async def test_the_book_holds_it_before_it_is_sent(
        self, wired: Wired, given: str | None
    ) -> None:
        # An answer can only come once it is sent; the book is what checks it.
        asking = asyncio.create_task(
            wired.actor.ask_operator("Changed?", options=["yes", "no"], id=given)
        )
        await wired.settle()

        topics = [t for t, _ in wired.frames]
        book_topic = questions_topic(wired.actor.actor_id)
        assert topics.index(book_topic) < next(
            i for i, t in enumerate(topics) if t.endswith("/chat")
        )
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking

    async def test_asked_again_once_answered_it_is_answered_at_once(self, wired: Wired) -> None:
        asking = await self._ask(wired)
        await wired.answer("no", index=1)
        assert await asking == "no"
        sent = len(wired.frames)

        assert (
            await wired.actor.ask_operator("Is the valve closed?", **self.ASKED, id="run-7-step-3")
            == "no"
        )
        assert len(wired.frames) == sent, "nothing is sent again"

    async def test_asked_again_while_open_it_waits_on_the_same_question(self, wired: Wired) -> None:
        first = await self._ask(wired)
        again = asyncio.create_task(
            wired.actor.ask_operator("Is the valve closed?", **self.ASKED, id="run-7-step-3")
        )
        await asyncio.sleep(0)

        await wired.answer("yes")

        assert (await first, await again) == ("yes", "yes")
        assert len(wired.chat_frames) == 1
        assert len(wired.actor.questions()) == 1

    async def test_two_asking_at_once_make_one_question(self, wired: Wired) -> None:
        both = [
            asyncio.create_task(
                wired.actor.ask_operator("Is the valve closed?", **self.ASKED, id="run-7-step-3")
            )
            for _ in range(2)
        ]
        await wired.settle()

        await wired.answer("yes")

        assert await asyncio.gather(*both) == ["yes", "yes"]
        assert len(wired.chat_frames) == 1
        assert list(wired.actor.questions()) == ["run-7-step-3"]

    @pytest.mark.parametrize(
        "other",
        [
            pytest.param({"text": "Is the pump on?"}, id="other words"),
            pytest.param({"options": ["yes", "no", "not sure"]}, id="other options"),
            pytest.param({"channel": "dashboard", "user": ""}, id="another channel"),
            pytest.param({"user": "43"}, id="another person"),
            pytest.param({"context": "Something else."}, id="other context"),
            pytest.param({"label": None}, id="no label"),
            pytest.param(
                {"label": {**ASKED["label"], "values": {"yes": "open", "no": "closed"}}},
                id="another label",
            ),
        ],
    )
    async def test_something_else_under_the_same_id_is_refused(
        self, wired: Wired, other: dict[str, Any]
    ) -> None:
        asking = await self._ask(wired)
        sent = len(wired.frames)
        text = other.pop("text", "Is the valve closed?")

        with pytest.raises(QuestionIdError, match="run-7-step-3"):
            await wired.actor.ask_operator(text, **{**self.ASKED, **other, "id": "run-7-step-3"})

        assert len(wired.frames) == sent, "refused before anything is sent"
        assert isinstance(QuestionIdError("x"), ValueError)
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking

    async def test_how_it_was_delivered_is_not_what_it_asks(self, wired: Wired) -> None:
        # Shown again on the dashboard after a quiet chat channel: the same question.
        asking = await self._ask(wired)
        (asked,) = wired.actor.questions().values()
        await wired.actor._write_question(dataclasses.replace(asked, fallen_back=True))

        again = asyncio.create_task(
            wired.actor.ask_operator("Is the valve closed?", **self.ASKED, id="run-7-step-3")
        )
        await asyncio.sleep(0)
        await wired.answer("yes")

        assert (await asking, await again) == ("yes", "yes")

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param("", id="empty"),
            pytest.param("x" * (MAX_ID_CHARS + 1), id="too long"),
            pytest.param("run-7\nstep-3", id="a line break"),
            pytest.param(7, id="not text"),
        ],
    )
    async def test_an_id_that_is_not_one_is_refused_before_anything_is_sent(
        self, wired: Wired, bad: Any
    ) -> None:
        with pytest.raises(QuestionIdError):
            await wired.actor.ask_operator("Changed?", options=["yes", "no"], id=bad)

        assert wired.frames == []
        assert wired.actor.questions() == {}

    async def test_after_a_restart_it_is_not_asked_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = spec_of(hvac_watch)
        assert spec is not None
        before = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        asking = await self._ask(before)
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking

        # A new instance over the same state, started as the supervisor starts
        # one, asks again, as a replay would.
        after = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        try:
            await after.actor.start()
            again = asyncio.create_task(
                after.actor.ask_operator("Is the valve closed?", **self.ASKED, id="run-7-step-3")
            )
            for _ in range(20):
                await asyncio.sleep(0)
            assert after.chat_frames == [], "asked once, before the restart"

            await after.answer("no", index=1, id="run-7-step-3")

            assert await again == "no"
        finally:
            await after.actor.stop()
        assert after.actor.questions()["run-7-step-3"].answer == "no"


class Gate:
    """Holds the actor's publishes on one topic until released, as a slow broker would."""

    def __init__(self, wired: Wired, monkeypatch: pytest.MonkeyPatch, topic: str) -> None:
        self.reached = asyncio.Event()
        self.open = asyncio.Event()
        passing = wired.actor._mqtt_publish

        async def _publish(
            published: str, payload: Any, retain: bool = False, qos: int = 0
        ) -> None:
            if published == topic and not self.open.is_set():
                self.reached.set()
                await self.open.wait()
            await passing(published, payload, retain=retain, qos=qos)

        monkeypatch.setattr(wired.actor, "_mqtt_publish", _publish)


class TestAQuestionIsSentAtLeastOnce:
    """Recorded first and sent after, a question whose sending is cut short is sent later.

    Sending is a task of its own: a caller cancelled while it runs leaves it to
    finish, one cut short by a stop leaves the question unsent in the book, and
    the next call naming it, or the agent's next start, sends it.
    """

    @staticmethod
    async def _held(
        wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Gate, "asyncio.Task[str]"]:
        gate = Gate(wired, monkeypatch, questions_topic(wired.actor.actor_id))
        asking = asyncio.create_task(
            wired.actor.ask_operator("Changed?", options=["yes", "no"], id="step-1")
        )
        await asyncio.wait_for(gate.reached.wait(), PATIENCE_S)
        return gate, asking

    async def test_a_caller_cancelled_while_it_is_sent_leaves_it_sent(
        self, wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate, asking = await self._held(wired, monkeypatch)
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking

        gate.open.set()

        await until(lambda: len(wired.chat_frames) == 1, what="the question sent all the same")
        await until(lambda: wired.actor.questions()["step-1"].sent, what="it recorded as sent")

    async def test_one_answered_in_the_inbox_before_it_is_sent_is_not_sent(
        self, wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The book is in the Inbox while it goes out; the person answers there first.
        gate, asking = await self._held(wired, monkeypatch)
        answering = asyncio.create_task(wired.answer("yes", channel="dashboard", id="step-1"))
        await asyncio.sleep(0)
        gate.open.set()

        await answering
        assert await asking == "yes"
        for _ in range(20):
            await asyncio.sleep(0)
        assert wired.chat_frames == [], "no message offers a question already answered"
        assert wired.actor.questions()["step-1"].answer == "yes"

    async def test_two_asking_when_the_first_is_cancelled_the_second_still_gets_it(
        self, wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate, first = await self._held(wired, monkeypatch)
        second = asyncio.create_task(
            wired.actor.ask_operator("Changed?", options=["yes", "no"], id="step-1")
        )
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        gate.open.set()
        await until(lambda: len(wired.chat_frames) == 1, what="the question sent once")
        await wired.answer("yes")

        assert await second == "yes"
        assert len(wired.chat_frames) == 1

    async def _cut_short(self, wired: Wired, monkeypatch: pytest.MonkeyPatch) -> None:
        """Leave ``step-1`` in the book unsent, as a stop during its sending does."""
        gate, asking = await self._held(wired, monkeypatch)
        wired.actor._deliveries["step-1"].cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking
        gate.open.set()
        assert wired.chat_frames == []
        assert not wired.actor.questions()["step-1"].sent

    async def test_one_cut_short_is_sent_when_asked_again(
        self, wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._cut_short(wired, monkeypatch)

        again = asyncio.create_task(
            wired.actor.ask_operator("Changed?", options=["yes", "no"], id="step-1")
        )
        await until(lambda: len(wired.chat_frames) == 1, what="the question sent at last")
        await wired.answer("no", index=1)

        assert await again == "no"

    async def test_one_cut_short_is_sent_when_the_agent_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = spec_of(hvac_watch)
        assert spec is not None
        await self._cut_short(
            Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch), monkeypatch
        )

        after = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        try:
            await after.actor.start()
            await until(lambda: len(after.chat_frames) == 1, what="the question sent on start")
            assert after.chat_frames[0]["id"] == "step-1"
            await until(lambda: after.actor.questions()["step-1"].sent, what="it recorded as sent")
        finally:
            await after.actor.stop()

    async def test_a_sent_one_is_not_sent_again_on_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = spec_of(hvac_watch)
        assert spec is not None
        before = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        asking = asyncio.create_task(
            before.actor.ask_operator("Changed?", options=["yes", "no"], id="step-1")
        )
        await until(
            lambda: (
                before.actor.questions().get("step-1") is not None
                and before.actor.questions()["step-1"].sent
            ),
            what="sent",
        )
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking

        after = Wired(spec.build(persistence_dir=str(tmp_path)), monkeypatch)
        try:
            await after.actor.start()
            for _ in range(20):
                await asyncio.sleep(0)
            assert after.chat_frames == []
        finally:
            await after.actor.stop()

    def test_a_record_from_before_reads_as_sent(self) -> None:
        # Recorded after it was sent, as every question was before.
        record = {"id": "q1", "text": "x", "options": ["a"], "channel": "dashboard"}
        question = Question.from_dict(record)
        assert question is not None and question.sent

    async def test_a_permission_is_in_the_book_before_it_is_sent(self, wired: Wired) -> None:
        await wired.actor.ask_permission("Install acme?", ["Install", "Refuse"])

        topics = [t for t, _ in wired.frames]
        assert topics.index(questions_topic(wired.actor.actor_id)) < next(
            i for i, t in enumerate(topics) if t.endswith("/chat")
        )


class TestAGeneratedAgentsQuestions:
    async def test_it_names_its_question_and_reads_it_back_with_the_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        actor = DynamicAgent(code="", name="asker", persistence_dir=str(tmp_path))
        wired = Wired(actor, monkeypatch)
        assert actor._api is not None

        asking = asyncio.create_task(
            actor._api.ask_operator("Changed?", options=["yes", "no"], id="step-1")
        )
        await wired.settle()
        assert wired.chat_frames[-1]["id"] == "step-1"
        await wired.answer("no", index=1, id="step-1", note="swapped the filter")

        assert await asking == "no"
        record = actor._api.question("step-1")
        assert record is not None
        assert (record["answer"], record["answer_note"]) == ("no", "swapped the filter")
        assert actor._api.question("never-asked") is None


class TestAGeneratedAgentsHook:
    def _agent(self, tmp_path: Path, program: dict[str, Any]) -> DynamicAgent:
        actor = DynamicAgent(code="", name="asker", persistence_dir=str(tmp_path))
        actor._ns.update(program)
        return actor

    async def test_the_programs_on_answer_gets_the_question_and_the_pick(
        self, tmp_path: Path
    ) -> None:
        seen: list[tuple[Any, dict[str, Any], str]] = []

        async def on_answer(api: Any, question: dict[str, Any], option: str) -> None:
            seen.append((api, question, option))

        actor = self._agent(tmp_path, {"on_answer": on_answer})
        question = Question(id="q1", text="Changed?", options=("yes", "no"), channel="telegram")

        await actor.on_operator_answer(question, "no")

        ((api, record, option),) = seen
        assert api is actor._api
        assert (record["id"], record["text"], option) == ("q1", "Changed?", "no")

    async def test_a_plain_function_works_too(self, tmp_path: Path) -> None:
        seen: list[str] = []
        actor = self._agent(tmp_path, {"on_answer": lambda api, q, o: seen.append(o)})
        await actor.on_operator_answer(
            Question(id="q1", text="x", options=("a",), channel="dashboard"), "a"
        )
        assert seen == ["a"]

    async def test_a_failing_hook_is_logged_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def on_answer(api: Any, question: dict[str, Any], option: str) -> None:
            raise RuntimeError("the label store is away")

        actor = self._agent(tmp_path, {"on_answer": on_answer})
        with caplog.at_level(logging.WARNING):
            await actor.on_operator_answer(
                Question(id="q1", text="x", options=("a",), channel="dashboard"), "a"
            )
        assert "the label store is away" in caplog.text

    async def test_without_a_hook_the_pick_is_only_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        actor = self._agent(tmp_path, {})
        with caplog.at_level(logging.INFO):
            await actor.on_operator_answer(
                Question(
                    id="q1",
                    text="x",
                    options=("a",),
                    channel="dashboard",
                    answer_channel="dashboard",
                ),
                "a",
            )
        assert "was answered 'a'" in caplog.text


def test_the_answer_vocabulary_is_what_the_actor_reads() -> None:
    """The frame a channel publishes is the one the actor matches: same keys, same topic."""
    answer = Answer(id="q1", agent_id="a", option="x", index=0, channel="telegram", timestamp=5.0)
    assert answer.topic == answer_topic("a")
    assert Answer.from_payload("a", answer.to_payload()) == answer


def test_a_question_waited_on_without_a_timeout_is_described_without_one() -> None:
    """The message for an unanswered question names the wait only when there was one."""
    assert str(NoAnswerError.to_question("asker", "Install it?", 2.5)).endswith("within 2.5s")
    assert str(NoAnswerError.to_question("asker", "Install it?", None)) == (
        "[asker] no answer to 'Install it?'"
    )
