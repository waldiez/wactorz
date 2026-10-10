"""One chat turn, followed wherever it goes.

A turn starts where a person's message enters and travels with the work done to
answer it -- into tasks it starts, in messages to other actors, across the
broker to a node -- so the log lines of one answer can be found together.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request

from wactorz.agents.main.actor import MainActor
from wactorz.core.actor import Actor, Message, MessageType
from wactorz.core.registry import ActorRegistry
from wactorz.core.turns import (
    TURN_KEY,
    begin_turn,
    current_agent,
    current_turn,
    is_task_topic,
    outside_any_turn,
    turn_of,
    turn_scope,
    working_on,
)
from wactorz.monitoring import log_buffer
from wactorz.node.runner import NodeRunner
from wactorz.web import api_logs, chat


class _Recorder(Actor):
    """An actor that notes the turn and agent it handles each message in."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[tuple[str, str]] = []

    async def handle_message(self, message: Message) -> None:
        self.seen.append((current_turn(), current_agent()))


class TestWhereATurnStarts:
    def test_outside_a_turn_there_is_none(self) -> None:
        assert (current_turn(), current_agent()) == ("", "")

    def test_a_scope_starts_one_and_ends_it(self) -> None:
        with turn_scope() as turn:
            assert current_turn() == turn != ""
        assert current_turn() == ""

    def test_an_inner_scope_keeps_the_turn_an_outer_one_started(self) -> None:
        # The dashboard's route starts the turn and hands the message to main,
        # which is also where the other interfaces come in.
        with turn_scope() as outer, turn_scope() as inner:
            assert inner == outer

    def test_a_scope_can_be_given_the_turn_to_join(self) -> None:
        with turn_scope("abc123"):
            assert current_turn() == "abc123"

    async def test_a_command_line_starts_one_per_line(self) -> None:
        async def two_lines() -> tuple[str, str]:
            first = begin_turn()
            second = begin_turn()
            assert current_turn() == second
            return first, second

        first, second = await asyncio.create_task(two_lines())
        assert first != second
        assert current_turn() == ""  # the task's, not the test's

    async def test_tasks_started_inside_it_inherit_it(self) -> None:
        # Which is how a model call or a Home Assistant call is in the turn
        # without being told.
        with turn_scope() as turn:
            inherited = await asyncio.create_task(_turn_now())
        assert inherited == turn


async def _turn_now() -> str:
    return current_turn()


