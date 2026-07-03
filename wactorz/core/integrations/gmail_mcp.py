"""Gmail MCP integration client (hosted MCP + REST fallback).

Mirrors the Calendar integration: Google's hosted Gmail MCP
(``gmailmcp.googleapis.com``) is access-gated and denies tool execution for
non-allowlisted projects, so we fall back to the Gmail REST API (v1) using the
same OAuth token. Gmail's MCP is draft-first (it cannot send mail); the REST
fallback follows the same policy — it drafts, searches, reads and labels, but
never sends.
"""

from __future__ import annotations

import asyncio
import base64
import html
import re
from typing import Any
from urllib.parse import quote

from .google_mcp import (
    GoogleMcpClient,
    GoogleMcpConfig,
    GoogleRestError,
    RestHandler,
    format_mcp_content,  # re-exported for symmetry with the calendar module
    rest_error_message,
)

GOOGLE_GMAIL_MCP_URL = "https://gmailmcp.googleapis.com/mcp/v1"
GOOGLE_GMAIL_API_URL = "https://gmail.googleapis.com/gmail/v1"
DEFAULT_GMAIL_MCP_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly "
    "https://www.googleapis.com/auth/gmail.compose "
    "https://www.googleapis.com/auth/gmail.labels"
)

GMAIL_CONFIG = GoogleMcpConfig(
    prefix="GMAIL_MCP",
    label="Gmail",
    default_mcp_url=GOOGLE_GMAIL_MCP_URL,
    api_base=GOOGLE_GMAIL_API_URL,
    default_scopes=DEFAULT_GMAIL_MCP_SCOPES,
    token_filename="gmail_mcp_token.json",
    login_tool="list_labels",
    client_name="Wactorz Gmail MCP",
)


def gmail_mcp_config_status() -> dict[str, Any]:
    return GMAIL_CONFIG.config_status()


def gmail_mcp_url() -> str:
    return GMAIL_CONFIG.mcp_url()


# Gmail's ``gmail.metadata`` scope (which the hosted MCP requests) forbids the
# free-text ``q`` parameter, but still allows filtering by ``labelIds``. Map the
# common label-style queries so unread/inbox/etc. work regardless of scope.
_LABEL_QUERIES = {
    "is:unread": "UNREAD",
    "in:inbox": "INBOX",
    "is:starred": "STARRED",
    "is:important": "IMPORTANT",
    "in:sent": "SENT",
    "in:spam": "SPAM",
    "in:trash": "TRASH",
    "is:draft": "DRAFT",
}


def _search_params(query: str, limit: int) -> dict[str, str]:
    label = _LABEL_QUERIES.get(query.strip().lower())
    if label:
        return {"labelIds": label, "maxResults": str(limit)}
    return {"q": query, "maxResults": str(limit)}


def _header(headers: list[dict], name: str) -> str:
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _format_message_line(msg: dict) -> str:
    headers = (msg.get("payload") or {}).get("headers", [])
    sender = _header(headers, "From") or "(unknown sender)"
    subject = html.unescape(_header(headers, "Subject") or "(no subject)")
    # Trim a display name's angle-bracketed address for brevity.
    if "<" in sender:
        sender = sender.split("<")[0].strip().strip('"') or sender
    snippet = html.unescape((msg.get("snippet") or "").strip())
    line = f"• {sender} — {subject}"
    if snippet:
        line += f"\n   {snippet[:140]}"
    return line


def _decode_b64(data: str) -> str:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    except Exception:
        return ""


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t ]+", " ", raw)
    raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
    return raw.strip()


def _extract_body(payload: dict) -> str:
    """Pull readable text from a Gmail message payload, preferring text/plain."""
    def find(part: dict, mime: str) -> str:
        if part.get("mimeType") == mime:
            data = (part.get("body") or {}).get("data")
            if data:
                return _decode_b64(data)
        for sub in part.get("parts") or []:
            found = find(sub, mime)
            if found:
                return found
        return ""

    plain = find(payload, "text/plain")
    if plain.strip():
        return plain.strip()
    html_body = find(payload, "text/html")
    if html_body.strip():
        return _strip_html(html_body)
    return ""


