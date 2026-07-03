"""Google Calendar MCP integration client (hosted MCP + REST fallback)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from .google_mcp import (
    GoogleMcpClient,
    GoogleMcpConfig,
    GoogleRestError,
    RestHandler,
    format_mcp_content,  # re-exported for backwards compatibility
    rest_error_message,
)

GOOGLE_CALENDAR_MCP_URL = "https://calendarmcp.googleapis.com/mcp/v1"
GOOGLE_CALENDAR_API_URL = "https://www.googleapis.com/calendar/v3"
DEFAULT_CALENDAR_MCP_SCOPES = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly "
    "https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/calendar.events.freebusy "
    "https://www.googleapis.com/auth/calendar.events.readonly"
)

CALENDAR_CONFIG = GoogleMcpConfig(
    prefix="CALENDAR_MCP",
    label="Calendar",
    default_mcp_url=GOOGLE_CALENDAR_MCP_URL,
    api_base=GOOGLE_CALENDAR_API_URL,
    default_scopes=DEFAULT_CALENDAR_MCP_SCOPES,
    token_filename="calendar_mcp_token.json",
    login_tool="list_calendars",
    client_name="Wactorz Calendar MCP",
)


def calendar_mcp_config_status() -> dict[str, Any]:
    return CALENDAR_CONFIG.config_status()


def calendar_mcp_url() -> str:
    return CALENDAR_CONFIG.mcp_url()


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _format_when(start: dict, end: dict) -> str:
    if start.get("date") and not start.get("dateTime"):
        day = _parse_iso(start["date"])
        return f"{day:%a %b %d} (all day)" if day else ""
    sd = _parse_iso(start.get("dateTime", ""))
    if not sd:
        return ""
    label = f"{sd:%a %b %d, %I:%M %p}".replace(" 0", " ")
    ed = _parse_iso(end.get("dateTime", "")) if end else None
    if ed:
        label += f"–{ed:%I:%M %p}".replace("–0", "–")
    return label


def _format_events(items: list[dict]) -> str:
    if not items:
        return "No events found for that time range."
    lines = []
    for event in items:
        summary = event.get("summary") or "(no title)"
        when = _format_when(event.get("start", {}), event.get("end", {}))
        lines.append(f"• {summary}" + (f" — {when}" if when else ""))
    return "\n".join(lines)


class GoogleCalendarMcpClient(GoogleMcpClient):
    """Client for Google's hosted Calendar MCP server, with a REST v3 fallback."""

    def __init__(self) -> None:
        super().__init__(CALENDAR_CONFIG)

    def _rest_handlers(self) -> dict[str, RestHandler]:
        return {
            "list_events": self._rest_list_events,
            "create_event": self._rest_create_event,
            "delete_event": self._rest_delete_event,
            "list_calendars": self._rest_list_calendars,
        }

    async def _rest_list_events(self, arguments: dict) -> str:
        calendar_id = arguments.get("calendarId") or arguments.get("calendar_id") or "primary"
        params: dict[str, str] = {"singleEvents": "true", "orderBy": "startTime"}
        if arguments.get("startTime"):
            params["timeMin"] = arguments["startTime"]
        if arguments.get("endTime"):
            params["timeMax"] = arguments["endTime"]
        params["maxResults"] = str(int(arguments.get("pageSize") or arguments.get("count") or 10))
        status, data = await self._rest_request(
            "GET", f"/calendars/{quote(calendar_id, safe='')}/events", params=params
        )
        if status != 200:
            raise GoogleRestError(rest_error_message(data))
        return _format_events(data.get("items", []))

    async def _rest_create_event(self, arguments: dict) -> str:
        calendar_id = arguments.get("calendarId") or arguments.get("calendar_id") or "primary"
        summary = arguments.get("summary") or "New event"
        start = {"dateTime": arguments.get("startTime")}
        end = {"dateTime": arguments.get("endTime")}
        tz = arguments.get("timeZone")
        if tz:
            start["timeZone"] = tz
            end["timeZone"] = tz
        body: dict[str, Any] = {"summary": summary, "start": start, "end": end}
        for key in ("location", "description"):
            if arguments.get(key):
                body[key] = arguments[key]
        status, data = await self._rest_request(
            "POST", f"/calendars/{quote(calendar_id, safe='')}/events", json_body=body
        )
        if status not in (200, 201):
            raise GoogleRestError(rest_error_message(data))
        when = _format_when(data.get("start", {}), data.get("end", {}))
        return f"Created '{summary}'" + (f" — {when}" if when else "") + "."

    async def _rest_delete_event(self, arguments: dict) -> str:
        event_id = arguments.get("eventId") or arguments.get("event_id")
        if not event_id:
            raise GoogleRestError("Missing event id")
        calendar_id = arguments.get("calendarId") or arguments.get("calendar_id") or "primary"
        status, data = await self._rest_request(
            "DELETE",
            f"/calendars/{quote(calendar_id, safe='')}/events/{quote(str(event_id), safe='')}",
        )
        if status not in (200, 204):
            raise GoogleRestError(rest_error_message(data))
        return "Event deleted."

    async def _rest_list_calendars(self, arguments: dict) -> str:
        status, data = await self._rest_request(
            "GET", "/users/me/calendarList", params={"maxResults": "50"}
        )
        if status != 200:
            raise GoogleRestError(rest_error_message(data))
        names = [c.get("summary") or "(unnamed)" for c in data.get("items", [])]
        if not names:
            return "No calendars found."
        return "Your calendars:\n" + "\n".join(f"• {n}" for n in names)


__all__ = [
    "GOOGLE_CALENDAR_MCP_URL",
    "GOOGLE_CALENDAR_API_URL",
    "DEFAULT_CALENDAR_MCP_SCOPES",
    "CALENDAR_CONFIG",
    "GoogleCalendarMcpClient",
    "calendar_mcp_config_status",
    "calendar_mcp_url",
    "format_mcp_content",
]
