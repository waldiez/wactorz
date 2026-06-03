"""
CalendarAgent - Google Calendar via the official Google API.

Reads, creates, and deletes events on the user's Google Calendar. Shares
OAuth credentials with GmailAgent (see _google_oauth.py) so the consent
flow only runs once.

Commands (prefix @calendar-agent stripped automatically):
  list [N]                          next N events (default 10)
  today                             events from now until end of day
  week                              events for the next 7 days
  create "<summary>" <start> [end]  create an event - ISO-8601 datetimes
  delete <event_id>                 delete by event id
  status                            auth / config status

Datetimes are interpreted in the calendar's default timezone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..core.actor import Actor, Message, MessageType
from . import _google_oauth as _gauth

logger = logging.getLogger(__name__)

_CAL_ID = "primary"


class CalendarAgent(Actor):
    """Google Calendar read/write."""

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "calendar-agent")
        super().__init__(**kwargs)
        self._service = None
        self._service_lock = asyncio.Lock()

    async def on_start(self):
        await self.publish_manifest(
            description="Google Calendar - list, create and delete events on the primary calendar.",
            capabilities=["calendar.list", "calendar.create", "calendar.delete"],
            input_schema={
                "action":  "list | today | week | create | delete | status",
                "summary": "str - event title (for 'create')",
                "start":   "ISO-8601 datetime (for 'create')",
                "end":     "ISO-8601 datetime (optional, default +1h)",
                "event_id":"str (for 'delete')",
                "count":   "int (for 'list', default 10)",
            },
            output_schema={"events": "list[{id,summary,start,end,location}]"},
        )
        logger.info(f"[{self.name}] Ready.")

    # ── Service lazy-init ────────────────────────────────────────────────

    async def _svc(self):
        if self._service is not None:
            return self._service
        async with self._service_lock:
            if self._service is None:
                self._service = await _gauth.build_service("calendar", "v3")
        return self._service

    # ── Entry points ─────────────────────────────────────────────────────

    async def chat(self, message: str) -> str:
        payload = self._parse(message)
        result = await self._handle_cmd(payload)
        return self._format(result)

    async def handle_message(self, msg: Message):
        if msg.type != MessageType.TASK:
            return
        raw = msg.payload
        if isinstance(raw, dict):
            text = raw.get("text") or raw.get("content") or ""
            parsed = self._parse(text) if text else raw
        else:
            parsed = self._parse(str(raw))
        result = await self._handle_cmd(parsed)
        result["result"] = self._format(result)
        if isinstance(raw, dict):
            for key in ("_task_id", "task"):
                if key in raw:
                    result[key] = raw[key]
        target = msg.reply_to or msg.sender_id
        if target:
            await self.send(target, MessageType.RESULT, result)

    def _parse(self, message: str) -> dict:
        text = message.strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        if not text:
            return {"action": "list"}
        parts = text.split(maxsplit=1)
        verb = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if verb == "list":
            count = int(rest) if rest.isdigit() else 10
            return {"action": "list", "count": count}
        if verb == "today":
            return {"action": "today"}
        if verb == "week":
            return {"action": "week"}
        if verb == "status":
            return {"action": "status"}
        if verb == "delete":
            return {"action": "delete", "event_id": rest}
        if verb == "create":
            # create "title" 2026-05-22T18:00 [2026-05-22T19:00]
            m = re.match(r'"([^"]+)"\s+(\S+)(?:\s+(\S+))?', rest)
            if not m:
                return {"action": "create", "_raw": rest}
            return {"action": "create", "summary": m.group(1), "start": m.group(2), "end": m.group(3)}
        return {"action": "list"}

    async def _handle_cmd(self, payload: dict) -> dict:
        action = (payload.get("action") or "list").lower()
        try:
            if action == "status":
                return {"google_oauth": _gauth.status()}
            if action == "list":
                return await self._list(int(payload.get("count") or 10))
            if action == "today":
                return await self._range_today()
            if action == "week":
                return await self._range_week()
            if action == "create":
                if "_raw" in payload:
                    return {"error": 'create syntax: create "Title" <start-iso> [end-iso]'}
                return await self._create(payload)
            if action == "delete":
                return await self._delete(payload.get("event_id") or "")
        except _gauth.GoogleAuthError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception(f"[{self.name}] failed: {exc}")
            return {"error": f"Calendar API error: {exc}"}
        return {"error": f"Unknown action: {action}"}

    # ── Operations ───────────────────────────────────────────────────────

    async def _list(self, count: int) -> dict:
        svc = await self._svc()
        now = datetime.now(timezone.utc).isoformat()
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: svc.events().list(
                calendarId=_CAL_ID, timeMin=now, maxResults=max(1, min(50, count)),
                singleEvents=True, orderBy="startTime",
            ).execute(),
        )
        return {"events": [_simplify(e) for e in resp.get("items", [])]}

    async def _range_today(self) -> dict:
        now = datetime.now().astimezone()
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return await self._range(now, end)

    async def _range_week(self) -> dict:
        now = datetime.now().astimezone()
        return await self._range(now, now + timedelta(days=7))

    async def _range(self, start: datetime, end: datetime) -> dict:
        svc = await self._svc()
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: svc.events().list(
                calendarId=_CAL_ID,
                timeMin=start.isoformat(), timeMax=end.isoformat(),
                singleEvents=True, orderBy="startTime",
            ).execute(),
        )
        return {"events": [_simplify(e) for e in resp.get("items", [])]}

    async def _create(self, payload: dict) -> dict:
        summary = payload.get("summary") or ""
        start_s = payload.get("start") or ""
        end_s   = payload.get("end")
        if not summary or not start_s:
            return {"error": "Need at least 'summary' and 'start'"}
        try:
            start = datetime.fromisoformat(start_s)
        except ValueError:
            return {"error": f"Invalid start datetime: {start_s}"}
        if end_s:
            try:
                end = datetime.fromisoformat(end_s)
            except ValueError:
                return {"error": f"Invalid end datetime: {end_s}"}
        else:
            end = start + timedelta(hours=1)

        body = {
            "summary": summary,
            "start": {"dateTime": start.isoformat()},
            "end":   {"dateTime": end.isoformat()},
        }
        if payload.get("location"):
            body["location"] = payload["location"]
        if payload.get("description"):
            body["description"] = payload["description"]

        svc = await self._svc()
        loop = asyncio.get_event_loop()
        created = await loop.run_in_executor(
            None,
            lambda: svc.events().insert(calendarId=_CAL_ID, body=body).execute(),
        )
        return {"status": "created", "event": _simplify(created)}

    async def _delete(self, event_id: str) -> dict:
        if not event_id:
            return {"error": "Need event_id"}
        svc = await self._svc()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: svc.events().delete(calendarId=_CAL_ID, eventId=event_id).execute(),
        )
        return {"status": "deleted", "event_id": event_id}

    # ── Formatting ───────────────────────────────────────────────────────

    def _format(self, result: dict) -> str:
        if "error" in result:
            return f"[error] {result['error']}"
        if "events" in result:
            evs = result["events"]
            if not evs:
                return "No upcoming events."
            lines = []
            for e in evs:
                when = e.get("start") or "?"
                loc  = f"  @{e['location']}" if e.get("location") else ""
                lines.append(f"  {when}  {e.get('summary', '(no title)')}{loc}  [{e['id']}]")
            return "\n".join(lines)
        if result.get("status") == "created":
            e = result["event"]
            return f"Created: {e.get('summary')} at {e.get('start')} (id={e['id']})"
        if result.get("status") == "deleted":
            return f"Deleted event {result['event_id']}"
        if "google_oauth" in result:
            return json.dumps(result["google_oauth"], indent=2)
        return str(result)


def _simplify(event: dict) -> dict:
    start = event.get("start") or {}
    end   = event.get("end") or {}
    return {
        "id":       event.get("id"),
        "summary":  event.get("summary"),
        "start":    start.get("dateTime") or start.get("date"),
        "end":      end.get("dateTime") or end.get("date"),
        "location": event.get("location"),
        "html_link": event.get("htmlLink"),
    }
