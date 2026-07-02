"""Google Calendar REST helpers.

This module intentionally uses aiohttp directly instead of the Google SDK so
Calendar support works in the existing Wactorz runtime without extra imports.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp


GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _timezone() -> str:
    return (
        os.getenv("GOOGLE_CALENDAR_TIMEZONE")
        or os.getenv("WACTORZ_TZ")
        or os.getenv("TZ")
        or "UTC"
    )


def _now() -> datetime:
    try:
        return datetime.now(ZoneInfo(_timezone()))
    except Exception:
        return datetime.now().astimezone()


def _parse_datetime(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value[:-1] + "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            try:
                dt = dt.replace(tzinfo=ZoneInfo(_timezone()))
            except Exception:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _event_time(value: dict[str, Any]) -> str:
    return str(value.get("dateTime") or value.get("date") or "")


def _format_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id"),
        "summary": event.get("summary", "(no title)"),
        "start": _event_time(event.get("start") or {}),
        "end": _event_time(event.get("end") or {}),
        "location": event.get("location"),
        "description": event.get("description"),
        "htmlLink": event.get("htmlLink"),
        "status": event.get("status"),
    }


def _calendar_id(calendar_id: str | None = None) -> str:
    return calendar_id or os.getenv("GOOGLE_CALENDAR_ID") or "primary"


def calendar_config_status() -> dict[str, Any]:
    access = bool(os.getenv("GOOGLE_CALENDAR_ACCESS_TOKEN"))
    refresh = bool(os.getenv("GOOGLE_CALENDAR_REFRESH_TOKEN"))
    client = bool(os.getenv("GOOGLE_CALENDAR_CLIENT_ID") and os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET"))
    return {
        "google_calendar_id": _calendar_id(),
        "google_calendar_auth": access or (refresh and client),
        "google_calendar_access_token": access,
        "google_calendar_refresh_token": refresh,
        "google_calendar_oauth_client": client,
        "google_calendar_timezone": _timezone(),
        "google_calendar_readonly": _truthy(os.getenv("GOOGLE_CALENDAR_READONLY", "")),
    }


@dataclass
class GoogleCalendarClient:
    """Small async Google Calendar client using env-based OAuth credentials."""

    access_token: str = ""
    _cached_access_token: str = ""
    _cached_expires_at: float = 0.0

    def __post_init__(self) -> None:
        self.access_token = self.access_token or os.getenv("GOOGLE_CALENDAR_ACCESS_TOKEN", "")

    async def _token(self) -> str:
        if self.access_token:
            return self.access_token
        if self._cached_access_token and time.time() < self._cached_expires_at - 60:
            return self._cached_access_token

        refresh_token = os.getenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "")
        client_id = os.getenv("GOOGLE_CALENDAR_CLIENT_ID", "")
        client_secret = os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET", "")
        if not (refresh_token and client_id and client_secret):
            raise RuntimeError(
                "Google Calendar is not configured. Set GOOGLE_CALENDAR_ACCESS_TOKEN "
                "or GOOGLE_CALENDAR_CLIENT_ID, GOOGLE_CALENDAR_CLIENT_SECRET, and "
                "GOOGLE_CALENDAR_REFRESH_TOKEN."
            )

        async with aiohttp.ClientSession() as session:
            async with session.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"Google OAuth error {resp.status}: {data}")
                token = str(data.get("access_token") or "")
                if not token:
                    raise RuntimeError("Google OAuth response did not include access_token.")
                self._cached_access_token = token
                self._cached_expires_at = time.time() + int(data.get("expires_in") or 3600)
                return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        for attempt in range(2):
            token = await self._token()
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    f"{GOOGLE_CALENDAR_API}{path}",
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp:
                    if resp.status == 204:
                        return {"ok": True}
                    data = await resp.json(content_type=None)
                    if resp.status == 401 and attempt == 0 and not self.access_token:
                        self._cached_access_token = ""
                        self._cached_expires_at = 0.0
                        continue
                    if resp.status >= 400:
                        raise RuntimeError(f"Google Calendar error {resp.status}: {data}")
                    return data
        raise RuntimeError("Google Calendar authentication failed after token refresh.")

    async def list_events(
        self,
        *,
        calendar_id: str | None = None,
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        now = _now()
        min_dt = _parse_datetime(time_min or "") or now
        max_dt = _parse_datetime(time_max or "") or (min_dt + timedelta(days=7))
        data = await self._request(
            "GET",
            f"/calendars/{quote(_calendar_id(calendar_id), safe='')}/events",
            params={
                "timeMin": min_dt.isoformat(),
                "timeMax": max_dt.isoformat(),
                "maxResults": max(1, min(int(max_results or 10), 100)),
                "singleEvents": "true",
                "orderBy": "startTime",
            },
        )
        return [_format_event(item) for item in data.get("items", [])]

    async def list_range(
        self,
        range_name: str,
        *,
        calendar_id: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        now = _now()
        start = now
        end = now + timedelta(days=7)
        if range_name == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
        elif range_name == "tomorrow":
            start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
        elif range_name == "week":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=7)
        return await self.list_events(
            calendar_id=calendar_id,
            time_min=start.isoformat(),
            time_max=end.isoformat(),
            max_results=max_results,
        )

    async def create_event(
        self,
        *,
        summary: str,
        start: str,
        end: str = "",
        calendar_id: str | None = None,
        location: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        if _truthy(os.getenv("GOOGLE_CALENDAR_READONLY", "")):
            raise RuntimeError("Google Calendar is in read-only mode.")
        start_dt = _parse_datetime(start)
        if not start_dt:
            raise ValueError("start must be an ISO-8601 datetime, e.g. 2026-07-03T10:00:00+03:00")
        end_dt = _parse_datetime(end) if end else start_dt + timedelta(hours=1)
        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": _timezone()},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": _timezone()},
        }
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        data = await self._request(
            "POST",
            f"/calendars/{quote(_calendar_id(calendar_id), safe='')}/events",
            json_body=body,
        )
        return _format_event(data)

    async def delete_event(self, event_id: str, *, calendar_id: str | None = None) -> dict[str, Any]:
        if _truthy(os.getenv("GOOGLE_CALENDAR_READONLY", "")):
            raise RuntimeError("Google Calendar is in read-only mode.")
        if not event_id:
            raise ValueError("event_id is required.")
        return await self._request(
            "DELETE",
            f"/calendars/{quote(_calendar_id(calendar_id), safe='')}/events/{quote(event_id, safe='')}",
        )


def format_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "No calendar events found."
    lines = []
    for event in events:
        when = event.get("start") or "unknown time"
        title = event.get("summary") or "(no title)"
        location = f" @ {event['location']}" if event.get("location") else ""
        event_id = f" [{event['id']}]" if event.get("id") else ""
        lines.append(f"- {when}: {title}{location}{event_id}")
    return "\n".join(lines)
