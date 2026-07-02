"""GoogleCalendarAgent - Read and manage Google Calendar events."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..core.actor import Message, MessageType
from ..core.integrations.google_calendar import (
    GoogleCalendarClient,
    calendar_config_status,
    format_events,
)
from .llm_agent import LLMAgent, LLMProvider

logger = logging.getLogger(__name__)


CALENDAR_PARSE_PROMPT = """You convert a user calendar request into JSON only.

Return exactly one JSON object with:
- action: one of status, list_events, today, tomorrow, week, create_event, delete_event
- summary: event title for create_event
- start: ISO-8601 datetime with timezone for create_event, or empty string
- end: ISO-8601 datetime with timezone for create_event, or empty string
- location: optional event location
- description: optional event description
- event_id: event id for delete_event
- count: integer max event count

If the user asks to see calendar events, use today/tomorrow/week/list_events.
If creating an event and any required detail is missing, still choose create_event
and leave the missing field empty so the caller can ask a follow-up.
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
        self.client = GoogleCalendarClient()

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
            action = action_payload.get("action") or action_payload.get("operation") or "today"

            if action == "status":
                status = calendar_config_status()
                return {"result": json.dumps(status, indent=2), "status": status}

            if action in ("list_events", "today", "tomorrow", "week"):
                count = int(action_payload.get("count") or action_payload.get("max_results") or 10)
                if action == "list_events":
                    events = await self.client.list_events(max_results=count)
                else:
                    events = await self.client.list_range(action, max_results=count)
                return {"result": format_events(events), "events": events}

            if action == "create_event":
                summary = str(action_payload.get("summary") or "").strip()
                start = str(action_payload.get("start") or "").strip()
                if not summary or not start:
                    return {
                        "result": "I need an event title and an ISO-8601 start time to create a calendar event.",
                        "missing": [name for name, value in (("summary", summary), ("start", start)) if not value],
                    }
                event = await self.client.create_event(
                    summary=summary,
                    start=start,
                    end=str(action_payload.get("end") or ""),
                    location=str(action_payload.get("location") or ""),
                    description=str(action_payload.get("description") or ""),
                )
                return {"result": f"Created calendar event: {event['summary']} at {event['start']}", "event": event}

            if action == "delete_event":
                event_id = str(action_payload.get("event_id") or action_payload.get("eventId") or "").strip()
                await self.client.delete_event(event_id)
                return {"result": f"Deleted calendar event {event_id}.", "event_id": event_id}

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
                    return data
            except Exception as exc:
                logger.debug("[%s] Calendar parse via LLM failed: %s", self.name, exc)

        return _fallback_parse(text)


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.S)
    return match.group(0) if match else text


def _fallback_parse(text: str) -> dict[str, Any]:
    lower = text.lower()
    action = "today"
    if any(word in lower for word in ("create", "add", "schedule", "book")):
        action = "create_event"
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