class TestLongLivedTasks:
    async def test_a_loop_started_inside_a_turn_does_not_wear_it(self) -> None:
        # An agent spawned while answering a message starts its loops then;
        # they must not carry that turn into everything they do afterwards.
        async def loop() -> tuple[str, str]:
            return current_turn(), current_agent()

        with turn_scope("spawning-turn"):
            seen = await asyncio.create_task(outside_any_turn(loop, "greeter"))

        assert seen == ("", "greeter")

    async def test_an_actor_started_inside_a_turn_runs_its_loops_outside_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, str]] = []
        actor = _Recorder(name="greeter", persistence_dir=str(tmp_path))

        async def loop() -> None:
            seen.append((current_turn(), current_agent()))

        monkeypatch.setattr(actor, "_message_loop", loop)
        monkeypatch.setattr(actor, "_heartbeat_loop", loop)
        monkeypatch.setattr(actor, "_command_listener", loop)

        async def nothing(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(actor, "_publish_status", nothing)
        with turn_scope("spawning-turn"):
            await actor.start()
            await asyncio.gather(*actor._tasks)

        assert seen == [("", "greeter")] * 3


class TestWorkingOnAMessage:
    def test_both_are_set_and_put_back(self) -> None:
        with working_on("t1", "weather"):
            assert (current_turn(), current_agent()) == ("t1", "weather")
        assert (current_turn(), current_agent()) == ("", "")

    def test_a_message_of_no_turn_is_not_handled_in_a_leftover_one(self) -> None:
        with turn_scope("leftover"), working_on("", "weather"):
            assert current_turn() == ""


class TestMessagesCarryIt:
    def test_a_message_takes_the_turn_it_is_made_in(self) -> None:
        with turn_scope("t9"):
            inside = Message(type=MessageType.TASK, sender_id="a")
        outside = Message(type=MessageType.TASK, sender_id="a")

        assert (inside.turn_id, outside.turn_id) == ("t9", "")

    async def test_the_recipient_handles_it_inside_that_turn_as_itself(
        self, tmp_path: Path
    ) -> None:
        registry = ActorRegistry()
        sender = _Recorder(name="main", persistence_dir=str(tmp_path))
        recipient = _Recorder(name="planner", persistence_dir=str(tmp_path))
        await registry.register(sender)
        await registry.register(recipient)

        with turn_scope("t42"):
            await sender.send(recipient.actor_id, MessageType.TASK, {"q": 1})
        await recipient._dispatch(recipient._mailbox.get_nowait())

        assert recipient.seen == [("t42", "planner")]
        assert current_turn() == ""


class TestLogLines:
    @pytest.fixture(name="buffer")
    def buffer_fixture(self) -> Any:
        log_buffer.uninstall()
        buffer = log_buffer.install(capacity=50)
        yield buffer
        log_buffer.uninstall()

    def test_a_line_names_the_turn_and_agent_it_was_written_for(self, buffer: Any) -> None:
        log = logging.getLogger("wactorz.test.turns")
        log.setLevel(logging.INFO)
        with working_on("t7", "weather"):
            log.info("fetching the forecast")
        log.info("between turns")

        during, between = buffer.snapshot()[-2:]
        assert (during["turn"], during["agent"]) == ("t7", "weather")
        assert "turn" not in between and "agent" not in between

    async def test_the_endpoint_filters_by_turn_and_by_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entries = [
            {"level": "INFO", "origin": "a", "text": "1", "turn": "t1", "agent": "main"},
            {"level": "INFO", "origin": "a", "text": "2", "turn": "t1", "agent": "weather"},
            {"level": "INFO", "origin": "a", "text": "3", "turn": "t2", "agent": "weather"},
            {"level": "INFO", "origin": "a", "text": "4"},
        ]
        fake = type("Fake", (), {"snapshot": lambda self: entries, "capacity": 50})()
        monkeypatch.setattr(api_logs, "get_buffer", lambda: fake)

        async def texts(query: str) -> list[str]:
            response = await api_logs.logs_handler(make_mocked_request("GET", f"/api/logs?{query}"))
            assert isinstance(response.body, bytes)
            return [e["text"] for e in json.loads(response.body)["entries"]]

        assert await texts("turn=t1") == ["1", "2"]
        assert await texts("agent=weather") == ["2", "3"]
        assert await texts("turn=t1&agent=weather") == ["2"]
        # Whole names: `weather` is not `weather-2`.
        assert await texts("agent=weath") == []


class TestAcrossTheBroker:
    @staticmethod
    def _publishing_actor(tmp_path: Path) -> tuple[_Recorder, list[tuple[str, Any]]]:
        sent: list[tuple[str, Any]] = []

        class _Client:
            async def publish(self, topic: str, payload: Any, **_: Any) -> None:
                sent.append((topic, json.loads(payload) if payload else payload))

        actor = _Recorder(name="main", persistence_dir=str(tmp_path))
        actor._mqtt_client = _Client()
        return actor, sent

    async def test_a_task_to_a_node_names_its_turn(self, tmp_path: Path) -> None:
        actor, sent = self._publishing_actor(tmp_path)

        with turn_scope("t3"):
            await actor._mqtt_publish("agents/by-name/counter/task", {"text": "one"})
            await actor._mqtt_publish("agents/abc/logs", {"message": "busy"})
            await actor._mqtt_publish("agents/abc/heartbeat", {"state": "running"})
        await actor._mqtt_publish("agents/by-name/counter/task", {"text": "two"})

        task, logs, heartbeat, outside = (payload for _, payload in sent)
        assert task[TURN_KEY] == "t3"
        assert logs["turn"] == "t3"
        assert "turn" not in heartbeat and TURN_KEY not in heartbeat
        assert TURN_KEY not in outside

    async def test_a_task_that_names_a_turn_keeps_it(self, tmp_path: Path) -> None:
        actor, sent = self._publishing_actor(tmp_path)

        with turn_scope("mine"):
            await actor._mqtt_publish("agents/by-name/x/task", {TURN_KEY: "theirs"})

        assert sent[0][1][TURN_KEY] == "theirs"

    async def test_the_node_works_on_it_inside_that_turn(self, tmp_path: Path) -> None:
        runner = NodeRunner("localhost", 1883, "rpi", state_dir=str(tmp_path))
        seen: list[tuple[str, str]] = []

        class _Agent:
            name = "counter"

            async def run_task(self, payload: Any) -> dict[str, Any]:
                seen.append((current_turn(), current_agent()))
                return {"result": "ok"}

        await runner._run_task(_Agent(), {"text": "one", TURN_KEY: "t3"}, None)  # pyright: ignore[reportArgumentType]

        assert seen == [("t3", "counter")]

    def test_what_counts_as_a_task_and_its_turn(self) -> None:
        assert is_task_topic("agents/by-name/counter/task")
        assert not is_task_topic("agents/by-name/counter/reply/1")
        assert (turn_of({TURN_KEY: "t"}), turn_of({}), turn_of("text"), turn_of({TURN_KEY: 3})) == (
            "t",
            "",
            "",
            "",
        )


class TestTheEntryPoints:
    async def test_main_answers_each_message_in_a_turn_of_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        async def answer(_self: Any, _text: str) -> str:
            seen.append(current_turn())
            return ""

        monkeypatch.setattr(MainActor, "_process_user_input", answer)
        monkeypatch.setattr(MainActor, "_process_user_input_restricted", answer)
        main = MainActor.__new__(MainActor)
        main.name = "main"

        await main.process_user_input("hi")
        await main.process_user_input_restricted("hi")
        with turn_scope("from-the-dashboard"):
            await main.process_user_input("hi")

        assert seen[0] and seen[1] and seen[0] != seen[1]
        assert seen[2] == "from-the-dashboard"

    async def test_mains_work_on_a_message_is_marked_as_mains(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        async def answer(_self: Any, _text: str) -> str:
            seen.append(current_agent())
            return ""

        monkeypatch.setattr(MainActor, "_process_user_input", answer)
        main = MainActor.__new__(MainActor)
        main.name = "main"

        await main.process_user_input("hi")

        assert seen == ["main"]
        assert current_agent() == ""

    async def test_the_dashboards_chat_route_starts_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        async def route(*_args: Any, **_kwargs: Any) -> None:
            seen.append(current_turn())

        monkeypatch.setattr(chat, "_route_chat", route)

        async def reply(_text: str) -> None:
            return None

        await chat.route_chat("hello", reply)

        assert seen and seen[0]
        assert current_turn() == ""
