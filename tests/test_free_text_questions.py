# SPDX-License-Identifier: Apache-2.0
"""A question asked for words: ``ask_operator(..., free_text=True)``.

The person types the answer in the dashboard's Inbox. Words cannot be checked
against options, so they are taken from the dashboard alone, handed to the
agent in the server's process, never heard from the answer topic, which
anything on the broker can write to. The agent records them, then publishes
them for the other dashboards, which show them only as the book recorded.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests.test_ask_operator import Wired, wired_fixture  # noqa: F401  # the fixture, by its name
from tests.test_free_text_on_nodes import _Main as NodeMain
from tests.waiting import until
from wactorz.agents.dynamic import DynamicAgent
from wactorz.core import outbound
from wactorz.core.labels import LabelError
from wactorz.core.outbound import Answer, OptionsError, OutboundMessage, answer_topic
from wactorz.core.questions import MAX_TEXT_CHARS, QuestionIdError, questions_topic, typed_text
from wactorz.interfaces.chat.outbound import WHERE_TO_ANSWER, Outbox
from wactorz.web import answers, events, questions, runtime
from wactorz.web.app import build_app

QUESTION = "What did you change on the AHU?"


def _typed(wired: Wired, text: str, question_id: str = "") -> Answer:
    """The answer the dashboard hands the agent: its words, as the server cleaned them."""
    return Answer(
        id=question_id or wired.chat_frames[-1]["id"],
        agent_id=wired.actor.actor_id,
        option=text,
        index=0,
        channel="dashboard",
    )


async def _asked(wired: Wired, **over: Any) -> "asyncio.Task[str]":
    asking = asyncio.create_task(wired.actor.ask_operator(QUESTION, free_text=True, **over))
    await wired.settle()
    return asking


class TestAsking:
    async def test_it_goes_out_saying_it_is_answered_in_words(self, wired: Wired) -> None:
        asking = await _asked(wired)

        (frame,) = wired.chat_frames
        assert frame["free_text"] is True
        assert "options" not in frame
        (question,) = wired.book
        assert (question.free_text, question.max_chars, question.options) == (
            True,
            MAX_TEXT_CHARS,
            (),
        )
        asking.cancel()

    @pytest.mark.parametrize(
        ("over", "error"),
        [
            pytest.param({"options": ["yes", "no"]}, OptionsError, id="with options"),
            pytest.param(
                {"label": {"source": "s", "start": 1.0, "values": {}}},
                LabelError,
                id="with a label",
            ),
        ],
    )
    async def test_what_cannot_go_with_words_is_refused_before_anything_is_sent(
        self, wired: Wired, over: dict[str, Any], error: type[Exception]
    ) -> None:
        with pytest.raises(error, match="free_text"):
            await wired.actor.ask_operator(QUESTION, free_text=True, **over)

        assert wired.frames == []
        assert wired.actor.questions() == {}

    def test_an_agent_on_main_takes_words(self, wired: Wired) -> None:
        assert wired.actor.takes_words is True

    async def test_on_a_node_that_does_not_enforce_main_signature_it_is_refused(
        self, wired: Wired
    ) -> None:
        # The node's runner says so (`_inject`): the answer would come by a topic
        # anything on the broker could write to.
        wired.actor._node, wired.actor._takes_words = "rpi", False

        with pytest.raises(OptionsError, match="does not enforce main's signature"):
            await wired.actor.ask_operator(QUESTION, free_text=True)

        assert wired.frames == []

    async def test_on_a_node_that_enforces_it_it_is_asked(self, wired: Wired) -> None:
        wired.actor._node, wired.actor._takes_words = "rpi", True

        asking = await _asked(wired)

        assert wired.chat_frames[-1]["free_text"] is True
        asking.cancel()

    async def test_no_options_without_free_text_is_still_no_question(self, wired: Wired) -> None:
        with pytest.raises(OptionsError, match="free_text"):
            await wired.actor.ask_operator(QUESTION)

    async def test_words_and_options_cannot_share_an_id(self, wired: Wired) -> None:
        asking = await _asked(wired, id="step-1")

        with pytest.raises(QuestionIdError):
            await wired.actor.ask_operator(QUESTION, options=["yes", "no"], id="step-1")
        asking.cancel()

    async def test_shown_again_on_the_dashboard_it_still_says_so(self, wired: Wired) -> None:
        asking = asyncio.create_task(
            wired.actor.ask_operator(
                QUESTION, free_text=True, channel="telegram", user="42", timeout=0.01
            )
        )
        await until(lambda: len(wired.chat_frames) == 2, what="the question shown again")

        assert [f.get("free_text") for f in wired.chat_frames] == [True, True]
        asking.cancel()


class TestTheAnswer:
    async def test_the_words_come_back_recorded_then_published(self, wired: Wired) -> None:
        asking = await _asked(wired)

        refused = await wired.actor.take_free_text(_typed(wired, "Swapped the filter."))

        assert refused is None
        assert await asking == "Swapped the filter."
        (question,) = wired.actor.questions().values()
        assert (question.answer, question.answer_index) == ("Swapped the filter.", None)
        topics = [t for t, _ in wired.frames]
        last_book = max(
            i for i, t in enumerate(topics) if t == questions_topic(wired.actor.actor_id)
        )
        echo = topics.index(answer_topic(wired.actor.actor_id))
        assert last_book < echo, "the book records it before it is published"
        assert wired.frames[echo][1]["option"] == "Swapped the filter."

    async def test_a_broker_frame_answers_nothing(self, wired: Wired) -> None:
        asking = await _asked(wired)

        await wired.answer("Anything at all")

        assert not asking.done()
        assert wired.actor.open_questions()[0].answer is None
        asking.cancel()

    async def test_a_second_answer_is_refused(self, wired: Wired) -> None:
        asking = await _asked(wired)
        await wired.actor.take_free_text(_typed(wired, "First."))
        await asking

        assert await wired.actor.take_free_text(_typed(wired, "Second.")) == (
            "This question was already answered."
        )

    async def test_words_for_a_question_with_options_are_refused(self, wired: Wired) -> None:
        asking = asyncio.create_task(wired.actor.ask_operator("Changed?", options=["yes", "no"]))
        await wired.settle()

        refused = await wired.actor.take_free_text(_typed(wired, "yes"))

        assert refused == "This question is answered by picking one of its options."
        assert not asking.done()
        asking.cancel()

    async def test_words_for_a_question_never_asked_are_refused(self, wired: Wired) -> None:
        assert await wired.actor.take_free_text(_typed(wired, "x", "never")) == (
            "This agent asked no such question."
        )

    async def test_one_that_comes_when_nothing_waits_reaches_the_hook(
        self, wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        heard: list[str] = []

        async def on_operator_answer(question: Any, option: str) -> None:
            heard.append(option)

        monkeypatch.setattr(wired.actor, "on_operator_answer", on_operator_answer)
        with pytest.raises(TimeoutError):
            await wired.actor.ask_operator(QUESTION, free_text=True, timeout=0.01)

        await wired.actor.take_free_text(_typed(wired, "Later."))

        assert heard == ["Later."]


class TestTheTyping:
    @pytest.mark.parametrize(
        ("raw", "kept"),
        [
            pytest.param("  Swapped it.  ", "Swapped it.", id="trimmed"),
            pytest.param("Line one\nline\ttwo", "Line one\nline\ttwo", id="line breaks and tabs"),
            pytest.param("bell\x07 and \x1b[31mred", "bell and [31mred", id="control characters"),
            pytest.param(7, "", id="not text"),
            pytest.param(" \n\t ", "", id="nothing left"),
        ],
    )
    def test_it_is_cleaned(self, raw: Any, kept: str) -> None:
        assert typed_text(raw) == kept

    def test_a_key_typed_by_mistake_is_not_kept(self) -> None:
        key = "sk-" + "Ab1" * 12

        assert key not in typed_text(f"use {key} please")

    def test_it_is_bounded(self) -> None:
        assert len(typed_text("x" * (MAX_TEXT_CHARS + 50))) == MAX_TEXT_CHARS


class TestOnAChatChannel:
    async def test_it_says_where_to_answer(self) -> None:
        sent: list[tuple[str, tuple[str, ...]]] = []

        class Sender:
            async def send(self, user: str, text: str, options: Any, **_: Any) -> None:
                sent.append((text, tuple(options)))

        outbox = Outbox()
        outbox._senders["telegram"] = Sender()  # pyright: ignore[reportArgumentType]  # a channel's sender
        message = OutboundMessage(
            id="m1",
            agent_id="a1",
            sender="hvac",
            text=QUESTION,
            channel="telegram",
            user="42",
            free_text=True,
        )

        assert await outbox.deliver(message)
        assert sent == [(f"{QUESTION}\n\n{WHERE_TO_ANSWER}", ())]

    def test_the_frame_says_so(self) -> None:
        frame = {"id": "m1", "to": "user", "content": QUESTION, "free_text": True}

        message = OutboundMessage.from_chat_frame("a1", frame)

        assert message is not None and message.free_text
        plain = OutboundMessage.from_chat_frame("a1", {**frame, "free_text": "yes"})
        assert plain is not None and not plain.free_text, "only true says it"


class _Registry:
    def __init__(self, *actors: Any) -> None:
        self._actors = {actor.actor_id: actor for actor in actors}

    def get(self, actor_id: str) -> Any:
        return self._actors.get(actor_id)

    def find_by_name(self, name: str) -> Any:
        return next((a for a in self._actors.values() if a.name == name), None)


@pytest.fixture(name="client")
async def client_fixture() -> AsyncIterator[TestClient[web.Request, web.Application]]:
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def _ledger_hears(wired: Wired) -> None:
    """Hand the server the agent's book, as the broker would."""
    topic = questions_topic(wired.actor.actor_id)
    events.parse_topic(topic, json.dumps(wired.retained[topic]))


