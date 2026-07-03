"""GmailAgent — search, read, label, and draft Gmail messages.

Draft-first by design: like Google's hosted Gmail MCP, this agent never sends
mail. It searches/reads/labels and creates drafts for you to review and send.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..core.actor import Message, MessageType
from ..core.integrations.gmail_mcp import GmailMcpClient, gmail_mcp_config_status
from .llm_agent import LLMAgent, LLMProvider

logger = logging.getLogger(__name__)


GMAIL_PARSE_PROMPT = """You convert a user Gmail request into JSON only.

Return exactly one JSON object with:
- action: one of status, search, unread, inbox, read, labels, drafts, create_draft, help
- query: a Gmail search query. For 'search' it filters the list (e.g. 'from:bank',
  'subject:invoice'); for 'read' it is the topic/sender of the single email to open.
- id: message id for the 'read' action (optional; if absent, the top match of query is opened)
- to: recipient email for create_draft
- subject: subject line for create_draft
- body: body text for create_draft
- count: integer max results

Guidance:
- "unread", "any new mail" -> unread
- "inbox", "recent emails", "what's in my inbox" -> inbox
- "emails from X", "mail about Y", "find ..." -> search with an appropriate Gmail query
- "what does the X (one) say", "content of the X", "read/open the X email",
  "what do I have to pay on the X bill" -> read, query=X (the sender/topic, e.g.
  'trello', 'vodafone bill'). Use read (not search) when the user wants the contents
  of one specific email.
