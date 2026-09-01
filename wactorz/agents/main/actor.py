"""MainActor - Primary conversational agent and orchestrator.
Spawns DynamicAgents whose core logic is written by the LLM on the fly.
"""

import asyncio
import contextvars
import json
import logging
import re
import socket
from collections.abc import AsyncGenerator
from typing import Any, ClassVar

from ...config import (
    deploy_env_prefix,
    deploy_target,
    deploy_target_help,
    deploy_target_names,
)
from ...core.actor import Actor, Message, MessageType
from ..llm_agent import LLMAgent, LLMProvider
from ..mixins import SpawnMixin, SpawnPlaceholder
from ..one_off_actuator_agent import SOCIAL_ACTUATE_DOMAINS
from ..prompts.main_actor_prompts import (
    ORCHESTRATOR_PROMPT,
)
from .commands import CommandContext
from .commands import registry as command_registry
from .delegation import DelegationManager
from .lifecycle import LifecycleService
from .llm_bridge import LLMBridge
from .manifests import ManifestRegistry
from .memory import MemoryMixin
from .migration import Migration
from .nodes import NodeManager
from .planning import PlanningMixin, starts_with_bypass
from .routing import RoutingMixin
from .spawns import SpawnService
from .turn_actions import TurnActions

logger = logging.getLogger(__name__)


_INTERFACE_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "wactorz_interface_source", default=""
)
_INTERFACE_HISTORY: contextvars.ContextVar[tuple[dict[str, Any], ...]] = contextvars.ContextVar(
    "wactorz_interface_history", default=()
)
_INTERFACE_VOICE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "wactorz_interface_voice", default=False
)
_INTERFACE_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "wactorz_interface_context", default=None
)
_INTERFACE_ACTION_RE = re.compile(
    r"<interface_action>\s*(\{.*?\})\s*</interface_action>",
    re.IGNORECASE | re.DOTALL,
)
_DELEGATE_BLOCK_RE = re.compile(r"<delegate>(.*?)</delegate>", re.IGNORECASE | re.DOTALL)


def _normalize_agent_name(name: str) -> str:
    """Canonicalise an agent name for fuzzy matching.

    Lowercases, turns spaces/underscores into dashes, and strips a redundant
    trailing '-agent' suffix so 'Smart Energy Agent', 'smart_energy_agent',
    and 'smart-energy' all collapse to 'smart-energy'.
    """
    norm = (name or "").lower().strip().replace("_", "-").replace(" ", "-")
    while "--" in norm:
        norm = norm.replace("--", "-")
    norm = norm.strip("-")
    if norm.endswith("-agent") and norm != "-agent":
        norm = norm[: -len("-agent")]
    return norm


def _strip_live_context(message: str) -> str:
    """Remove the [CURRENT SYSTEM STATE...][END SYSTEM STATE] prefix if present.
    Used before fact extraction so the auto-injected agent list doesn't get
    treated as user-stated facts.
    """
    if not isinstance(message, str) or "[CURRENT SYSTEM STATE" not in message:
        return message
    end_marker = "[END SYSTEM STATE]"
    idx = message.find(end_marker)
    if idx == -1:
        return message
    # Skip past the marker and any whitespace following it
    return message[idx + len(end_marker) :].lstrip("\n").lstrip()


def _response_delegates_to(response: str, agent_name: str) -> bool:
    """Return whether model output asks a named agent to execute something."""
    wanted = _normalize_agent_name(agent_name)
    for match in _DELEGATE_BLOCK_RE.finditer(str(response or "")):
        try:
            payload = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        named = str(payload.get("agent") or payload.get("name") or "")
        if _normalize_agent_name(named) == wanted:
            return True
    return any(
        _normalize_agent_name(match.group(1)) == wanted
        for match in re.finditer(r"@([\w][\w-]*)", str(response or ""))
    )


#: Openings that mean the planner, whatever the intent classifier would say.
#:
#: Checked before classification rather than after: someone who writes
#: "pipeline:" has told you what they want, and a classifier that disagrees is
#: overruling them rather than helping.
PLANNER_PREFIXES = (
    "coordinate:",
    "coordinate ",
    "plan:",
    "pipeline:",
    "pipeline ",
    "@planner",
    "set up a pipeline",
    "create a rule",
    "set up a rule",
)


