"""The calendar agent's request flow: understanding a request, then serving it.

A request is understood by the model when there is one and by a rule-based
parser otherwise. Event times are computed in the calendar's time zone, named
by `CALENDAR_MCP_TIMEZONE` or `TZ`, and fall back to local time when that name
is not a zone this machine knows — the offsets in the timestamps still pin the
window.

An event missing its title or time is held, and the next message that supplies
the missing part completes it, so "add a meeting" followed by "tomorrow 3pm to
4pm" creates one event rather than listing tomorrow's.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents import google_calendar_agent as cal_mod
from wactorz.agents.google_calendar_agent import (
    GoogleCalendarAgent,
    _calendar_timezone,
    _extract_json,
    _fallback_parse,
    _list_events_arguments,
    _parse_create_details,
    _time_on_day,
)
from wactorz.core.actor import Message, MessageType


class _Llm:
    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)

    async def complete(self, **_kwargs: Any) -> Any:
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply, {"input_tokens": 2, "output_tokens": 1, "cost_usd": 0.001}


class _Client:
    def __init__(self, answer: str | Exception = "ok") -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool: str, args: dict[str, Any]) -> str:
        self.calls.append((tool, args))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _agent(
    tmp_path: Path, llm: _Llm | None = None, answer: str | Exception = "ok"
) -> tuple[GoogleCalendarAgent, _Client]:
    agent = GoogleCalendarAgent(llm_provider=None, persistence_dir=str(tmp_path))
    agent.llm = llm  # pyright: ignore[reportAttributeAccessIssue]
    client = _Client(answer)
    agent.client = client  # pyright: ignore[reportAttributeAccessIssue]
    return agent, client


@pytest.fixture(autouse=True)
def _utc_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALENDAR_MCP_TIMEZONE", "UTC")


class TestTimeZone:
    def test_a_named_zone_is_used_and_handed_to_the_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALENDAR_MCP_TIMEZONE", "Europe/Athens")

        tz, name = _calendar_timezone()

        assert name == "Europe/Athens"
        assert datetime(2026, 7, 1, tzinfo=tz).utcoffset() == timedelta(hours=3)

    def test_an_unknown_zone_falls_back_to_local_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CALENDAR_MCP_TIMEZONE", "Mars/Olympus")

        tz, _ = _calendar_timezone()

        assert tz is not None

    @pytest.mark.parametrize(
        ("action", "days"), [("today", 1), ("tomorrow", 1), ("week", 7), ("list_events", 365)]
    )
    def test_each_listing_covers_its_window(self, action: str, days: int) -> None:
        args = _list_events_arguments(action, 5)

        start = datetime.fromisoformat(args["startTime"])
        end = datetime.fromisoformat(args["endTime"])
        assert end - start == timedelta(days=days)
        assert args["pageSize"] == 5 and args["timeZone"] == "UTC"

    def test_twelve_oclock_is_read_correctly(self) -> None:
        day = datetime(2026, 1, 1, tzinfo=timezone.utc)

        assert _time_on_day(day, 12, 0, "am").hour == 0
        assert _time_on_day(day, 12, 30, "pm").hour == 12
        assert _time_on_day(day, 3, 0, "pm").hour == 15


class TestParsing:
    def test_a_titled_range_tomorrow(self) -> None:
        details = _parse_create_details("add a meeting called Standup tomorrow 11pm to 1am")

        start = datetime.fromisoformat(details["start"])
        end = datetime.fromisoformat(details["end"])
        assert details["summary"] == "Standup"
        assert (start.hour, end.hour) == (23, 1)
        assert end - start == timedelta(hours=2), "an end before the start rolls to the next day"

    def test_now_for_a_duration(self) -> None:
        details = _parse_create_details("block now for 30 minutes")

        start = datetime.fromisoformat(details["start"])
        assert datetime.fromisoformat(details["end"]) - start == timedelta(minutes=30)

    def test_a_single_time_lasts_an_hour(self) -> None:
        details = _parse_create_details("dentist at 3pm")

        start = datetime.fromisoformat(details["start"])
        assert (start.hour, datetime.fromisoformat(details["end"]) - start) == (
            15,
            timedelta(hours=1),
        )

    def test_empty_text_has_no_details(self) -> None:
        assert _parse_create_details("  ") == {}

    @pytest.mark.parametrize(
        ("text", "action"),
        [
            ("help", "help"),
            ("book a room at 2pm", "create_event"),
            ("cancel that", "delete_event"),
            ("what about tomorrow", "tomorrow"),
            ("this week please", "week"),
            ("show my agenda", "list_events"),
            ("hello", "today"),
        ],
    )
    def test_the_rule_based_parser(self, text: str, action: str) -> None:
        assert _fallback_parse(text)["action"] == action

    def test_json_is_found_inside_fences(self) -> None:
        assert _extract_json('```json\n{"action": "week"}\n```') == '{"action": "week"}'


class TestServing:
    async def test_an_empty_request_lists_today(self, tmp_path: Path) -> None:
        agent, client = _agent(tmp_path)

        await agent._process({})

        assert client.calls[0][0] == "list_events"

    async def test_the_model_decides_and_a_create_keeps_the_parsed_times(
        self, tmp_path: Path
    ) -> None:
        agent, client = _agent(
            tmp_path,
            _Llm('{"action": "create_event", "summary": "Dentist", "location": "Main St"}'),
        )

        await agent._process({"text": "dentist at 3pm"})

        tool, args = client.calls[0]
        assert tool == "create_event"
        assert args["summary"] == "Dentist" and args["location"] == "Main St"
        assert datetime.fromisoformat(args["startTime"]).hour == 15
        assert agent.total_cost_usd > 0

    @pytest.mark.parametrize("reply", ["not json", RuntimeError("down")])
    async def test_an_unusable_model_answer_falls_back_to_the_parser(
        self, tmp_path: Path, reply: str | Exception
    ) -> None:
        agent, client = _agent(tmp_path, _Llm(reply))

        await agent._process({"text": "this week"})

        assert client.calls[0][0] == "list_events"

    async def test_an_incomplete_event_is_held_and_completed_by_the_next_message(
        self, tmp_path: Path
    ) -> None:
        agent, client = _agent(tmp_path)

        held = await agent._process({"text": "add an event called Gym"})
        done = await agent._process({"text": "6pm to 7pm"})

        assert held["missing"] == ["start", "end"]
        assert held["result"].startswith("Sure. What time")
        tool, args = client.calls[0]
        assert (tool, args["summary"]) == ("create_event", "Gym")
        assert done == {"result": "ok"}
        assert agent._pending_create == {}

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            (
                {"operation": "create_event", "start": "a", "end": "b"},
                "What should I call the event?",
            ),
            ({"operation": "create_event"}, "Sure. What should I call it, and when"),
        ],
    )
    async def test_the_missing_part_is_asked_for(
        self, tmp_path: Path, payload: dict[str, Any], message: str
    ) -> None:
        agent, _ = _agent(tmp_path)

        assert (await agent._process(payload))["result"].startswith(message)

    async def test_a_structured_create_passes_its_extras(self, tmp_path: Path) -> None:
        agent, client = _agent(tmp_path)

        await agent._process(
            {
                "operation": "create_event",
                "summary": "Trip",
                "startTime": "s",
                "endTime": "e",
                "calendar_id": "work",
                "timezone": "UTC",
                "description": "d",
            }
        )

        assert client.calls == [
            (
                "create_event",
                {
                    "summary": "Trip",
                    "startTime": "s",
                    "endTime": "e",
                    "calendarId": "work",
                    "timeZone": "UTC",
                    "description": "d",
                },
            )
        ]

    async def test_a_followup_that_is_another_action_is_not_merged(self, tmp_path: Path) -> None:
        agent, client = _agent(tmp_path)
        await agent._process({"text": "add an event called Gym"})

        await agent._process({"operation": "week", "text": "at 5pm"})

        assert [tool for tool, _ in client.calls] == ["list_events"]
        assert agent._pending_create == {"action": "create_event", "summary": "Gym"}

    async def test_delete_needs_an_id(self, tmp_path: Path) -> None:
        agent, client = _agent(tmp_path)

        missing = await agent._process({"operation": "delete_event"})
        deleted = await agent._process({"operation": "delete_event", "eventId": "e1"})

        assert missing["missing"] == ["event_id"]
        assert deleted == {"result": "ok", "event_id": "e1"}
        assert client.calls == [("delete_event", {"eventId": "e1"})]

    async def test_status_help_unknown_and_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            cal_mod, "calendar_mcp_config_status", lambda: {"calendar_mcp_auth": False}
        )
        agent, _ = _agent(tmp_path, answer=RuntimeError("quota"))

        assert "not connected yet" in (await agent._process({"operation": "status"}))["result"]
        assert "Google Calendar" in (await agent._process({"text": "what can you do"}))["result"]
        assert await agent._process({"operation": "move"}) == {
            "result": "Unsupported calendar action: move"
        }
        assert await agent._process({"operation": "today"}) == {
            "result": "Google Calendar error: quota",
            "error": "quota",
        }


class TestEntryPoints:
    async def test_chat_and_tasks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        agent, _ = _agent(tmp_path, answer="2 events")
        monkeypatch.setattr(agent, "_log_chat_turn", lambda *a, **k: None)
        sent: list[tuple[str, Any]] = []

        async def _send(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append((target, payload))
            return True

        monkeypatch.setattr(agent, "send", _send)

        chunks = [chunk async for chunk in agent.chat_stream("today")]
        await agent.handle_message(
            Message(
                type=MessageType.TASK,
                sender_id="main",
                payload={"operation": "today", "_task_id": "t"},
            )
        )
        await agent.handle_message(Message(type=MessageType.TASK, sender_id="", payload=None))
        await agent.handle_message(Message(type=MessageType.RESULT, sender_id="main"))

        assert chunks == ["2 events", {}]
        assert sent == [("main", {"result": "2 events", "task": "today", "_task_id": "t"})]
