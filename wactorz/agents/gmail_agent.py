"""
GmailAgent - Gmail via the official Google API.

Reads recent mail, runs Gmail search queries, fetches message bodies, and
sends mail. Shares OAuth credentials with CalendarAgent (see _google_oauth.py)
so the consent flow only runs once.

Commands (prefix @gmail-agent stripped automatically):
  recent [N]                  N most recent inbox messages (default 10)
  search <gmail-query>        Gmail search syntax, e.g. 'from:boss is:unread'
  read <message_id>           fetch full message body
  send <to> "<subj>" "<body>" send a plain-text message
  unread                      shortcut for 'search is:unread'
  status                      auth / config status

Gmail search syntax reference:
  https://support.google.com/mail/answer/7190
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from email.message import EmailMessage
from typing import Optional

from ..core.actor import Actor, Message, MessageType
from . import _google_oauth as _gauth

logger = logging.getLogger(__name__)


class GmailAgent(Actor):
    """Gmail read + send."""

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "gmail-agent")
        super().__init__(**kwargs)
        self._service = None
        self._service_lock = asyncio.Lock()

    async def on_start(self):
        await self.publish_manifest(
            description="Gmail - search, read and send mail on the authenticated account.",
            capabilities=["gmail.search", "gmail.read", "gmail.send"],
            input_schema={
                "action":     "recent | search | read | send | unread | status",
                "query":      "str - Gmail search expression",
                "message_id": "str - for 'read'",
                "to":         "str - recipient (for 'send')",
                "subject":    "str (for 'send')",
                "body":       "str (for 'send')",
                "count":      "int (for 'recent', default 10)",
            },
            output_schema={"messages": "list[{id,from,subject,snippet,date}]"},
        )
        logger.info(f"[{self.name}] Ready.")

    async def _svc(self):
        if self._service is not None:
            return self._service
        async with self._service_lock:
            if self._service is None:
                self._service = await _gauth.build_service("gmail", "v1")
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
            return {"action": "recent", "count": 10}
        parts = text.split(maxsplit=1)
        verb = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if verb == "recent":
            count = int(rest) if rest.isdigit() else 10
            return {"action": "recent", "count": count}
        if verb == "unread":
            return {"action": "search", "query": "is:unread"}
        if verb == "search":
            return {"action": "search", "query": rest}
        if verb == "read":
            return {"action": "read", "message_id": rest}
        if verb == "status":
            return {"action": "status"}
        if verb == "send":
            # send <to> "Subject" "Body text"
            m = re.match(r'(\S+)\s+"([^"]+)"\s+"(.+)"', rest, re.DOTALL)
            if not m:
                return {"action": "send", "_raw": rest}
            return {"action": "send", "to": m.group(1), "subject": m.group(2), "body": m.group(3)}
        return {"action": "search", "query": text}

    async def _handle_cmd(self, payload: dict) -> dict:
        action = (payload.get("action") or "recent").lower()
        try:
            if action == "status":
                return {"google_oauth": _gauth.status()}
            if action == "recent":
                return await self._list(query="in:inbox", count=int(payload.get("count") or 10))
            if action == "search":
                q = payload.get("query") or ""
                if not q:
                    return {"error": "Need a search query"}
                return await self._list(query=q, count=int(payload.get("count") or 20))
            if action == "read":
                return await self._read(payload.get("message_id") or "")
            if action == "send":
                if "_raw" in payload:
                    return {"error": 'send syntax: send <to> "Subject" "Body"'}
                return await self._send(
                    payload.get("to") or "",
                    payload.get("subject") or "",
                    payload.get("body") or "",
                )
        except _gauth.GoogleAuthError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.exception(f"[{self.name}] failed: {exc}")
            return {"error": f"Gmail API error: {exc}"}
        return {"error": f"Unknown action: {action}"}

    # ── Operations ───────────────────────────────────────────────────────

    async def _list(self, query: str, count: int) -> dict:
        svc = await self._svc()
        loop = asyncio.get_event_loop()
        listing = await loop.run_in_executor(
            None,
            lambda: svc.users().messages().list(
                userId="me", q=query, maxResults=max(1, min(50, count)),
            ).execute(),
        )
        ids = [m["id"] for m in listing.get("messages", [])]
        if not ids:
            return {"messages": []}

        # Fetch metadata for each id in parallel-ish via executor
        async def _meta(mid: str) -> dict:
            return await loop.run_in_executor(
                None,
                lambda: svc.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute(),
            )
        metas = await asyncio.gather(*[_meta(i) for i in ids])
        return {"messages": [_headers(m) for m in metas]}

    async def _read(self, message_id: str) -> dict:
        if not message_id:
            return {"error": "Need message_id"}
        svc = await self._svc()
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(
            None,
            lambda: svc.users().messages().get(userId="me", id=message_id, format="full").execute(),
        )
        return {"message": _full(msg)}

    async def _send(self, to: str, subject: str, body: str) -> dict:
        if not to or not subject:
            return {"error": "Need 'to' and 'subject'"}
        em = EmailMessage()
        em["To"] = to
        em["Subject"] = subject
        em.set_content(body)
        raw = base64.urlsafe_b64encode(em.as_bytes()).decode()

        svc = await self._svc()
        loop = asyncio.get_event_loop()
        sent = await loop.run_in_executor(
            None,
            lambda: svc.users().messages().send(userId="me", body={"raw": raw}).execute(),
        )
        return {"status": "sent", "message_id": sent.get("id"), "to": to, "subject": subject}

    # ── Formatting ───────────────────────────────────────────────────────

    def _format(self, result: dict) -> str:
        if "error" in result:
            return f"[error] {result['error']}"
        if "messages" in result:
            msgs = result["messages"]
            if not msgs:
                return "No messages."
            lines = []
            for m in msgs:
                lines.append(
                    f"  {m.get('date', '?'):<32} {m.get('from', '?'):<32} "
                    f"{m.get('subject', '(no subject)'):<60} [{m['id']}]"
                )
            return "\n".join(lines)
        if "message" in result:
            m = result["message"]
            return (
                f"From:    {m.get('from')}\n"
                f"To:      {m.get('to')}\n"
                f"Subject: {m.get('subject')}\n"
                f"Date:    {m.get('date')}\n\n"
                f"{m.get('body', '')[:4000]}"
            )
        if result.get("status") == "sent":
            return f"Sent to {result['to']}: {result['subject']} (id={result['message_id']})"
        if "google_oauth" in result:
            return json.dumps(result["google_oauth"], indent=2)
        return str(result)


def _headers(msg: dict) -> dict:
    hdrs = {h["name"].lower(): h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
    return {
        "id":      msg.get("id"),
        "thread":  msg.get("threadId"),
        "from":    hdrs.get("from"),
        "subject": hdrs.get("subject"),
        "date":    hdrs.get("date"),
        "snippet": msg.get("snippet"),
    }


def _full(msg: dict) -> dict:
    base = _headers(msg)
    hdrs = {h["name"].lower(): h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
    base["to"] = hdrs.get("to")
    base["body"] = _extract_body(msg.get("payload") or {})
    return base


def _extract_body(part: dict) -> str:
    mime = part.get("mimeType", "")
    if mime.startswith("text/plain"):
        data = part.get("body", {}).get("data") or ""
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
    for sub in part.get("parts") or []:
        body = _extract_body(sub)
        if body:
            return body
    # Fallback: try text/html and strip tags lazily
    if mime.startswith("text/html"):
        data = part.get("body", {}).get("data") or ""
        html = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", "", html)
    return ""
