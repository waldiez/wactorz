"""SocialCommandGuard — whitelist gate for inbound social-channel messages.

Discord and Telegram are notification-first channels, not the main control
surface. Left unguarded they route straight to ``MainActor.process_user_input``,
which can spawn agents, delete them, and run arbitrary Python via dynamic
agents — an unacceptable capability to hand to anyone who can message a bot.

This guard sits in front of that call. It accepts a small, explicit whitelist of
safe, read-mostly commands and maps each to a *specific* method. Anything else —
free-form natural language, ``spawn …``, ``delete …``, code — is refused and
never reaches the orchestrator. The whitelist is deny-by-default: a message only
runs if its first word is a known command.

Allowed:
  help                 list the allowed commands
  status               read-only agent/system status
  summarize            read-only digest of recent activity
  workflows            list saved pipeline rules (workflows)
  run <workflow-id>    execute an EXISTING saved workflow (predefined agents only)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agents.main_actor import MainActor

logger = logging.getLogger(__name__)


class SocialCommandGuard:
    """Deny-by-default command gate for social channels (Discord/Telegram)."""

    # Order matters only for the help/refusal text.
    ALLOWED: tuple[str, ...] = ("help", "status", "summarize", "workflows", "run")

    def __init__(self, main_actor: MainActor):
        self.main = main_actor

    async def handle(self, text: str) -> str:
        """Classify one inbound message and return the reply to send back.

        Never calls ``process_user_input``: unknown/unsafe input is refused here.
        """
        cmd, arg = self._parse(text)
        if not cmd or cmd == "help":
            return self._help()
        if cmd == "status":
            return self._status()
        if cmd == "summarize":
            return self._summarize()
        if cmd == "workflows":
            return self._workflows()
        if cmd == "run":
            return await self._run(arg)
        return self._refused(cmd)

    # ── Parsing ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse(text: str) -> tuple[str, str]:
        text = (text or "").strip()
        if text.startswith("/"):  # accept '/status' as 'status'
            text = text[1:].lstrip()
        if not text:
            return "", ""
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        return cmd, arg

    # ── Command handlers (all safe / bounded) ───────────────────────────────

    def _help(self) -> str:
        return (
            "This is a notification channel. Available commands:\n"
            "• `status` — running agents and their state\n"
            "• `summarize` — short digest of recent activity\n"
            "• `workflows` — list saved workflows\n"
            "• `run <workflow-id>` — run an existing saved workflow\n\n"
            "Spawning agents, deleting agents, and running code are disabled here."
        )

    def _status(self) -> str:
        registry = getattr(self.main, "_registry", None)
        if registry is None or not hasattr(registry, "all_actors"):
            return "Status unavailable right now."
        actors = registry.all_actors()
        if not actors:
            return "No agents are currently running."
        lines = [f"System status — {len(actors)} agent(s) running:"]
        for actor in sorted(actors, key=lambda a: getattr(a, "name", "")):
            state = getattr(getattr(actor, "state", None), "value", "unknown")
            lines.append(f"• {getattr(actor, 'name', '?')}: {state}")
        return "\n".join(lines)

    def _summarize(self) -> str:
        # Read-only: reuse the rolling conversation summary main already keeps.
        summary = ""
        try:
            summary = (self.main.recall("history_summary") or "").strip()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[social] summarize recall failed: %s", exc)
        if summary:
            return f"Recent activity summary:\n{summary}"
        return "No recent activity to summarize yet."

    def _workflows(self) -> str:
        rules = self._rules()
        if not rules:
            return "No saved workflows. (Create them from the main interface.)"
        lines = ["Saved workflows:"]
        for rule_id, rule in rules.items():
            task = (rule.get("task") or "").strip()
            agents = ", ".join(rule.get("agents", []) or [])
            suffix = f" — {task[:80]}" if task else ""
            lines.append(f"• `{rule_id}`{suffix}" + (f" [{agents}]" if agents else ""))
        lines.append("\nRun one with: `run <workflow-id>`")
        return "\n".join(lines)

    async def _run(self, arg: str) -> str:
        arg = (arg or "").strip()
        if not arg:
            return "Usage: `run <workflow-id>`. See `workflows` for the list."
        rules = self._rules()
        rule = rules.get(arg)
        if rule is None:
            # Deny-by-default: only exact, already-saved workflows may run. This
            # is what stops "run <arbitrary python/agent>" from doing anything.
            return (
                f"No saved workflow `{arg}`. This channel can only run existing "
                f"workflows — see `workflows`."
            )
        if not hasattr(self.main, "run_pipeline"):
            return "Workflow execution is unavailable right now."
        agents = list(rule.get("agents", []) or [])
        goal = rule.get("task", "") or ""
        logger.info("[social] Running saved workflow '%s' (agents=%s)", arg, agents)
        result = await self.main.run_pipeline(goal=goal, agents=agents)
        if isinstance(result, dict):
            return str(result.get("result") or result.get("text") or result)
        return str(result)

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _rules(self) -> dict:
        if not hasattr(self.main, "get_pipeline_rules"):
            return {}
        try:
            return self.main.get_pipeline_rules() or {}
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[social] get_pipeline_rules failed: %s", exc)
            return {}

    def _refused(self, cmd: str) -> str:
        return (
            f"⛔ `{cmd}` isn't allowed from this channel. It's a notification "
            f"channel and only supports: {', '.join(self.ALLOWED)}. "
            f"Spawning, deleting, and running code are disabled here."
        )