class GmailMcpClient(GoogleMcpClient):
    """Client for Google's hosted Gmail MCP server, with a REST v1 fallback."""

    def __init__(self) -> None:
        super().__init__(GMAIL_CONFIG)

    def _rest_handlers(self) -> dict[str, RestHandler]:
        return {
            "search_threads": self._rest_search,
            "list_messages": self._rest_search,
            "get_thread": self._rest_get_thread,
            "read_email": self._rest_read_email,
            "get_message": self._rest_read_email,
            "list_labels": self._rest_list_labels,
            "list_drafts": self._rest_list_drafts,
            "create_draft": self._rest_create_draft,
        }

    async def _rest_search(self, arguments: dict) -> str:
        query = arguments.get("query") or arguments.get("q") or "in:inbox"
        limit = int(arguments.get("pageSize") or arguments.get("maxResults") or 10)
        status, data = await self._rest_request(
            "GET", "/users/me/messages", params=_search_params(query, limit)
        )
        if status != 200:
            msg = rest_error_message(data)
            if "metadata scope" in msg.lower() and "q" in msg.lower():
                msg = (
                    "free-text search needs a non-metadata scope; try 'unread' or 'inbox', "
                    "or re-authorize without the gmail.metadata scope"
                )
            raise GoogleRestError(msg)
        ids = [m["id"] for m in data.get("messages", [])][:limit]
        if not ids:
            return f"No emails found for '{query}'."
        metas = await asyncio.gather(*(self._get_message_meta(i) for i in ids))
        return "\n".join(_format_message_line(m) for m in metas if m)

    async def _get_message_meta(self, message_id: str) -> dict | None:
        status, data = await self._rest_request(
            "GET",
            f"/users/me/messages/{quote(message_id, safe='')}",
            params=[
                ("format", "metadata"),
                ("metadataHeaders", "From"),
                ("metadataHeaders", "Subject"),
                ("metadataHeaders", "Date"),
            ],
        )
        return data if status == 200 else None

    async def _rest_read_email(self, arguments: dict) -> str:
        """Return the full readable body of a single email.

        Accepts an explicit message id, or a ``query`` whose top match is opened.
        """
        message_id = arguments.get("id") or arguments.get("messageId") or arguments.get("message_id")
        if not message_id:
            query = arguments.get("query") or arguments.get("q") or "in:inbox"
            status, data = await self._rest_request(
                "GET", "/users/me/messages", params=_search_params(query, 1)
            )
            if status != 200:
                raise GoogleRestError(rest_error_message(data))
            messages = data.get("messages", [])
            if not messages:
                return f"No email found for '{query}'."
            message_id = messages[0]["id"]
        status, data = await self._rest_request(
            "GET", f"/users/me/messages/{quote(str(message_id), safe='')}", params={"format": "full"}
        )
        if status != 200:
            raise GoogleRestError(rest_error_message(data))
        headers = (data.get("payload") or {}).get("headers", [])
        sender = _header(headers, "From") or "(unknown sender)"
        subject = html.unescape(_header(headers, "Subject") or "(no subject)")
        date = _header(headers, "Date")
        body = _extract_body(data.get("payload") or {}) or html.unescape(data.get("snippet", ""))
        body = body.strip()
        if len(body) > 1800:
            body = body[:1800].rstrip() + "\n… (truncated)"
        head = f"From: {sender}\nSubject: {subject}"
        if date:
            head += f"\nDate: {date}"
        return f"{head}\n\n{body}" if body else f"{head}\n\n(no readable text content)"

    async def _rest_get_thread(self, arguments: dict) -> str:
        thread_id = arguments.get("threadId") or arguments.get("id") or arguments.get("thread_id")
        if not thread_id:
            raise GoogleRestError("Missing thread id")
        status, data = await self._rest_request(
            "GET",
            f"/users/me/threads/{quote(str(thread_id), safe='')}",
            params=[
                ("format", "metadata"),
                ("metadataHeaders", "From"),
                ("metadataHeaders", "Subject"),
                ("metadataHeaders", "Date"),
            ],
        )
        if status != 200:
            raise GoogleRestError(rest_error_message(data))
        msgs = data.get("messages", [])
        if not msgs:
            return "Thread has no messages."
        return "\n".join(_format_message_line(m) for m in msgs)

    async def _rest_list_labels(self, arguments: dict) -> str:
        status, data = await self._rest_request("GET", "/users/me/labels")
        if status != 200:
            raise GoogleRestError(rest_error_message(data))
        names = [lbl.get("name") for lbl in data.get("labels", []) if lbl.get("name")]
        if not names:
            return "No labels found."
        return "Your labels:\n" + "\n".join(f"• {n}" for n in names)

    async def _rest_list_drafts(self, arguments: dict) -> str:
        status, data = await self._rest_request(
            "GET", "/users/me/drafts", params={"maxResults": "20"}
        )
        if status != 200:
            raise GoogleRestError(rest_error_message(data))
        drafts = data.get("drafts", [])
        return f"You have {len(drafts)} draft(s)." if drafts else "No drafts."

    async def _rest_create_draft(self, arguments: dict) -> str:
        to = arguments.get("to") or arguments.get("recipient") or ""
        subject = arguments.get("subject") or arguments.get("summary") or ""
        body = arguments.get("body") or arguments.get("text") or arguments.get("message") or ""
        lines = []
        if to:
            lines.append(f"To: {to}")
        lines.append(f"Subject: {subject}")
        mime = "\r\n".join(lines) + "\r\n\r\n" + body
        raw = base64.urlsafe_b64encode(mime.encode("utf-8")).decode("ascii")
        status, data = await self._rest_request(
            "POST", "/users/me/drafts", json_body={"message": {"raw": raw}}
        )
        if status not in (200, 201):
            raise GoogleRestError(rest_error_message(data))
        target = f" to {to}" if to else ""
        return f"Draft{target} created: '{subject or '(no subject)'}'."


__all__ = [
    "GOOGLE_GMAIL_MCP_URL",
    "GOOGLE_GMAIL_API_URL",
    "DEFAULT_GMAIL_MCP_SCOPES",
    "GMAIL_CONFIG",
    "GmailMcpClient",
    "gmail_mcp_config_status",
    "gmail_mcp_url",
    "format_mcp_content",
]
