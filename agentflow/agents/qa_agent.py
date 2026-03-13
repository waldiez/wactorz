"""
QAAgent - Passive safety observer.

Receives a copy of every chat message and publishes `system/qa-flag` on
policy violations. Rule-based only — no LLM required, zero latency.

Checks:
  1. Prompt-injection patterns (user → agent)
  2. Agent error bleed-through (agent → user)
  3. Raw JSON/data bleed (agent returning internal structures)
  4. PII — email-like patterns (any direction)
  5. No-response tracking — flags agents that don't reply within 30s
"""

import asyncio
import json
import logging
import time

from ..core.actor import Actor, ActorState, Message, MessageType

logger = logging.getLogger(__name__)

AGENT_RESPONSE_TIMEOUT_S = 30.0

INJECTION_PATTERNS = [
    "ignore previous instructions",
    "ignore your previous",
    "forget all previous",
    "forget your instructions",
    "you are now",
    "pretend you are",
    "act as if you are",
    "disregard all",
    "override your instructions",
    "new persona",
    "system prompt",
    "jailbreak",
    "dan mode",
]

ERROR_PATTERNS = [
    "script error:",
    "llm error:",
    "rhai error:",
    "panicked at",
    "thread 'main' panicked",
    "(no output)",
    "script not compiled",
]


class QAAgent(Actor):
    """Passive QA/safety observer — no LLM, instant responses."""

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "qa-agent")
        super().__init__(**kwargs)
        self.protected = False
        # agent_name → (excerpt, sent_at)
        self._pending: dict[str, tuple[str, float]] = {}

    async def on_start(self):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {
                "agentId":   self.actor_id,
                "agentName": self.name,
                "agentType": "guardian",
                "timestamp": time.time(),
            },
        )
        self._tasks.append(asyncio.create_task(self._pending_check_loop()))
        logger.info(f"[{self.name}] started")

    # ── Content checks ─────────────────────────────────────────────────────

    @staticmethod
    def _check(from_: str, content: str) -> tuple[str, str] | None:
        if not content:
            return None
        lower = content.lower()

        if from_ == "user" or not from_:
            for pat in INJECTION_PATTERNS:
                if pat in lower:
                    return f"prompt-injection (matched: {pat})", "warning"

        if from_ and from_ != "user":
            for pat in ERROR_PATTERNS:
                if pat in lower:
                    return f"agent-error-exposed ({pat})", "error"
            trimmed = content.strip()
            if (trimmed.startswith("{") or trimmed.startswith("[")) and len(trimmed) > 20:
                try:
                    json.loads(trimmed)
                    return "raw-data-bleed", "warning"
                except json.JSONDecodeError:
                    pass

        for word in content.split():
            if word.startswith("@"):
                continue
            at_pos = word.find("@")
            if at_pos != -1:
                after = word[at_pos + 1:]
                if "." in after and len(after) >= 4 and "/" not in after:
                    return "pii-possible-email", "info"

        return None

    def _publish_flag(self, category: str, severity: str, from_: str, excerpt: str):
        snippet = excerpt[:80]
        asyncio.create_task(
            self._mqtt_publish(
                "system/qa-flag",
                {
                    "agentId":   self.actor_id,
                    "agentName": self.name,
                    "from":      from_,
                    "category":  category,
                    "severity":  severity,
                    "excerpt":   snippet,
                    "message":   f"[QA/{category}] from={from_}: {snippet}",
                    "timestamp": time.time(),
                },
            )
        )

    # ── No-response tracking ───────────────────────────────────────────────

    async def _pending_check_loop(self):
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                await asyncio.sleep(10)
                now = time.time()
                stale = [
                    (agent, excerpt)
                    for agent, (excerpt, sent_at) in list(self._pending.items())
                    if now - sent_at >= AGENT_RESPONSE_TIMEOUT_S
                ]
                for agent, excerpt in stale:
                    logger.warning(f"[{self.name}] no-response: agent={agent}")
                    self._publish_flag("no-response", "warning", agent, excerpt)
                    self._pending.pop(agent, None)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[{self.name}] pending check error: {exc}")

    # ── handle_message ─────────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        # QA receives chat copies as TASK with JSON payload
        payload = msg.payload
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode()
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return
        if not isinstance(payload, dict):
            return

        from_ = payload.get("from", "")
        to = payload.get("to", "")
        content = payload.get("content", "")
        if not content:
            return

        # No-response tracking
        if from_ == "user" or not from_:
            target = to if (to and to != "io-agent") else None
            if not target:
                # parse @mention from content
                first = content.split()[0] if content.split() else ""
                if first.startswith("@"):
                    target = first[1:]
            if target:
                excerpt = content[:60]
                self._pending[target] = (excerpt, time.time())
        elif to == "user" or not to:
            self._pending.pop(from_, None)

        # Content checks
        result = self._check(from_, content)
        if result:
            category, severity = result
            logger.warning(f"[{self.name}] flag: {category} | from={from_}")
            self._publish_flag(category, severity, from_, content)

    def get_status(self) -> dict:
        s = super().get_status()
        s["agent_type"] = "guardian"
        s["pending_responses"] = len(self._pending)
        return s