- "draft/compose/write an email to X ..." -> create_draft (never send)
- "my drafts" -> drafts
- "labels/folders" -> labels
This agent never sends email; it only drafts.
"""


class GmailAgent(LLMAgent):
    """Gmail agent for searching, reading, labeling, and drafting mail."""

    DESCRIPTION = "Accesses Gmail: search/read mail, list labels and drafts, create drafts (never sends)"
    CAPABILITIES = [
        "gmail",
        "email",
        "mail",
        "inbox",
        "search_email",
        "read_email",
        "create_draft",
        "labels",
    ]
    INPUT_SCHEMA = {
        "text": "Natural language Gmail request, e.g. 'any unread email?' or 'draft a reply to bob@x.com'",
        "operation": "Optional structured action: status, search, unread, inbox, read, labels, drafts, create_draft",
        "query": "Gmail search query for search",
        "to": "Recipient for create_draft",
        "subject": "Subject for create_draft",
        "body": "Body text for create_draft",
        "thread_id": "Thread/message id for read",
    }
    OUTPUT_SCHEMA = {
        "result": "Human-readable Gmail result",
        "status": "Sanitized Gmail configuration status",
    }

    def __init__(self, llm_provider: LLMProvider | None = None, **kwargs) -> None:
        kwargs.setdefault("name", "gmail-agent")
        kwargs.setdefault("system_prompt", GMAIL_PARSE_PROMPT)
        super().__init__(llm_provider=llm_provider, **kwargs)
        self.client = GmailMcpClient()
        self._pending_draft: dict[str, Any] = {}
        self._awaiting_draft = False

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
            action = action_payload.get("action") or action_payload.get("operation") or "inbox"
            count = int(action_payload.get("count") or action_payload.get("pageSize") or 10)

            if action == "help":
                return {"result": _help_text()}

            if action == "status":
                status = gmail_mcp_config_status()
                auth = "connected" if status.get("gmail_mcp_auth") else "not connected yet"
                return {
                    "result": (
                        f"Gmail is {auth}. I can search your mail, show unread and inbox, "
                        "list labels and drafts, and create drafts (I never send)."
                    ),
                    "status": status,
                }

            if action in ("search", "unread", "inbox"):
                query = _query_for(action, action_payload)
                result = await self.client.call_tool(
                    "search_threads", {"query": query, "pageSize": count}
                )
                return {"result": result}

            if action == "read":
                msg_id = str(
                    action_payload.get("id")
                    or action_payload.get("message_id")
                    or action_payload.get("thread_id")
                    or action_payload.get("threadId")
                    or ""
                ).strip()
                query = str(action_payload.get("query") or action_payload.get("q") or "").strip()
                args = {"id": msg_id} if msg_id else {"query": query or "in:inbox"}
                result = await self.client.call_tool("read_email", args)
                return {"result": result}

            if action == "labels":
                return {"result": await self.client.call_tool("list_labels", {})}

            if action == "drafts":
                return {"result": await self.client.call_tool("list_drafts", {})}

            if action == "create_draft":
                to = str(action_payload.get("to") or "").strip()
                subject = str(action_payload.get("subject") or "").strip()
                body = str(action_payload.get("body") or action_payload.get("text") or "").strip()
                if not to or not body:
                    self._pending_draft = {
                        k: v
                        for k, v in {**self._pending_draft, "to": to, "subject": subject, "body": body}.items()
                        if v
                    }
                    self._awaiting_draft = True
                    return {
                        "result": _missing_draft_message(to, body),
                        "missing": [n for n, v in (("to", to), ("body", body)) if not v],
                    }
                args = {"to": to, "subject": subject or "(no subject)", "body": body}
                result = await self.client.call_tool("create_draft", args)
                self._pending_draft = {}
                self._awaiting_draft = False
                return {"result": result}

            return {"result": f"Unsupported Gmail action: {action}"}
        except Exception as exc:
            self.metrics.tasks_failed += 1
            logger.warning("[%s] Gmail request failed: %s", self.name, exc)
            return {"result": f"Gmail error: {exc}", "error": str(exc)}

    async def _resolve_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = payload.get("operation") or payload.get("action")
        if operation:
            data = dict(payload)
            data["action"] = str(operation)
            return data

        text = str(payload.get("text") or payload.get("message") or payload.get("query") or "").strip()
        if not text:
            return {"action": "inbox"}
        if _is_help_request(text):
            return {"action": "help"}

        # Merge a follow-up reply into a pending draft. A bare reply answers the
        # field we just asked for: the body if we already have a recipient, or the
        # recipient if the reply looks like an email address.
        if self._awaiting_draft:
            had_to = bool(self._pending_draft.get("to"))
            merged = {**self._pending_draft, **_parse_draft_details(text), "action": "create_draft"}
            if not merged.get("to"):
                email = re.search(r"[\w.+-]+@[\w.-]+", text)
                if email:
                    merged["to"] = email.group(0)
            # We already had the recipient and were waiting on the body → the reply is the body.
            if had_to and not merged.get("body"):
                merged["body"] = text.strip()
            return {k: v for k, v in merged.items() if v or k == "action"}

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
                if isinstance(data, dict) and (data.get("action") or data.get("operation")):
                    return data
            except Exception as exc:
                logger.debug("[%s] Gmail parse via LLM failed: %s", self.name, exc)

        return _fallback_parse(text)


# ── Helpers ────────────────────────────────────────────────────────────────


def _help_text() -> str:
    return (
        "I can help with your Gmail. Try: 'any unread email?', 'show my inbox', "
        "'emails from stripe', 'find invoices', 'list my labels', or "
        "'draft an email to sam@x.com about lunch'. I create drafts only — I never send."
    )


def _missing_draft_message(to: str, body: str) -> str:
    missing = []
    if not to:
        missing.append("who it's to")
    if not body:
        missing.append("what it should say")
    if missing == ["what it should say"]:
        return "What should the email say?"
    if missing == ["who it's to"]:
        return "Who should I address the draft to?"
    return "Who is it to, and what should it say? e.g. 'to sam@x.com saying running 10 min late'."


def _query_for(action: str, payload: dict[str, Any]) -> str:
    if action == "unread":
        return "is:unread"
    if action == "inbox":
        return "in:inbox"
    return str(payload.get("query") or payload.get("q") or "in:inbox")


def _parse_draft_details(text: str) -> dict[str, Any]:
    details: dict[str, Any] = {}
    to_match = re.search(r"\bto\s+([\w.+-]+@[\w.-]+)", text, re.I)
    if to_match:
        details["to"] = to_match.group(1)
    subj_match = re.search(r"\b(?:subject|about|re)\s+(.+?)(?=\s+(?:saying|body|that says)\b|$)", text, re.I)
    if subj_match:
        details["subject"] = subj_match.group(1).strip(" .")
    body_match = re.search(r"\b(?:saying|body|that says|message)\s+(.+)$", text, re.I)
    if body_match:
        details["body"] = body_match.group(1).strip()
    return details


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

    # Compose intent (verb) — distinct from listing drafts (noun).
    _compose_triggers = (
        "compose", "write an email", "write email", "draft ", "make an email",
        "make a draft", "make draft", "make email", "new email", "reply to",
        "email to", "send an email", "send email",
    )
    if any(w in lower for w in _compose_triggers):
        details = _parse_draft_details(text)
        details["action"] = "create_draft"
        return details

    # Read one specific email's contents (distinct from listing/search).
    read_match = re.search(
        r"\b(?:what does|what'?s in|content[s]? of|read|open)\b\s+"
        r"(?:the\s+|my\s+|latest\s+|last\s+)*(.+?)"
        r"(?:\s+(?:one|email|mail|says?|saying)\b|[?.!,]|$)",
        text,
        re.I,
    )
    if read_match:
        topic = read_match.group(1).strip(" ,.?")
        if topic:
            return {"action": "read", "query": topic}

    if "unread" in lower or "new mail" in lower or "new email" in lower:
        return {"action": "unread", "count": 10}
    if re.search(r"\bmy drafts\b|\blist drafts\b|\bdrafts\b", lower):
        return {"action": "drafts"}
    if "label" in lower or "folder" in lower:
        return {"action": "labels"}

    from_match = re.search(r"\bfrom\s+([\w.+-]+@[\w.-]+|\w[\w .'-]*)", text, re.I)
    if from_match:
        return {"action": "search", "query": f"from:{from_match.group(1).strip()}", "count": 10}

    about_match = re.search(r"\b(?:about|regarding|find|search(?:\s+for)?|containing)\s+(.+)$", text, re.I)
    if about_match:
        return {"action": "search", "query": about_match.group(1).strip(" ?."), "count": 10}

    if "inbox" in lower or "recent" in lower:
        return {"action": "inbox", "count": 10}

    return {"action": "inbox", "count": 10}