class TestTheDashboardRoute:
    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            runtime,
            "state",
            {"agents": {}, "nodes": {}, "alerts": [], "system_health": {}, "log_feed": []},
        )
        monkeypatch.setattr(runtime, "deleted_agent_ids", [])
        monkeypatch.setattr(questions, "_books", {})

    async def _post(
        self, client: TestClient[web.Request, web.Application], wired: Wired, body: dict[str, Any]
    ) -> Any:
        question_id = wired.chat_frames[-1]["id"]
        return await client.post(
            f"/api/questions/{wired.actor.actor_id}/{question_id}/answer", json=body
        )

    async def test_the_words_reach_the_agent_here_cleaned(
        self,
        client: TestClient[web.Request, web.Application],
        wired: Wired,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(runtime, "registry", _Registry(wired.actor))
        asking = await _asked(wired)
        _ledger_hears(wired)

        response = await self._post(client, wired, {"text": "  Swapped\x07 the filter.  "})

        assert response.status == 200
        assert (await response.json())["answer"]["option"] == "Swapped the filter."
        assert await asking == "Swapped the filter."

    @pytest.mark.parametrize("body", [{"text": "  "}, {"option": "Swapped it."}, {"text": 7}])
    async def test_no_words_is_refused(
        self,
        client: TestClient[web.Request, web.Application],
        wired: Wired,
        monkeypatch: pytest.MonkeyPatch,
        body: dict[str, Any],
    ) -> None:
        monkeypatch.setattr(runtime, "registry", _Registry(wired.actor))
        asking = await _asked(wired)
        _ledger_hears(wired)

        response = await self._post(client, wired, body)

        assert response.status == 400
        assert "words" in (await response.json())["error"]
        assert not asking.done()
        asking.cancel()

    async def test_an_agent_not_in_this_process_is_refused_not_reported_sent(
        self,
        client: TestClient[web.Request, web.Application],
        wired: Wired,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(runtime, "registry", _Registry())
        sent: list[str] = []

        class Broker:
            async def publish(self, topic: str, payload: Any, **_: Any) -> None:
                sent.append(topic)

        monkeypatch.setattr(runtime, "mqtt_client_ref", Broker())
        asking = await _asked(wired)
        _ledger_hears(wired)

        response = await self._post(client, wired, {"text": "Swapped it."})

        assert response.status == 503
        assert "process" in (await response.json())["error"]
        assert sent == [], "nothing goes on the broker for the agent to hear"
        asking.cancel()

    async def test_for_an_agent_on_a_node_the_words_go_through_main(
        self,
        client: TestClient[web.Request, web.Application],
        wired: Wired,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Not in this process: main sends them, signed, to the node it places the agent on.
        main = NodeMain(agent=wired.actor.name)
        monkeypatch.setattr(runtime, "registry", _Registry())
        monkeypatch.setattr(answers, "find_main_actor", lambda _registry: main)
        asking = await _asked(wired)
        _ledger_hears(wired)

        response = await self._post(client, wired, {"text": "  Swapped it.  "})

        assert response.status == 200
        assert (await response.json())["forwarded"] is True
        ((topic, payload),) = main.published
        assert topic == "nodes/rpi/answer"
        assert payload["answer"]["option"] == "Swapped it."
        assert not asking.done(), "answered once the node's agent records it, not before"
        asking.cancel()

    @pytest.mark.parametrize(
        "move",
        [
            pytest.param({"from_node": "rpi", "target_node": ""}, id="node to main"),
            pytest.param({"from_node": "rpi", "target_node": "nuc"}, id="node to node"),
            pytest.param({"from_node": "local", "target_node": "rpi"}, id="main to node"),
        ],
    )
    async def test_while_the_agent_is_being_moved_it_is_refused_to_retry(
        self,
        client: TestClient[web.Request, web.Application],
        wired: Wired,
        monkeypatch: pytest.MonkeyPatch,
        move: dict[str, Any],
    ) -> None:
        # Also for an agent here: one being moved from main is stopped, and its
        # state taken, so words recorded now would stay in the copy left behind.
        main = NodeMain(agent=wired.actor.name)
        main.migration.pending_spawns["t1"] = {"agent_name": wired.actor.name, **move}
        here = move["from_node"] == "local"
        monkeypatch.setattr(runtime, "registry", _Registry(wired.actor) if here else _Registry())
        monkeypatch.setattr(answers, "find_main_actor", lambda _registry: main)
        asking = await _asked(wired)
        _ledger_hears(wired)

        response = await self._post(client, wired, {"text": "Swapped it."})

        assert response.status == 409
        body = await response.json()
        assert body["retry"] is True and "being moved" in body["error"]
        assert main.published == []
        assert wired.actor.questions()[wired.chat_frames[-1]["id"]].open
        asking.cancel()

    async def test_for_an_agent_on_a_node_that_cannot_take_them_it_is_refused(
        self,
        client: TestClient[web.Request, web.Application],
        wired: Wired,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        main = NodeMain(agent=wired.actor.name, signing="warn")
        monkeypatch.setattr(runtime, "registry", _Registry())
        monkeypatch.setattr(answers, "find_main_actor", lambda _registry: main)
        asking = await _asked(wired)
        _ledger_hears(wired)

        response = await self._post(client, wired, {"text": "Swapped it."})

        assert response.status == 503
        assert "does not enforce" in (await response.json())["error"]
        assert main.published == []
        asking.cancel()

    async def test_an_answered_one_is_refused(
        self,
        client: TestClient[web.Request, web.Application],
        wired: Wired,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(runtime, "registry", _Registry(wired.actor))
        asking = await _asked(wired)
        await wired.actor.take_free_text(_typed(wired, "First."))
        await asking
        _ledger_hears(wired)

        response = await self._post(client, wired, {"text": "Second."})

        assert response.status == 409

    async def test_the_websocket_pick_cannot_answer_it(
        self, wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The toast and the thread offer no buttons for one; a pick sent anyway is no option.
        monkeypatch.setattr(runtime, "registry", _Registry(wired.actor))
        asking = await _asked(wired)
        _ledger_hears(wired)

        problem = await answers.answer_from_dashboard(
            {"agent_id": wired.actor.actor_id, "id": wired.chat_frames[-1]["id"], "option": "x"}
        )

        assert problem is not None
        assert not asking.done()
        asking.cancel()


class TestShownOnlyAsRecorded:
    """A dashboard shows an answer in words only as the agent's book recorded it."""

    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(questions, "_books", {})
        monkeypatch.setattr(runtime, "deleted_agent_ids", [])

    async def test_the_agents_own_echo_is_shown_and_anything_else_is_not(
        self, wired: Wired
    ) -> None:
        asking = await _asked(wired)
        _ledger_hears(wired)
        question_id = wired.chat_frames[-1]["id"]
        forged = json.dumps({"id": question_id, "option": "Forged words.", "index": 0})

        assert events.refused_answer(answer_topic(wired.actor.actor_id), forged), "while open"

        await wired.actor.take_free_text(_typed(wired, "Swapped it."))
        await asking
        _ledger_hears(wired)
        (topic, echo) = next(
            (t, p) for t, p in wired.frames if t == answer_topic(wired.actor.actor_id)
        )

        assert not events.refused_answer(topic, json.dumps(echo))
        assert events.refused_answer(topic, forged), "once answered, only its own words"


class TestAGeneratedAgent:
    async def test_it_asks_for_words_too(
        self, wired: Wired, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        actor = DynamicAgent(code="", name="asker")
        dynamic = Wired(actor, monkeypatch)
        assert actor._api is not None
        asking = asyncio.create_task(actor._api.ask_operator(QUESTION, free_text=True))
        await dynamic.settle()

        assert dynamic.chat_frames[-1]["free_text"] is True
        await actor.take_free_text(_typed(dynamic, "Swapped it."))
        assert await asking == "Swapped it."


def test_the_closing_index_is_not_one_of_these() -> None:
    # A free-text echo is index 0; only a permission's closing uses the closing index.
    assert outbound.CLOSED_INDEX != 0