class MainActor(LLMAgent, SpawnMixin, MemoryMixin, RoutingMixin, PlanningMixin):
    DESCRIPTION = "Main orchestrator: spawns agents, routes tasks, manages the multi-agent system"
    CAPABILITIES: ClassVar[list[str]] = [
        "spawn_agent",
        "list_agents",
        "list_nodes",
        "list_topics",
        "orchestration",
    ]

    #: Most queued system notices to carry. They are prepended to the next chat
    #: reply, so the newest are what matter and a long backlog would bury the
    #: answer itself. Oldest are dropped first.
    MAX_PENDING_NOTIFICATIONS = 50

    def __init__(
        self,
        llm_provider: LLMProvider | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("name", "main")
        kwargs.setdefault("system_prompt", ORCHESTRATOR_PROMPT)
        super().__init__(llm_provider=llm_provider, **kwargs)
        self._result_futures: dict[str, asyncio.Future] = {}
        # Queued monitor notifications — prepended to next user response, and
        # capped at MAX_PENDING_NOTIFICATIONS: they drain only when someone
        # chats, so on an idle system with a failing agent nothing empties them.
        self._pending_notifications: list[dict] = []
        self.protected = True
        # Stopping main leaves chat unanswered, which is the surface a user
        # would reach for to start it again.
        self.essential = True
        # Remote node tracking: node_name → {"last_seen": float, "agents": [...]}
        self.manifests = ManifestRegistry(self)
        self.nodes = NodeManager(self, self.manifests)
        self.migration = Migration(self, self.nodes)
        self.llm_bridge = LLMBridge(self)
        self.spawns = SpawnService(self)
        self.delegation = DelegationManager(self)
        self.lifecycle = LifecycleService(self)

    def _current_interface_source(self) -> str:
        """Return the task-local interface excluded from delegation this turn."""
        return _INTERFACE_SOURCE.get()

    def _current_interface_history(self) -> tuple[dict, ...]:
        """Return recent structured turns supplied by the active interface."""
        return _INTERFACE_HISTORY.get()

    def _current_interface_is_voice(self) -> bool:
        """Return whether the active interface used speech recognition."""
        return bool(_INTERFACE_VOICE.get())

    def _current_interface_context(self) -> dict:
        """Return the sanitized capabilities of the active interface."""
        return dict(_INTERFACE_CONTEXT.get() or {})

    def _is_interface_source(self, agent_name: str) -> bool:
        """Return whether an agent name resolves to the active interface."""
        source = self._current_interface_source()
        return bool(source) and _normalize_agent_name(agent_name) == _normalize_agent_name(source)

    @staticmethod
    def _sanitize_interface_context(raw_context: Any, source: str) -> dict[str, Any]:
        """Keep bounded display metadata and explicit action allow-lists."""
        if not isinstance(raw_context, dict):
            return {}
        display_name = str(raw_context.get("display_name") or source or "interface")[:80]
        kind = str(raw_context.get("kind") or "interface")[:80]
        capabilities: dict[str, tuple[str, ...]] = {}
        raw_capabilities = raw_context.get("capabilities")
        if isinstance(raw_capabilities, dict):
            for raw_command, raw_options in list(raw_capabilities.items())[:12]:
                command = str(raw_command).strip().lower()
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", command):
                    continue
                if not isinstance(raw_options, (list, tuple)):
                    continue
                options = []
                for raw_option in list(raw_options)[:20]:
                    option = str(raw_option).strip().lower()
                    if re.fullmatch(r"[a-z][a-z0-9_]{0,39}", option):
                        options.append(option)
                if options:
                    capabilities[command] = tuple(dict.fromkeys(options))
        prompt_note = " ".join(str(raw_context.get("prompt_note") or "").split())[:400]
        if prompt_note and not prompt_note.endswith(" "):
            prompt_note += " "
        return {
            "display_name": display_name,
            "kind": kind,
            "capabilities": capabilities,
            "prompt_note": prompt_note,
        }

    def _extract_interface_actions(self, response: str) -> tuple[str, list[dict[str, str]]]:
        """Remove action blocks and validate them against the interface allow-list."""
        capabilities = self._current_interface_context().get("capabilities") or {}
        actions: list[dict[str, str]] = []
        for match in _INTERFACE_ACTION_RE.finditer(str(response or "")):
            if len(actions) >= 3:
                break
            try:
                candidate = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(candidate, dict):
                continue
            command = str(candidate.get("cmd") or candidate.get("action") or "").lower()
            name = str(candidate.get("name") or "").lower()
            if command and name and name in capabilities.get(command, ()):
                actions.append({"cmd": command, "name": name})
        clean = _INTERFACE_ACTION_RE.sub("", str(response or ""))
        return re.sub(r"\n{3,}", "\n\n", clean).strip(), actions

    def _replace_latest_interface_reply(self, raw_reply: str, clean_reply: str) -> None:
        """Keep interface protocol blocks out of durable conversation history."""
        if raw_reply == clean_reply:
            return
        for item in reversed(self._conversation_history):
            if item.get("role") == "assistant" and item.get("content") == raw_reply:
                item["content"] = clean_reply
                self.persist("conversation_history", self._conversation_history)
                return

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self) -> None:
        await super().on_start()
        # Executable blocks are transport syntax, not conversation. Older
        # versions persisted the raw block, which taught the model to replay a
        # completed or failed action on later, unrelated turns.
        history_changed = False
        for item in self._conversation_history:
            if item.get("role") != "assistant":
                continue
            content = str(item.get("content") or "")
            if not _DELEGATE_BLOCK_RE.search(content):
                continue
            if _response_delegates_to(content, "home-assistant-agent"):
                cleaned = "I couldn't safely complete that request."
            else:
                cleaned = _DELEGATE_BLOCK_RE.sub("", content).strip()
            item["content"] = cleaned
            history_changed = True
        if history_changed:
            self.persist("conversation_history", self._conversation_history)
        await self._restore_spawned_agents()
        # Listen for remote node heartbeats so we know what's online
        self._tasks.append(asyncio.create_task(self._node_heartbeat_listener()))
        # Detect nodes that go silent and clean up their agents
        self._tasks.append(asyncio.create_task(self._node_offline_watcher()))
        # Listen for agent capability manifests to build topic registry
        self._tasks.append(asyncio.create_task(self._manifest_listener()))
        # LLM bridge — lets remote agents call agent.ask_llm() / agent.chat()
        self._tasks.append(asyncio.create_task(self._llm_bridge_listener()))
        # Observe real MQTT payloads from remote agents to populate observed_samples
        self._tasks.append(asyncio.create_task(self._remote_observed_samples_listener()))
        # Receive state + config from remote nodes during remote→local migration
        self._tasks.append(asyncio.create_task(self._state_return_listener()))
        # Inject persisted user facts into system prompt
        self._inject_user_facts_into_prompt()

    # ── Spawn registry ─────────────────────────────────────────────────────

    def _get_spawn_registry(self) -> dict[str, Any]:
        return self.spawns._get_spawn_registry()

    def _restore_earned_trust(self, agent_name: str, local_cfg: dict[str, Any]) -> bool:
        """Re-attach a migrating agent's ``trusted`` flag from our own registry.

        A config coming back from a node arrives over MQTT, where a publisher
        can claim anything, so it is not evidence of anything. Our registry
        entry is: it was written here when the agent was sent out. Reading the
        flag from that record rather than from the wire is what lets a catalog
        agent migrate home without meeting a validator its recipe was never
        written to pass, while a node's claim of trust still buys nothing.

        Returns whether the flag was earned, for the caller to pass as
        ``from_registry``. A claim that was not earned is left in place rather
        than removed here, so the spawn path strips it and says so.
        """
        if not self._get_spawn_registry().get(agent_name, {}).get("trusted"):
            return False
        local_cfg["trusted"] = True
        return True

    def _save_to_spawn_registry(self, config: dict[str, Any]) -> None:
        self.spawns._save_to_spawn_registry(config)

    def _remove_from_spawn_registry(self, name: str) -> None:
        self.spawns._remove_from_spawn_registry(name)

    async def _clear_agent_manifest(self, name: str, actor_id: str | None = None) -> None:
        await self.lifecycle._clear_agent_manifest(name, actor_id)

    def _record_agent_deletion(self, name: str, reason: str = "user request") -> None:
        self.lifecycle._record_agent_deletion(name, reason)

    def get_notification_urls(self) -> dict[str, Any]:
        """Return persisted notification webhook URLs (discord, telegram, slack, etc.)"""
        return self.recall("_notification_urls") or {}

    # ── User facts ─────────────────────────────────────────────────────────
    # Key facts extracted from conversation: HA URL, entity names, preferences,
    # user name, webhook URLs, etc. Stored separately from history so they
    # survive summarization and persist indefinitely.

    async def _restore_spawned_agents(self) -> None:
        await self.spawns._restore_spawned_agents()

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message) -> None:
        if msg.type == MessageType.TASK:
            # Intercept monitor notifications BEFORE passing to LLM _handle_task
            if isinstance(msg.payload, dict) and msg.payload.get("_monitor_notification"):
                self._queue_notification(msg.payload)
                logger.info(
                    "[%s] Monitor alert queued: %s", self.name, msg.payload.get("message", "")[:80]
                )
                return
            await self._handle_task(msg)

    async def _handle_task(self, msg: Message) -> None:
        """Route interface tasks through the full orchestrator without blocking replies."""
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        if payload.get("_via_interface"):
            task = asyncio.create_task(self._handle_interface_request(payload, msg))
            self._tasks.append(task)
            task.add_done_callback(
                lambda done: self._tasks.remove(done) if done in self._tasks else None
            )
            return
        await super()._handle_task(msg)

    async def _handle_interface_request(self, payload: dict[str, Any], msg: Message) -> None:
        """Process one interface turn and reply on its correlation address."""
        text = str(
            payload.get("text")
            or payload.get("message")
            or payload.get("query")
            or payload.get("task")
            or ""
        ).strip()
        task_id = payload.get("_task_id")
        reply_to = payload.get("_reply_to") or msg.reply_to or msg.sender_id
        history = []
        raw_history = payload.get("_interface_history")
        if isinstance(raw_history, list):
            for item in raw_history[-4:]:
                if not isinstance(item, dict):
                    continue
                transcript = str(item.get("transcript") or "").strip()
                response = str(item.get("response") or "").strip()
                if transcript or response:
                    history.append({"transcript": transcript[:1000], "response": response[:2000]})
        source_value = str(payload.get("_interface_source") or "")
        interface_context = self._sanitize_interface_context(
            payload.get("_interface_context"), source_value
        )
        source_token = _INTERFACE_SOURCE.set(source_value)
        history_token = _INTERFACE_HISTORY.set(tuple(history))
        voice_token = _INTERFACE_VOICE.set(bool(payload.get("_interface_voice")))
        context_token = _INTERFACE_CONTEXT.set(interface_context)
        source = self._current_interface_source()
        logger.info("[%s] interface request from %s: %r", self.name, source or "unknown", text[:80])
        try:
            try:
                reply = await self.process_user_input(text) if text else ""
                clean_reply, interface_actions = self._extract_interface_actions(reply)
                if interface_actions and not clean_reply:
                    clean_reply = "Okay."
                self._replace_latest_interface_reply(reply, clean_reply)
                reply = clean_reply
            except Exception as exc:
                logger.warning("[%s] interface request failed: %s", self.name, exc)
                reply = f"[error] {exc}"
                interface_actions = []
        finally:
            _INTERFACE_CONTEXT.reset(context_token)
            _INTERFACE_VOICE.reset(voice_token)
            _INTERFACE_HISTORY.reset(history_token)
            _INTERFACE_SOURCE.reset(source_token)
        if reply_to:
            result: dict[str, Any] = {
                "text": reply,
                "result": reply,
                "task": text,
                "agent": "main",
                "interface_actions": interface_actions,
                "interface_display_name": interface_context.get("display_name", source),
            }
            if task_id:
                result["_task_id"] = task_id
            await self.send(reply_to, MessageType.RESULT, result)

    # ── User input ─────────────────────────────────────────────────────────

    async def chat(self, user_message: str, attachments: list[dict] | None = None) -> str:
        response = await super().chat(user_message, attachments)
        # Fire-and-forget fact extraction — strip auto-injected context first
        clean_msg = _strip_live_context(user_message)
        asyncio.create_task(self._extract_and_save_facts(clean_msg, response))
        return response

    async def chat_stream(
        self, user_message: str, attachments: list[dict[str, Any]] | None = None
    ) -> AsyncGenerator[str | dict[str, Any]]:
        full_response = []
        got_usage = False
        async for chunk in super().chat_stream(user_message, attachments):
            if isinstance(chunk, dict):
                got_usage = True
                yield chunk
            else:
                full_response.append(chunk)
                yield chunk
        # Only extract facts when a real LLM response was received (usage dict present).
        # Skips early-exit cases like cost-limit errors so no extra LLM call is made.
        if full_response and got_usage:
            clean_msg = _strip_live_context(user_message)
            asyncio.create_task(self._extract_and_save_facts(clean_msg, "".join(full_response)))

    async def _record_external_exchange(self, user_message: str, assistant_response: str) -> None:
        """Record a turn that was handled OUTSIDE self.chat() / self.chat_stream() —
        i.e. by the HA, ACTUATE, or PIPELINE branches that return before the LLM
        is called on main. Without this, those exchanges vanish from history and
        future turns have no memory of them.

        Mirrors what LLMAgent.chat() does for OTHER turns:
          - append user + assistant to _conversation_history
          - run rolling summarization if needed
          - persist history to disk
          - trigger fact extraction
        """
        if not user_message or assistant_response is None:
            return
        try:
            self.metrics.messages_processed += 1
            self._conversation_history.append({"role": "user", "content": user_message})
            self._conversation_history.append(
                {"role": "assistant", "content": str(assistant_response)}
            )
            # Same summarization + persistence path that LLMAgent.chat() uses
            await self._maybe_summarize()
            self.persist("conversation_history", self._conversation_history)
        except Exception as e:
            logger.warning("[%s] Failed to record external exchange: %s", self.name, e)
        # Fire-and-forget fact extraction — same as chat()
        asyncio.create_task(self._extract_and_save_facts(user_message, str(assistant_response)))

    def _queue_notification(self, notice: dict[str, Any]) -> None:
        """Queue a system notice for the next chat reply, keeping the newest.

        The queue drains only when someone chats, so an idle system with a
        failing agent has nothing emptying it. Dropping from the front keeps
        the most recent notices, which are the ones still worth reading.
        """
        self._pending_notifications.append(notice)
        excess = len(self._pending_notifications) - self.MAX_PENDING_NOTIFICATIONS
        if excess > 0:
            del self._pending_notifications[:excess]

    def _drain_notifications(self) -> str:
        """Pop queued monitor notifications as a formatted prefix string."""
        if not self._pending_notifications:
            return ""
        icons = {"critical": "\U0001f534", "warning": "\U0001f7e1", "info": "\u2705"}
        lines = []
        for n in self._pending_notifications:
            icon = icons.get(n.get("severity", "warning"), "\u26a0\ufe0f")
            lines.append(f"{icon} **System:** {n.get('message', '').strip()}")
        self._pending_notifications.clear()
        return "\n".join(lines) + "\n\n---\n\n"

    def _restore_unprefixed_turn(self, prefixed_text: str, text: str) -> None:
        """Put the user's own words back into history after the model saw more.

        The model is given the live agent list ahead of the message, because it
        trusts what is in the message over what is in the system prompt. History
        keeps what was actually typed: left prefixed, every turn would carry the
        agent list of the moment it was sent, and the context window would fill
        with stale copies of it.

        Searched from the end because that is where this turn is, and only the
        first match is replaced — the same question asked twice should not have
        its earlier answer rewritten.
        """
        for entry in reversed(self._conversation_history):
            if entry.get("role") == "user" and entry.get("content") == prefixed_text:
                entry["content"] = text
                break
        self.persist("conversation_history", self._conversation_history)

    async def process_user_input(self, text: str) -> str:
        note_prefix = self._drain_notifications()

        # ── Pending-plan response detection ─────────────────────────────────
        # If there's a dry-run plan waiting for approval and the user's message
        # looks like "yes"/"no", handle it BEFORE any other processing. This
        # must come first so a bare "yes" doesn't accidentally hit the intent
        # classifier or get treated as a new prompt. Slash commands and other
        # explicit prefixes are NOT intercepted (the user might want to inspect
        # /plans or /registry while a plan is pending).
        if not text.strip().startswith("/") and not text.strip().startswith("@"):
            plan_response = await self._handle_pending_plan_response(text)
            if plan_response is not None:
                # Record the exchange so history reflects the approval/rejection
                await self._record_external_exchange(text, plan_response)
                return note_prefix + plan_response

            # Pending-plan ambiguity guard: if the user has a plan pending
            # and now types something that looks like another spawn / pipeline
            # request, we'd otherwise silently create the new agents AND
            # later spawn the pending ones too — duplicates galore. Warn
            # the user and ask them to resolve the pending plan first.
            warn = self._warn_if_pending_plan_collision(text)
            if warn:
                await self._record_external_exchange(text, warn)
                return note_prefix + warn

        # ── Direct API intercepts — handle without LLM round-trip ──────────
        stripped = text.strip().rstrip("()")

        # ── Commands ────────────────────────────────────────────────────────
        # Every command lives in the registry; registration order is dispatch
        # order, so a command answers where the chain this replaced answered it.
        found = command_registry.find(stripped)
        if found is not None:
            handler, argument = found[0].handler, found[1]
            return note_prefix + await handler(CommandContext(actor=self), argument)

        # ── Webhook / notification URL management ───────────────────────────

        # Auto-detect webhook URLs in any message and persist them
        _webhook_match = re.search(
            r"https?://(?:discord\.com/api/webhooks|hooks\.slack\.com|api\.telegram\.org)/\S+", text
        )
        if _webhook_match:
            url = _webhook_match.group(0).rstrip(".,;!)'\"")
            urls = self.recall("_notification_urls") or {}
            if "discord" in url:
                urls["discord"] = url
            elif "slack" in url:
                urls["slack"] = url
            elif "telegram" in url:
                urls["telegram"] = url
            self.persist("_notification_urls", urls)
            logger.info("[%s] Auto-saved webhook URL from message", self.name)

        # ── @mention direct routing ─────────────────────────────────────────
        if text.startswith("@"):
            return note_prefix + await self.delegation.route_mention(text)

        # Explicit planner prefix always wins
        lowered = text.lower()
        if any(lowered.startswith(p) for p in PLANNER_PREFIXES):
            result = await self._run_planner(text)
            response = result or "Planner did not return a result. Please retry."
            await self._record_external_exchange(text, response)
            return note_prefix + response

        # Single LLM call classifies intent: ACTUATE, HA, PIPELINE (reactive rule), OTHER
        intent = await self._classify_intent(text)
        logger.info("[%s] Intent: %s — %s", self.name, intent, text[:60])

        if intent == "PIPELINE":
            response = await self._propose_or_execute_pipeline(text)
            await self._record_external_exchange(text, response)
            return note_prefix + response

        if intent == "ACTUATE":
            response = await self._handle_actuate_intent(text)
            await self._record_external_exchange(text, response)
            return note_prefix + response

        if intent == "HA":
            response = await self.delegation.ask_home_assistant(text)
            await self._record_external_exchange(text, response)
            return note_prefix + response

        # Refresh the system prompt with live registry + facts before any LLM call.
        # This ensures the LLM never answers from a stale view of which agents exist.
        self._rebuild_system_prompt()

        # Belt-and-braces: also inject the live agent list as a prefix on the
        # user message itself. Models trust in-message context over system-prompt
        # claims, so this is the strongest signal we can give without using a tool.
        # We send the prefixed text to the LLM but replace it with the clean
        # original in conversation history afterward — otherwise stale prefixes
        # would accumulate across turns and bloat the context window.
        prefixed_text = self._prefix_with_live_context(text)
        response = await self.chat(prefixed_text)
        # Find the most recent user message in history that matches the prefixed
        # text and replace it with the user's original. The assistant turn after
        # it remains unchanged.
        self._restore_unprefixed_turn(prefixed_text, text)
        raw_llm_response = response

        # A voice transcript classified as OTHER is not authority for the LLM
        # to resurrect a Home Assistant command from history. Correct device
        # commands take the ACTUATE/HA branches above; anything reaching here
        # must be repeated instead of being executed from stale context.
        if self._current_interface_is_voice() and _response_delegates_to(
            response, "home-assistant-agent"
        ):
            safe_reply = "I may have misheard that. Please repeat the device command."
            logger.warning(
                "[%s] Blocked Home Assistant delegation from an OTHER voice turn: %r",
                self.name,
                text[:80],
            )
            self._replace_latest_interface_reply(response, safe_reply)
            return note_prefix + safe_reply

        # If the LLM wrote agent code but forgot the <spawn> wrapper, remind it once
        has_spawn = "<spawn>" in response
        has_code = "async def handle_task" in response or "async def setup" in response
        asked_spawn = any(
            w in text.lower() for w in ("spawn", "create", "make", "build", "add", "agent")
        )
        if has_code and not has_spawn and asked_spawn:
            logger.info("[%s] Code written without <spawn> — prompting to wrap it", self.name)
            response = await self.chat(
                "You wrote agent code but forgot to wrap it in a <spawn> block. "
                "Please output the complete spawn block now with that exact code inside it. "
                "Output ONLY the <spawn>...</spawn> block, nothing else."
            )

        clean, spawned = await self._process_spawn_commands(response)

        # Process any <delete>{"name": "..."}</delete> blocks the LLM produced.
        # This is the orchestrator-side counterpart of <spawn> — lets the LLM
        # remove agents in response to user requests like "delete the math agent".
        clean, deleted, missing = await self._process_delete_commands(clean)

        # Execute structured <delegate>{...}</delegate> blocks — the preferred,
        # unambiguous delegation form. Results are spliced in-place.
        clean, _delegate_results = await self._process_delegate_commands(clean)

        # Execute any looser @agent-name delegation patterns the LLM produced
        clean = await self._execute_llm_delegations(clean)
        self._replace_latest_interface_reply(raw_llm_response, clean)

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "user_interaction", "input": text[:100], "response": clean[:200]},
        )

        summary = TurnActions(tuple(spawned), tuple(deleted), tuple(missing)).summary(response)
        if summary:
            clean += f"\n\n[System: {summary}]"

        return note_prefix + clean

    # Delegation allow-list for social channels. A deny-list won't hold: any
    # DynamicAgent or code-agent runs code, so delegating to one launders code
    # execution. Allow only bounded native agents; fail closed.

    @staticmethod
    def _neutralize_action_blocks(response: str) -> tuple[str, bool]:
        """Strip <spawn>/<delete> blocks without running them; return (cleaned, had_any)."""
        had = bool(re.search(r"<(spawn|delete)>", response))
        cleaned = re.sub(r"<spawn>.*?</spawn>", "", response, flags=re.DOTALL)
        cleaned = re.sub(r"<delete>.*?</delete>", "", cleaned, flags=re.DOTALL)
        return cleaned.strip(), had

    async def process_user_input_restricted(self, text: str) -> str:
        """Social-channel (Discord/Telegram) entry point — the untrusted-surface
        counterpart of process_user_input.

        Conversation, device control (ACTUATE), and HA queries only. Spawning,
        deleting, code, pipelines, admin commands, and delegation to
        non-allowlisted agents are blocked — at the action, not by classifying
        the text, so it can't be talked around.
        """
        note_prefix = self._drain_notifications()
        stripped = text.strip()

        # Slash commands and the bypass markers are the admin surface — not
        # exposed here. Every marker the planner honours is refused, not just
        # the first: a guard that catches one spelling of a family reads as
        # covering the family.
        if stripped.startswith("/") or starts_with_bypass(stripped):
            reply = (
                "Admin commands aren't available on this channel — just talk to me "
                "normally. I can answer questions, tell you what's going on, and "
                "control your devices. (Spawning, deleting, and running code stay "
                "on the dashboard.)"
            )
            await self._record_external_exchange(text, reply)
            return note_prefix + reply

        # Reuse the main intent classifier; PIPELINE (creates rules/agents) is refused.
        intent = await self._classify_intent(text)
        logger.info("[%s] Intent (restricted): %s — %s", self.name, intent, text[:60])

        if intent == "PIPELINE":
            reply = (
                "I can't create automations or new agents from a social channel — "
                "set those up on the dashboard. I can still answer questions and "
                "control your devices from here."
            )
            await self._record_external_exchange(text, reply)
            return note_prefix + reply

        if intent == "ACTUATE":
            # Everyday devices only. The actuator executes the domain/service the
            # LLM picked, so without this gate "control my devices" reaches
            # shell_command/python_script/hassio and becomes code execution.
            response = await self._handle_actuate_intent(
                text, allowed_domains=SOCIAL_ACTUATE_DOMAINS
            )
            await self._record_external_exchange(text, response)
            return note_prefix + response

        if intent == "HA":
            response = await self.delegation.ask_home_assistant(text)
            await self._record_external_exchange(text, response)
            return note_prefix + response

        # OTHER: converse, but run no action executors.
        self._rebuild_system_prompt()
        prefixed_text = self._prefix_with_live_context(text)
        response = await self.chat(prefixed_text)
        self._restore_unprefixed_turn(prefixed_text, text)

        # The prose around a spawn/delete block asserts the action happened, so
        # stripping the block would leave a false claim. Discard the whole reply.
        clean, had_actions = self._neutralize_action_blocks(response)
        if had_actions:
            reply = (
                "I can't create or delete agents from this channel — that's a "
                "dashboard-only action. I'm happy to chat, answer questions, or "
                "control your devices instead."
            )
            await self._record_external_exchange(text, reply)
            return note_prefix + reply

        # Structured delegation only (allow-listed, no spawn); skip loose @mentions.
        clean, _ = await self._process_delegate_commands(clean, restricted=True)
        return note_prefix + clean.strip()

    async def process_user_input_stream(
        self, text: str, attachments: list[dict[str, Any]] | None = None
    ) -> AsyncGenerator[Any]:
        """Streaming version of process_user_input().
        Yields text chunks as the LLM generates them, then a final dict:
          {"done": True, "spawned": [...names...], "system_msg": "..."}

        The CLI calls this and prints chunks immediately.
        REST/Discord/WhatsApp should use process_user_input() instead.

        `attachments` are content blocks for this turn. They reach the model on
        the branch below that calls the LLM; a turn answered without one — a
        command, an actuation, a pipeline plan, a delegation to the Home
        Assistant agent — does not read them.
        """
        # Drain monitor notifications first
        note_prefix = self._drain_notifications()
        if note_prefix:
            yield note_prefix

        # ── Pending-plan response detection (same as non-streaming path) ─────
        if not text.strip().startswith("/") and not text.strip().startswith("@"):
            plan_response = await self._handle_pending_plan_response(text)
            if plan_response is not None:
                await self._record_external_exchange(text, plan_response)
                yield plan_response
                yield {"done": True, "spawned": [], "system_msg": ""}
                return

            # Collision guard — see process_user_input for rationale
            warn = self._warn_if_pending_plan_collision(text)
            if warn:
                await self._record_external_exchange(text, warn)
                yield warn
                yield {"done": True, "spawned": [], "system_msg": ""}
                return

        # All slash-commands and direct API intercepts are handled by process_user_input
        # Route them there to avoid duplicating all that logic here
        _stripped = text.strip().rstrip("()")
        _is_command = _stripped.startswith(("/", "@")) or _stripped in (
            "list_nodes",
            "main.list_nodes",
            "rules",
        )
        if _is_command:
            # /deploy is the one slash command that needs to stream progress
            # messages mid-execution (subnet scan, deploy phases). Other commands
            # go through process_user_input which is request/response.
            if _stripped.startswith("/deploy"):
                async for chunk in self._slash_deploy_stream(_stripped):
                    yield chunk
                yield {"done": True, "spawned": [], "system_msg": ""}
                return
            result = await self.process_user_input(text)
            yield result
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        # Explicit planner prefix always wins
        _lowered = text.lower()
        if any(_lowered.startswith(p) for p in PLANNER_PREFIXES):
            result = await self._run_planner(text)
            response = result or "Planner did not return a result. Please retry."
            await self._record_external_exchange(text, response)
            yield response
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        # Single LLM call classifies intent: ACTUATE, HA, PIPELINE, or OTHER
        intent = await self._classify_intent(text)
        logger.info("[%s] Intent: %s — %s", self.name, intent, text[:60])

        if intent == "PIPELINE":
            response = await self._propose_or_execute_pipeline(text)
            await self._record_external_exchange(text, response)
            yield response
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        if intent == "ACTUATE":
            response = await self._handle_actuate_intent(text)
            await self._record_external_exchange(text, response)
            yield response
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        if intent == "HA":
            response = await self.delegation.ask_home_assistant(text)
            await self._record_external_exchange(text, response)
            yield response
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        # Refresh the system prompt with live registry + facts before any LLM call.
        # This ensures the LLM never answers from a stale view of which agents exist.
        self._rebuild_system_prompt()

        # Belt-and-braces: inject live agent list as a prefix on the user message.
        # Same as the non-streaming path — see _prefix_with_live_context for why.
        prefixed_text = self._prefix_with_live_context(text)

        # Stream the LLM response chunk by chunk
        full_chunks = []
        async for chunk in self.chat_stream(prefixed_text, attachments):
            if isinstance(chunk, dict):
                break  # usage dict — discard, already tracked inside chat_stream
            full_chunks.append(chunk)
            yield chunk

        # Replace the prefixed user message in history with the clean original
        # so future turns aren't polluted with stale prefixes.
        self._restore_unprefixed_turn(prefixed_text, text)

        full_response = "".join(full_chunks)

        if self._current_interface_is_voice() and _response_delegates_to(
            full_response, "home-assistant-agent"
        ):
            safe_reply = "I may have misheard that. Please repeat the device command."
            logger.warning(
                "[%s] Blocked Home Assistant delegation from an OTHER streaming voice turn: %r",
                self.name,
                text[:80],
            )
            self._replace_latest_interface_reply(full_response, safe_reply)
            yield "\n" + safe_reply
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        # Process any <spawn> blocks in the completed response
        _, spawned = await self._process_spawn_commands(full_response)

        # Process any <delete> blocks — orchestrator-side counterpart of <spawn>
        _, deleted, missing = await self._process_delete_commands(full_response)

        # Execute structured <delegate>{...}</delegate> blocks first (preferred
        # form). The raw block was already streamed to the user above, so append
        # each result as an additional chunk — same approach as @mention results.
        full_response, _delegate_results = await self._process_delegate_commands(full_response)
        for _r in _delegate_results:
            yield "\n" + _r

        # Execute any looser @agent-name delegation patterns the LLM produced.
        # If delegations ran, yield the results as an additional chunk.
        delegated = await self._execute_llm_delegations(full_response)
        if delegated != full_response:
            # Find what changed and yield just the new parts
            results = re.findall(r"[✅❌]\s+\S+.*", delegated)
            if results:
                yield "\n" + "\n".join(results)
        full_response = delegated
        self._replace_latest_interface_reply("".join(full_chunks), full_response)

        system_msg = TurnActions(tuple(spawned), tuple(deleted), tuple(missing)).summary(
            full_response
        )

        # Also surface the concrete spawn/delete outcome for stream consumers
        # that ignore the final done dict.
        if system_msg:
            yield f"\n\n_ℹ️ {system_msg}_"

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "user_interaction", "input": text[:100], "response": full_response[:200]},
        )

        yield {"done": True, "spawned": spawned, "system_msg": system_msg}

    # ── Planner ────────────────────────────────────────────────────────────

    def _match_catalog_recipe(self, name: str, capabilities: list[str] | None = None) -> str | None:
        """Return the name of a catalog recipe that already covers this request.

        Used to stop the LLM from reimplementing an agent that the catalog
        already provides (e.g. it writes a fresh 'smart-energy-agent' when the
        catalog has 'smart-energy'). Matching is by normalised name only —
        the safest signal. Capability overlap is intentionally NOT used here
        because it produces false positives that would block genuinely new
        agents.

        Checks two sources so it doesn't depend on manifest-injection timing
        (the catalog injects its recipe manifests into main on startup, but
        that races with main coming up — if it lost the race, the manifests
        wouldn't be here yet). The live catalog actor is the real source of
        truth and is consulted as a fallback.
        """
        want = _normalize_agent_name(name)
        if not want:
            return None

        # 1. Injected manifests (fast path)
        for recipe_name, manifest in self._agent_manifests.items():
            if not (manifest.get("spawnable") and manifest.get("catalog")):
                continue
            if _normalize_agent_name(recipe_name) == want:
                return recipe_name

        # 2. Live catalog actor — authoritative, immune to injection races
        if self._registry:
            cat = self._registry.find_by_name("catalog")
            if cat and hasattr(cat, "list_recipes"):
                try:
                    for recipe_name in cat.list_recipes():  # pyright: ignore[reportAttributeAccessIssue]
                        if _normalize_agent_name(recipe_name) == want:
                            return recipe_name
                except Exception as exc:
                    logger.debug("[%s] Catalog recipe lookup failed: %s", self.name, exc)

        return None

    async def _resolve_or_spawn(self, agent_name: str) -> tuple[Any, bool]:
        return await self.spawns._resolve_or_spawn(agent_name)

    async def _run_delegation(self, agent_name: str, payload: Any) -> str:
        return await self.delegation._run_delegation(agent_name, payload)

    async def _process_delegate_commands(
        self, response: str, restricted: bool = False
    ) -> tuple[str, list[str]]:
        return await self.delegation._process_delegate_commands(response, restricted)

    async def _execute_llm_delegations(self, response: str) -> str:
        return await self.delegation._execute_llm_delegations(response)

    async def _process_spawn_commands(self, response: str) -> tuple[str, list[Any]]:
        return await self.spawns._process_spawn_commands(response)

    async def _process_delete_commands(self, response: str) -> tuple[str, list[str], list[str]]:
        return await self.lifecycle._process_delete_commands(response)

    async def _spawn_from_config(
        self, config: dict[str, Any], save: bool = True, *, from_registry: bool = False
    ) -> Actor | SpawnPlaceholder | None:
        return await self.spawns._spawn_from_config(config, save, from_registry=from_registry)

    def _inject_llm_bridge_code(self, config: dict[str, Any]) -> dict[str, Any]:
        return self.spawns._inject_llm_bridge_code(config)

    async def _spawn_remote(self, config: dict[str, Any], node: str, save: bool) -> None:
        await self.spawns._spawn_remote(config, node, save)

    async def _update_node_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        """Republish what a node should be running. Owned by `self.migration`."""
        await self.migration.update_desired_state(node, new_config, remove_name)

    # ── Node registry ──────────────────────────────────────────────────────

    @property
    def _node_agent_misses(self) -> dict[tuple[str, str], int]:
        """Consecutive heartbeats each agent has been missing, owned by `self.nodes`."""
        return self.nodes.agent_misses

    @_node_agent_misses.setter
    def _node_agent_misses(self, value: dict[tuple[str, str], int]) -> None:
        self.nodes.agent_misses = value

    def _agents_to_prune(
        self, node_name: str, curr_agents: set[str], prev_agents: set[str]
    ) -> list[str]:
        return self.nodes.agents_to_prune(node_name, curr_agents, prev_agents)

    @property
    def _agent_manifests(self) -> dict[str, dict[str, Any]]:
        """Latest manifest per agent, owned by `self.manifests`.

        Kept as a name here because the catalog agent writes recipe manifests
        straight into it, and the chat router, the dynamic agents and the spawn
        mixin all read it off the main actor.
        """
        return self.manifests.manifests

    @_agent_manifests.setter
    def _agent_manifests(self, value: dict[str, dict[str, Any]]) -> None:
        self.manifests.manifests = value

    @property
    def _topic_registry(self) -> dict[str, list[dict[str, Any]]]:
        """Topic to publishing agents, owned by `self.manifests`."""
        return self.manifests.topic_registry

    @_topic_registry.setter
    def _topic_registry(self, value: dict[str, list[dict[str, Any]]]) -> None:
        self.manifests.topic_registry = value

    @property
    def _known_nodes(self) -> dict[str, dict[str, Any]]:
        """The heartbeat table, which `self.nodes` owns.

        Kept as a name on MainActor because several modules read it directly off
        the main actor — the planner, the CLI, the chat router and the dynamic
        agents all walk it to find where an agent is running.
        """
        return self.nodes.known

    @_known_nodes.setter
    def _known_nodes(self, value: dict[str, dict[str, Any]]) -> None:
        self.nodes.known = value

    def list_nodes(self) -> list[dict[str, Any]]:
        """Return all known remote nodes with their last-seen time, running agents, and system metrics."""
        return self.nodes.list_nodes()

    def _node_is_online(self, node_name: str) -> bool:
        """True if ``node_name`` sent a heartbeat inside the freshness window."""
        return self.nodes.is_online(node_name)

    def _online_node_names(self) -> list[str]:
        """Names of all nodes currently considered online."""
        return self.nodes.online_names()

    def list_topics(self, keyword: str = "") -> list[dict[str, Any]]:
        """Return all known MQTT topics published by agents, optionally filtered by keyword.
        Each entry: {"topic": str, "agents": [{"name", "node", "description"}, ...]}

        Example:
            list_topics("cpu")     → topics containing "cpu"
            list_topics("temp")    → topics containing "temp"
            list_topics()          → all topics
        """
        results = []
        kw = keyword.lower()
        for topic, manifests in self._topic_registry.items():
            if kw and kw not in topic.lower():
                continue
            results.append(
                {
                    "topic": topic,
                    "agents": [
                        {
                            "name": m.get("name"),
                            "node": m.get("node"),
                            "description": m.get("description", ""),
                        }
                        for m in manifests
                    ],
                }
            )
        return sorted(results, key=lambda x: x["topic"])

    def list_capabilities(self, keyword: str = "") -> list[dict[str, Any]]:
        """Return all known agents with their full capability profile:
        name, description, capabilities, input_schema, output_schema.

        Includes remote agents — they appear in _agent_manifests via the
        manifest listener (remote agents publish retained manifests just like
        local ones). The `running` flag is True for both local registry actors
        AND agents currently listed in a live node's heartbeat.
        """
        remote_running = self.nodes.running_agents()

        results = []
        kw = keyword.lower().strip()
        kw_words = kw.split() if kw else []
        for name, manifest in self._agent_manifests.items():
            desc = manifest.get("description", "")
            caps = manifest.get("capabilities", [])
            if kw_words:
                haystack = desc.lower() + " " + " ".join(caps).lower() + " " + name.lower()
                if not any(w in haystack for w in kw_words):
                    continue
            local_running = bool(self._registry and self._registry.find_by_name(name))
            results.append(
                {
                    "name": name,
                    "node": manifest.get("node"),
                    "description": desc,
                    "capabilities": caps,
                    "input_schema": manifest.get("input_schema", {}),
                    "output_schema": manifest.get("output_schema", {}),
                    "spawnable": manifest.get("spawnable", False),
                    "running": local_running or name in remote_running,
                    "remote": name in remote_running and not local_running,
                }
            )
        return sorted(results, key=lambda x: x["name"])

    async def _manifest_listener(self) -> None:
        """Follow agent manifests. Owned by `self.manifests`."""
        await self.manifests.manifest_listener()

    async def _state_return_listener(self) -> None:
        """Receive agents returning from a node. Owned by `self.migration`."""
        await self.migration.state_return_listener()

    async def _slash_deploy_stream(self, stripped: str) -> AsyncGenerator[str]:
        """Async generator implementing /deploy. Yields progress strings.

        Accepts one form only::

            /deploy <node>

        where ``<node>`` names a target configured in the environment
        (``DEPLOY_TARGETS`` plus a ``DEPLOY_<NODE>_*`` block). The older
        ``/deploy <node> <host> <user> <password> [broker]`` form is refused:
        the password reached the reply stream and the persisted conversation
        history, and running with no host port-scanned the local /24 for SSH.
        """
        parts = stripped.split()
        if len(parts) < 2:
            names = deploy_target_names()
            listing = "\n".join(f"  {n}" for n in names) or "  (none configured)"
            yield f"[usage] /deploy <node-name>\nConfigured targets:\n{listing}"
            return

        node_name = parts[1]
        if len(parts) > 2:
            # Do not echo parts[2:] — the old form put a live SSH password there
            # and this reply is recorded into conversation history.
            yield (
                "[error] /deploy takes a node name only — host and SSH credentials "
                "now come from the environment, not from chat.\n\n" + deploy_target_help(node_name)
            )
            return

        target = deploy_target(node_name)
        if target is None:
            yield "[error] " + deploy_target_help(node_name)
            return

        host = target.host
        if not host:
            # A single name lookup, not a sweep — it asks about one host and
            # learns nothing about any other machine on the network.
            yield f"[discover] No host configured for '{node_name}' — trying mDNS..."
            try:
                host = await asyncio.to_thread(socket.gethostbyname, f"{node_name}.local")
            except OSError:
                host = ""
            if not host:
                yield (
                    f"[error] Could not resolve '{node_name}.local'.\n"
                    f"Set {deploy_env_prefix(node_name)}_HOST in your environment."
                )
                return
            yield f"[discover] Found via mDNS: {node_name}.local → {host}"

        if not hasattr(self, "delegate_to_installer"):
            yield "[error] Installer agent not available."
            return

        yield (
            f"[deploy] Deploying to {target.user}@{host} as node '{node_name}'...\n"
            f"(This may take 20-60 seconds while packages install on the remote machine)"
        )
        try:
            result = await self.delegate_to_installer(
                {
                    "action": "node_deploy",
                    "host": host,
                    "node_name": target.name,
                    "broker": target.broker or "localhost",
                    "port": target.broker_port,
                },
                timeout=120.0,
            )
        except Exception as exc:
            logger.exception("[main] /deploy failed for node %r", node_name)
            yield f"[FAIL] Deploy failed: {exc}"
            return

        if result.get("success"):
            yield (
                f"[OK] Node '{node_name}' is live! It will appear in /nodes within ~15 seconds.\n\n"
                f"Spawn agents on it:\n"
                f'  "spawn a CPU monitor agent on {node_name}"\n'
                f'  "spawn a temperature sensor on {node_name}"'
            )
        else:
            yield f"[FAIL] Deploy failed: {result.get('error', result)}"

    async def migrate_agent(self, agent_name: str, target_node: str) -> dict[str, Any]:
        """Move a running agent to a different node. Owned by `self.migration`."""
        return await self.migration.migrate_agent(agent_name, target_node)

    async def _node_heartbeat_listener(self) -> None:
        """Follow node heartbeats. Owned by `self.nodes`."""
        await self.nodes.heartbeat_listener()

    async def _node_offline_watcher(self) -> None:
        await self.nodes._node_offline_watcher()

    async def _llm_bridge_listener(self) -> None:
        """Serve LLM calls for remote agents. Owned by `self.llm_bridge`."""
        await self.llm_bridge.listen()

    async def _remote_observed_samples_listener(self) -> None:
        await self.manifests._remote_observed_samples_listener()

    async def delegate_to_installer(
        self, payload: dict[str, Any], timeout: float = 300.0
    ) -> dict[str, Any]:
        return await self.delegation.delegate_to_installer(payload, timeout)

    async def delegate_task(
        self, target_name: str, task: str, timeout: float = 60.0
    ) -> dict[str, Any] | None:
        return await self.delegation.delegate_task(target_name, task, timeout)

    async def list_agents(self) -> list[dict]:
        if not self._registry:
            return []
        return [a.get_status() for a in self._registry.all_actors()]

    async def send_command(self, target_name: str, command: MessageType) -> None:
        if not self._registry:
            return
        target = self._registry.find_by_name(target_name)
        if target:
            await self.send(target.actor_id, command)

    def _node_running_agent(self, name: str) -> str:
        """The live remote node running ``name``, or "" if none claims it.

        Fallback for when the spawn registry does not record the node.
        """
        return self.nodes.running_agent(name)

    async def delete_spawned_agent(self, name: str) -> None:
        await self.lifecycle.delete_spawned_agent(name)

    async def _purge_agent_retained_topics(self, actor_id: str | None) -> None:
        await self.lifecycle._purge_agent_retained_topics(actor_id)

    async def _purge_local_agent_persistence(self, actor: Any, name: str) -> None:
        await self.lifecycle._purge_local_agent_persistence(actor, name)
