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

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from tests.waiting import PATIENCE_S, until
from wactorz.agents.dynamic import DynamicAgent
from wactorz.agents.function_agent import agent, spec_of
from wactorz.core.actor import Actor, NoAnswerError
from wactorz.core.outbound import Answer, answer_topic
from wactorz.core.questions import (
    ANSWERED_KEPT,
    QUESTIONS_KEY,
    Question,
    answered,
    book_of,
    questions_in,
    questions_in_frame,
    questions_topic,
)


@agent(name="hvac-watch")
def hvac_watch(payload: dict) -> dict:
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

    async def test_listening_starts_once(self, wired: Wired) -> None:
        first = asyncio.create_task(wired.actor.ask_operator("One?", options=["a", "b"]))
        await wired.settle()
        await wired.answer("a")
        await first
        second = asyncio.create_task(wired.actor.ask_operator("Two?", options=["a", "b"]))
        await asyncio.sleep(0)
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
                raise asyncio.TimeoutError
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
