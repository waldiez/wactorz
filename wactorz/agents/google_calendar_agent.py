"""GoogleCalendarAgent - Read and manage Google Calendar events."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..core.actor import Message, MessageType
from ..core.integrations.google_calendar_mcp import (
    GoogleCalendarMcpClient,
    calendar_mcp_config_status,
)
from .llm_agent import LLMAgent, LLMProvider

logger = logging.getLogger(__name__)


CALENDAR_PARSE_PROMPT = """You convert a user calendar request into JSON only.

Return exactly one JSON object with:
- action: one of status, list_events, today, tomorrow, week, create_event, delete_event
- summary: event title for create_event. If the user did not name it, use "New event".
- start: ISO-8601 datetime with timezone for create_event, or empty string
- end: ISO-8601 datetime with timezone for create_event, or empty string
- location: optional event location
- description: optional event description
- event_id: event id for delete_event
- count: integer max event count

If the user asks to see calendar events, use today/tomorrow/week/list_events.
If creating an event and a title is missing, use "New event".
If start or end is missing, still choose create_event and leave the missing
field empty so the caller can ask a follow-up.
Use the live date/time context from the system prompt to resolve relative dates.
"""


class GoogleCalendarAgent(LLMAgent):
    """Google Calendar agent for listing, creating, and deleting events."""

    DESCRIPTION = "Accesses Google Calendar events: list today/upcoming events, create events, delete events"
    CAPABILITIES = [
        "google_calendar",
        "calendar",
        "schedule",
        "events",
        "list_events",
        "create_event",
        "delete_event",
    ]
    INPUT_SCHEMA = {
        "text": "Natural language calendar request, e.g. 'what is on my calendar today?'",
        "operation": "Optional structured action: status, list_events, today, tomorrow, week, create_event, delete_event",
        "summary": "Event title for create_event",
        "start": "ISO-8601 start datetime for create_event",
        "end": "Optional ISO-8601 end datetime for create_event",
        "event_id": "Event id for delete_event",
    }
    OUTPUT_SCHEMA = {
        "result": "Human-readable calendar result",
        "events": "List of event objects for list operations",
        "event": "Created event object for create_event",
        "status": "Sanitized Calendar configuration status",
    }

    def __init__(self, llm_provider: LLMProvider | None = None, **kwargs) -> None:
        kwargs.setdefault("name", "google-calendar-agent")
        kwargs.setdefault("system_prompt", CALENDAR_PARSE_PROMPT)
        super().__init__(llm_provider=llm_provider, **kwargs)
        self.client = GoogleCalendarMcpClient()
        self._pending_create: dict[str, Any] = {}

    async def chat(self, user_message: str) -> str:
        ts_user = time.time()
        self._conversation_history.append({"role": "user", "content": user_message, "ts": ts_user})
        result = await self._process({"text": user_message})
        response = str(result.get("result") or result)
        self._conversation_history.append({"role": "assistant", "content": response, "ts": time.time()})
        self.persist("conversation_history", self._conversation_history)
        self._log_chat_turn(user_message, response, ts_user=ts_user, ts_reply=time.time())
        return response

    async def chat_stream(self, user_message: str):
        yield await self.chat(user_message)
        yield {}

    async def handle_message(self, msg: Message) -> None:
        if msg.type != MessageType.TASK:
            return
        payload = msg.payload if isinstance(msg.payload, dict) else {"text": str(msg.payload or "")}
        result = await self._process(payload)
        result.setdefault("task", payload.get("task") or payload.get("text") or payload.get("operation"))
        if payload.get("_task_id"):
            result["_task_id"] = payload["_task_id"]
        self.metrics.tasks_completed += 1
        reply_to = payload.get("_reply_to") or msg.reply_to or msg.sender_id
        if reply_to:
            await self.send(reply_to, MessageType.RESULT, result)

    async def _process(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            action_payload = await self._resolve_action(payload)
            text = str(payload.get("text") or payload.get("message") or payload.get("query") or "")
            action_payload = self._merge_calendar_followup(action_payload, text)
            action = action_payload.get("action") or action_payload.get("operation") or "today"

            if action == "help":
                return {"result": _help_text()}

            if action == "status":
                status = calendar_mcp_config_status()
                auth = "connected" if status.get("calendar_mcp_auth") else "not connected yet"
                return {
                    "result": (
                        f"Google Calendar MCP is {auth}. I can show today's events, "
                        "list upcoming events, create events, and delete events by id."
                    ),
                    "status": status,
                }

            if action in ("list_events", "today", "tomorrow", "week"):
                count = int(action_payload.get("count") or action_payload.get("pageSize") or 10)
                arguments = _list_events_arguments(action, count)
                result = await self.client.call_tool("list_events", arguments)
                return {"result": result}

            if action == "create_event":
                summary = str(action_payload.get("summary") or "").strip()
                start = str(action_payload.get("start") or action_payload.get("startTime") or "").strip()
                end = str(action_payload.get("end") or action_payload.get("endTime") or "").strip()
                if not summary and not (payload.get("operation") or payload.get("action")):
                    summary = "New event"
                if not summary or not start or not end:
                    self._pending_create = {
                        key: value
                        for key, value in {**self._pending_create, **action_payload, "summary": summary}.items()
                        if value
                    }
                    return {
                        "result": _missing_create_message(summary, start, end),
                        "missing": [
                            name
                            for name, value in (("summary", summary), ("start", start), ("end", end))
                            if not value
                        ],
                    }
                arguments = {"summary": summary, "startTime": start, "endTime": end}
                for source, target in (
                    ("location", "location"),
                    ("description", "description"),
                    ("calendar_id", "calendarId"),
                    ("calendarId", "calendarId"),
                    ("timeZone", "timeZone"),
                    ("timezone", "timeZone"),
                ):
                    value = action_payload.get(source)
                    if value:
                        arguments[target] = value
                result = await self.client.call_tool("create_event", arguments)
                self._pending_create = {}
                return {"result": result}

            if action == "delete_event":
                event_id = str(action_payload.get("event_id") or action_payload.get("eventId") or "").strip()
                if not event_id:
                    return {"result": "I need the calendar event id to delete an event.", "missing": ["event_id"]}
                result = await self.client.call_tool("delete_event", {"eventId": event_id})
                return {"result": result, "event_id": event_id}

            return {"result": f"Unsupported calendar action: {action}"}
        except Exception as exc:
            self.metrics.tasks_failed += 1
            logger.warning("[%s] Calendar request failed: %s", self.name, exc)
            return {"result": f"Google Calendar error: {exc}", "error": str(exc)}

    async def _resolve_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = payload.get("operation") or payload.get("action")
        if operation:
            data = dict(payload)
            data["action"] = str(operation)
            return data

        text = str(payload.get("text") or payload.get("message") or payload.get("query") or "").strip()
        if not text:
            return {"action": "today"}
        if _is_help_request(text):
            return {"action": "help"}

        if self.llm is not None:
            try:
                response, usage = await self.llm.complete(
                    messages=[{"role": "user", "content": text}],
                    system=self._system_prompt_with_now(),
                    max_tokens=500,
                    reasoning_effort="none",
                )
                self.total_input_tokens += usage.get("input_tokens", 0)
                self.total_output_tokens += usage.get("output_tokens", 0)
                self.total_cost_usd += usage.get("cost_usd", 0.0)
                self._persist_cost()
                data = json.loads(_extract_json(response))
                if isinstance(data, dict):
                    if (data.get("action") or data.get("operation")) == "create_event":
                        data = {**_parse_create_details(text), **data}
                    return data
            except Exception as exc:
                logger.debug("[%s] Calendar parse via LLM failed: %s", self.name, exc)

        return _fallback_parse(text)

    def _merge_calendar_followup(self, action_payload: dict[str, Any], text: str) -> dict[str, Any]:
        if not self._pending_create:
            return action_payload
        action = action_payload.get("action") or action_payload.get("operation")
        if action not in (None, "today", "list_events"):
            return action_payload
        parsed = _parse_create_details(text)
        if not parsed:
            return action_payload
        merged = {**self._pending_create, **parsed, "action": "create_event"}
        return {key: value for key, value in merged.items() if value}


def _calendar_timezone() -> tuple[Any, str | None]:
    """Return ``(tzinfo, iana_name)`` for calendar math.

    ``iana_name`` is only set when it's a zone we can safely hand to the Google
    Calendar API; otherwise it's ``None`` and callers omit the ``timeZone``
    argument (the offset-aware ISO timestamps already pin the window). This
    keeps the agent working on systems without the IANA tz database installed
    (e.g. Windows without the ``tzdata`` package), where even ``ZoneInfo("UTC")``
    raises. Note that the Calendar API rejects a bare ``"UTC"`` key too.
    """
    name = os.getenv("CALENDAR_MCP_TIMEZONE") or os.getenv("TZ")
    if name:
        try:
            return ZoneInfo(name), name
        except Exception:
            logger.debug("Unknown calendar timezone %r; using local time", name)
    local = datetime.now().astimezone().tzinfo or timezone.utc
    return local, getattr(local, "key", None)


def _list_events_arguments(action: str, count: int) -> dict[str, Any]:
    args: dict[str, Any] = {"pageSize": count, "orderBy": "startTime"}
    tz, tz_name = _calendar_timezone()
    now = datetime.now(tz)
    if action == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif action == "tomorrow":
        start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif action == "week":
        start = now
        end = now + timedelta(days=7)
    else:
        return args
    args.update({"startTime": start.isoformat(), "endTime": end.isoformat()})
    if tz_name:
        args["timeZone"] = tz_name
    return args


def _help_text() -> str:
    return (
        "I can help with your Google Calendar. Try: 'what's on today?', "
        "'show this week', 'make an event now for 2 hours', or "
        "'create Dentist tomorrow 3pm to 4pm'."
    )


def _missing_create_message(summary: str, start: str, end: str) -> str:
    missing = []
    if not summary:
        missing.append("a title")
    if not start or not end:
        missing.append("a time window")
    if missing == ["a time window"]:
        return "Sure. What time should I put it on the calendar for? For example: 'today 8am to 7pm'."
    if missing == ["a title"]:
        return "What should I call the event?"
    return "Sure. What should I call it, and when should it be? For example: 'Dentist tomorrow 3pm to 4pm'."


def _parse_create_details(text: str) -> dict[str, Any]:
    lower = text.lower().strip()
    if not lower:
        return {}
    details: dict[str, Any] = {"action": "create_event"}
    title_match = re.search(
        r"\b(?:called|named|title(?:d)?|about)\s+(.+?)(?=\s+(?:today|tomorrow|now|from|at|\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b)|$)",
        text,
        re.I,
    )
    if title_match:
        details["summary"] = title_match.group(1).strip(" .")

    tz, _ = _calendar_timezone()
    now = datetime.now(tz).replace(microsecond=0)
    day = now
    if "tomorrow" in lower:
        day = now + timedelta(days=1)

    duration_match = re.search(r"\bnow\s+for\s+(\d+(?:\.\d+)?)\s*(hour|hours|hr|hrs|minute|minutes|min|mins)\b", lower)
    if duration_match:
        amount = float(duration_match.group(1))
        unit = duration_match.group(2)
        delta = timedelta(hours=amount) if unit.startswith(("hour", "hr")) else timedelta(minutes=amount)
        details["start"] = now.isoformat()
        details["end"] = (now + delta).isoformat()
        return details

    range_match = re.search(
        r"\b(?:from\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:to|-|until)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
        lower,
    )
    if range_match:
        start_hour, start_min, start_ampm, end_hour, end_min, end_ampm = range_match.groups()
        start_dt = _time_on_day(day, int(start_hour), int(start_min or 0), start_ampm or end_ampm)
        end_dt = _time_on_day(day, int(end_hour), int(end_min or 0), end_ampm or start_ampm)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        details["start"] = start_dt.isoformat()
        details["end"] = end_dt.isoformat()
        return details

    at_match = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", lower)
    if at_match:
        hour, minute, ampm = at_match.groups()
        start_dt = _time_on_day(day, int(hour), int(minute or 0), ampm)
        details["start"] = start_dt.isoformat()
        details["end"] = (start_dt + timedelta(hours=1)).isoformat()
    return details


def _time_on_day(day: datetime, hour: int, minute: int, ampm: str | None) -> datetime:
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.S)
    return match.group(0) if match else text


def _is_help_request(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in ("what can you do", "help", "capabilities"))


def _fallback_parse(text: str) -> dict[str, Any]:
    lower = text.lower()
    if _is_help_request(text):
        return {"action": "help"}
    create_details = _parse_create_details(text)
    action = "today"
    if any(word in lower for word in ("create", "add", "schedule", "book", "make an event", "make a meeting")):
        create_details["action"] = "create_event"
        return create_details
    elif "delete" in lower or "remove" in lower or "cancel" in lower:
        action = "delete_event"
    elif "tomorrow" in lower:
        action = "tomorrow"
    elif "week" in lower:
        action = "week"
    elif any(word in lower for word in ("upcoming", "next events", "calendar", "agenda", "schedule")):
        action = "list_events"
    event_id = ""
    event_match = re.search(r"(?:event[_ -]?id|id)\s*[:=]?\s*([\w@.\-]+)", text, re.I)
    if event_match:
        event_id = event_match.group(1)
    return {"action": action, "event_id": event_id, "count": 10}
