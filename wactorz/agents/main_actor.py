"""MainActor - Primary conversational agent and orchestrator.
Spawns DynamicAgents whose core logic is written by the LLM on the fly.
"""

import asyncio
import contextlib
import json
import logging
import re
import shutil
import socket
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any, ClassVar

from ..config import (
    deploy_env_prefix,
    deploy_target,
    deploy_target_help,
    deploy_target_names,
)
from ..core.actor import Actor, Message, MessageType
from ..core.mqtt import mqtt_client
from .commands import CommandContext
from .commands import registry as command_registry
from .helpers.main_actor_helpers import (
    SPAWN_REGISTRY_KEY,
    _normalize_agent_name,
    _parse_spawn_config,
    _strip_live_context,
    starts_with_bypass,
)
from .llm_agent import LLMAgent, LLMProvider
from .llm_bridge import LLMBridge
from .manifests import ManifestRegistry
from .migration import Migration
from .mixins import (
    MemoryMixin,
    PlanningMixin,
    RoutingMixin,
    SpawnMixin,
    SpawnPlaceholder,
)
from .nodes import NodeManager
from .one_off_actuator_agent import SOCIAL_ACTUATE_DOMAINS
from .prompts.main_actor_prompts import (
    ORCHESTRATOR_PROMPT,
)

logger = logging.getLogger(__name__)


def _decoded_json(payload: bytes | None) -> Any | None:
    """The payload decoded as JSON, or None if it is not.

    Used where the subscription is a wildcard over every agent topic and an
    agent may publish whatever it likes: a payload that is not JSON is ordinary
    traffic rather than a fault, and reporting each one would bury the listener
    in its own output.
    """
    if not payload:
        return None
    try:
        return json.loads(payload.decode())
    except Exception:
        return None


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
        # Remote node tracking: node_name → {"last_seen": float, "agents": [...]}
        self.manifests = ManifestRegistry(self)
        self.nodes = NodeManager(self, self.manifests)
        self.migration = Migration(self, self.nodes)
        self.llm_bridge = LLMBridge(self)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self) -> None:
        await super().on_start()
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
        return self.recall(SPAWN_REGISTRY_KEY) or {}

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
        reg = self._get_spawn_registry()
        reg[config["name"]] = config
        self.persist(SPAWN_REGISTRY_KEY, reg)
        logger.info("[%s] Spawn registry: %s", self.name, list(reg.keys()))

    def _remove_from_spawn_registry(self, name: str) -> None:
        reg = self._get_spawn_registry()
        if name in reg:
            del reg[name]
            self.persist(SPAWN_REGISTRY_KEY, reg)
            logger.info("[%s] Removed %r from spawn registry.", self.name, name)

    async def _clear_agent_manifest(self, name: str, actor_id: str | None = None) -> None:
        """Clear an agent's manifest from main's in-memory caches AND from the
        retained MQTT manifest topic. Without this, list_capabilities() will
        keep reporting the agent (with running=false but never disappearing),
        and on next restart it would be re-loaded from the retained message.

        Call this whenever an agent is stopped/deleted/replaced.
        """
        # Drop from in-memory caches immediately
        self._agent_manifests.pop(name, None)
        for topic, entries in list(self._topic_registry.items()):
            self._topic_registry[topic] = [m for m in entries if m.get("name") != name]
            if not self._topic_registry[topic]:
                self._topic_registry.pop(topic, None)
        # Publish empty retained payload to clear the broker-side retained manifest.
        # Need actor_id for the topic — fall back to looking it up from the registry
        # (only works if the actor is still alive — best-effort).
        if not actor_id and self._registry:
            target = self._registry.find_by_name(name)
            if target:
                actor_id = target.actor_id
        if actor_id:
            await self._mqtt_publish(f"agents/{actor_id}/manifest", b"", retain=True)
            logger.debug("[%s] Cleared retained manifest for %r", self.name, name)

    def _record_agent_deletion(self, name: str, reason: str = "user request") -> None:
        """Inject a system-style note into conversation history that an agent was
        deleted. This is critical because the LLM otherwise sees its own earlier
        turn ("Spawned 'chat-agent'") and assumes the agent still exists when
        the user later asks to spawn one with the same name.

        Strengthens the running-agents system prompt block with explicit textual
        evidence inside the message stream — which models weight more heavily
        than system-prompt assertions.
        """
        try:
            note = (
                f"[SYSTEM] Agent '{name}' was deleted ({reason}). "
                f"It is no longer running. If the user asks to spawn an agent "
                f"with this name again, treat it as a fresh spawn — do NOT claim "
                f"it already exists."
            )
            self._conversation_history.append({"role": "user", "content": note})
            self._conversation_history.append(
                {
                    "role": "assistant",
                    "content": f"Acknowledged — '{name}' has been removed from my view.",
                }
            )
            self.persist("conversation_history", self._conversation_history)
            logger.info("[%s] Recorded deletion note for %r in history", self.name, name)
        except Exception as e:
            logger.warning("[%s] Failed to record deletion note: %s", self.name, e)

    def get_notification_urls(self) -> dict[str, Any]:
        """Return persisted notification webhook URLs (discord, telegram, slack, etc.)"""
        return self.recall("_notification_urls") or {}

    # ── User facts ─────────────────────────────────────────────────────────
    # Key facts extracted from conversation: HA URL, entity names, preferences,
    # user name, webhook URLs, etc. Stored separately from history so they
    # survive summarization and persist indefinitely.

    async def _restore_spawned_agents(self) -> None:
        reg = self._get_spawn_registry()
        if not reg:
            return

        # ── Skip names already brought up by the Supervisor's factories ─────
        # Both the Supervisor (registry.py) and this method spawn user agents
        # at startup. Without this guard they race: supervisor.start() spawns
        # instance #1 via its stored factory, then on_start() runs us here and
        # we spawn instance #2. Both register under the same deterministic
        # actor_id (uuid5 of name) — the dict entry gets overwritten but the
        # first instance's aiomqtt subscribe listeners keep running, causing
        # every MQTT message to be delivered twice.
        sup = getattr(self._registry, "_supervisor_ref", None) if self._registry else None
        already_supervised: set[str] = set()
        if sup is not None:
            for sup_name, spec in sup._specs.items():
                if spec.actor is not None and not spec.retired:
                    already_supervised.add(sup_name)

        if already_supervised:
            skip = sorted(n for n in reg if n in already_supervised)
            if skip:
                logger.info(
                    "[%s] Supervisor already restarted %s agent(s); skipping restore for: %s",
                    self.name,
                    len(skip),
                    skip,
                )

        pending = {n: c for n, c in reg.items() if n not in already_supervised}
        if not pending:
            return

        logger.info("[%s] Restoring %s agent(s): %s", self.name, len(pending), list(pending.keys()))
        for name, config in pending.items():
            node = config.get("node", "").strip()
            if node:
                # Remote agent — re-publish spawn to its node; no local object expected
                logger.info("[%s] Re-spawning remote agent %r on node %r", self.name, name, node)
                try:
                    await self._spawn_remote(config, node, save=False)
                except Exception:
                    logger.exception(
                        "[%s] Failed to restore remote %r on %r", self.name, name, node
                    )
                continue
            if self._registry and self._registry.find_by_name(name):
                logger.info("[%s] %r already running, skipping.", self.name, name)
                continue
            try:
                await self._spawn_from_config(config, save=False, from_registry=True)
                logger.info("[%s] Restored: %s", self.name, name)
            except Exception:
                logger.exception("[%s] Failed to restore %r", self.name, name)

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

        # ── /help ───────────────────────────────────────────────────────────
        # Commands that have moved to the registry are found here; the chain
        # below still holds the rest. Registration order is dispatch order, so a
        # command answers exactly where it did before.
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

        # ── /delete <agent>, /stop <agent> — direct shortcuts ──────────────
        # Same behaviour as `/agents delete <name>` / `/agents stop <name>`,
        # but as a top-level command so users (and main itself) don't need to
        # round-trip through the LLM. Reuses the unified handler below by
        # rewriting `stripped` and falling through.

        # ── /migrate <agent> <node> ─────────────────────────────────────────
        # Moved here from io_agent so all interfaces (CLI, UI, Discord) share
        # one implementation. The actual work is done by self.migrate_agent().

        # ── /deploy (non-streaming path) ────────────────────────────────────
        # The streaming version yields progress chunks live; this version
        # collects them and returns one joined string. Callers without
        # streaming (Discord, REST, CLI input()) get the full transcript at
        # the end. Implementation lives in _slash_deploy_stream so there is
        # exactly one source of truth for what /deploy does.

        # ── /clear-plans ────────────────────────────────────────────────────

        # ── /pause <agent>, /resume <agent> ─────────────────────────────────
        # NOTE: the underlying remote_runner.py does not implement pause/resume
        # topics, so these only affect LOCAL agents. For remote agents we tell
        # the user honestly and suggest /stop instead.

        # ── /agents stop|delete|pause|remove <name> ─────────────────────────
        # NOTE: stop and pause are reversible (state preserved, spawn registry
        # entry kept if you ever want to /agents restart). delete and remove
        # are PERMANENT — they go through delete_spawned_agent(), which wipes
        # the on-disk state file, purges retained MQTT topics, and removes the
        # entry from the spawn registry. Picking the wrong verb here is the
        # difference between "the agent comes back on next runner restart" and
        # "the agent is gone forever".

        # ── /agents restart <name> ──────────────────────────────────────────

        # ── /nodes remove <node> ────────────────────────────────────────────

        # ── /nodes restart <node> ───────────────────────────────────────────

        # ── /nodes shutdown <node> ──────────────────────────────────────────

        # ── /agents / /capabilities ─────────────────────────────────────────

        # ── /registry — diagnostic: compare all three sources of truth ──────

        # ── /plans — pending dry-run proposals ──────────────────────────────

        # ── @mention direct routing ─────────────────────────────────────────
        if text.startswith("@"):
            # Extract agent name and message: "@cpu-monitor-rpi-room what is the cpu?"
            parts = text.split(None, 1)
            target_name = parts[0].lstrip("@").rstrip(":,")
            message = parts[1].strip() if len(parts) > 1 else text

            # Try local registry first
            local_target = self._registry.find_by_name(target_name) if self._registry else None
            if not local_target:
                # Not running — check if it's a spawnable catalog recipe
                manifest = self._agent_manifests.get(target_name, {})
                if manifest.get("spawnable") and manifest.get("catalog"):
                    catalog_name = manifest["catalog"]
                    catalog_actor = (
                        self._registry.find_by_name(catalog_name) if self._registry else None
                    )
                    if catalog_actor and hasattr(catalog_actor, "_action_spawn"):
                        logger.info(
                            "[main] %r not running — auto-spawning via %s...",
                            target_name,
                            catalog_name,
                        )
                        try:
                            spawn_result = await catalog_actor._action_spawn(target_name, {})  # pyright: ignore[reportAttributeAccessIssue]
                            if spawn_result and spawn_result.get("ok"):
                                await asyncio.sleep(0.5)
                                local_target = (
                                    self._registry.find_by_name(target_name)
                                    if self._registry
                                    else None
                                )
                                logger.info("[main] %r spawned, routing task...", target_name)
                            else:
                                err = (
                                    spawn_result.get("message", "unknown error")
                                    if spawn_result
                                    else "no response"
                                )
                                return note_prefix + f"Could not spawn '{target_name}': {err}"
                        except Exception as e:
                            return note_prefix + f"Could not spawn '{target_name}': {e}"

            if local_target:
                result = await self.delegate_task(target_name, message, timeout=60.0)
                if result:
                    reply = result.get("result") or result.get("response") or str(result)
                    return note_prefix + f"**{target_name}**: {reply}"
                return note_prefix + f"{target_name} did not respond."

            # Check if it's a known remote agent
            remote_node = None
            for node_name, nd in self._known_nodes.items():
                if target_name in nd.get("agents", []):
                    remote_node = node_name
                    break

            if remote_node:
                # Send via MQTT and wait for reply
                async with self._reply_topic() as (reply_topic, future):
                    await self._mqtt_publish(
                        f"agents/by-name/{target_name}/task",
                        {
                            "text": message,
                            "_reply_topic": reply_topic,
                            "_remote_task": True,
                            "payload": message,
                        },
                    )
                    try:
                        result = await asyncio.wait_for(asyncio.shield(future), timeout=30.0)
                        reply = result.get("result") or result.get("response") or str(result)
                        return note_prefix + f"**{target_name}** (on {remote_node}): {reply}"
                    except asyncio.TimeoutError:
                        return (
                            note_prefix
                            + f"{target_name} on {remote_node} did not respond within 30s."
                        )

            # Not found locally or remotely
            known_remote = [a for nd in self._known_nodes.values() for a in nd.get("agents", [])]
            if known_remote:
                return note_prefix + (
                    f"Agent '{target_name}' not found. Remote agents: {', '.join(known_remote)}"
                )
            return note_prefix + f"Agent '{target_name}' not found."

        # Explicit planner prefix always wins
        lowered = text.lower()
        if any(
            lowered.startswith(p)
            for p in (
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
        ):
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
            result = await self.delegate_task("home-assistant-agent", text, timeout=120.0)
            if result and isinstance(result, dict) and result.get("result"):
                response = str(result["result"])
            elif not result:
                response = "I could not reach the Home Assistant agent right now. Please retry."
            else:
                response = "The Home Assistant agent did not return a result. Please retry."
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
        for i in range(len(self._conversation_history) - 1, -1, -1):
            m = self._conversation_history[i]
            if m.get("role") == "user" and m.get("content") == prefixed_text:
                m["content"] = text
                break
        self.persist("conversation_history", self._conversation_history)

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

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "user_interaction", "input": text[:100], "response": clean[:200]},
        )

        # Build a system footer summarizing spawn/delete actions
        footer_parts = []
        if spawned:
            bg_names = [a.name for a in spawned if isinstance(a, SpawnPlaceholder)]
            live_names = [a.name for a in spawned if not isinstance(a, SpawnPlaceholder)]
            if live_names:
                replaced = '"replace": true' in response or '"replace":true' in response
                action = "Replaced" if replaced else "Spawned"
                footer_parts.append(
                    f"{action} {', '.join(live_names)} — will auto-restore on restart"
                )
            if bg_names:
                footer_parts.append(
                    f"Installing packages for {', '.join(bg_names)} — will appear shortly"
                )
        if deleted:
            footer_parts.append(f"Deleted {', '.join(deleted)}")
        if missing:
            footer_parts.append(f"Could not delete {', '.join(missing)} — not currently registered")
        if footer_parts:
            clean += f"\n\n[System: {' | '.join(footer_parts)}]"

        return note_prefix + clean

    # Delegation allow-list for social channels. A deny-list won't hold: any
    # DynamicAgent or code-agent runs code, so delegating to one launders code
    # execution. Allow only bounded native agents; fail closed.
    _RESTRICTED_DELEGATION_ALLOW = frozenset({"weather-agent", "home-assistant-agent"})

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
            result = await self.delegate_task("home-assistant-agent", text, timeout=120.0)
            if result and isinstance(result, dict) and result.get("result"):
                response = str(result["result"])
            elif not result:
                response = "I could not reach the Home Assistant agent right now. Please retry."
            else:
                response = "The Home Assistant agent did not return a result. Please retry."
            await self._record_external_exchange(text, response)
            return note_prefix + response

        # OTHER: converse, but run no action executors.
        self._rebuild_system_prompt()
        prefixed_text = self._prefix_with_live_context(text)
        response = await self.chat(prefixed_text)
        for i in range(len(self._conversation_history) - 1, -1, -1):
            m = self._conversation_history[i]
            if m.get("role") == "user" and m.get("content") == prefixed_text:
                m["content"] = text
                break
        self.persist("conversation_history", self._conversation_history)

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
        if any(
            _lowered.startswith(p)
            for p in (
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
        ):
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
            result = await self.delegate_task("home-assistant-agent", text, timeout=120.0)
            if result and isinstance(result, dict) and result.get("result"):
                response = str(result["result"])
            elif not result:
                response = "I could not reach the Home Assistant agent right now. Please retry."
            else:
                response = "The Home Assistant agent did not return a result. Please retry."
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
        for i in range(len(self._conversation_history) - 1, -1, -1):
            m = self._conversation_history[i]
            if m.get("role") == "user" and m.get("content") == prefixed_text:
                m["content"] = text
                break
        self.persist("conversation_history", self._conversation_history)

        full_response = "".join(full_chunks)

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

        system_msg_parts = []
        if spawned:
            names = ", ".join(f"'{a.name}'" for a in spawned if not isinstance(a, SpawnPlaceholder))
            bg_names = [a.name for a in spawned if isinstance(a, SpawnPlaceholder)]
            if names:
                replaced = '"replace": true' in full_response or '"replace":true' in full_response
                system_msg_parts.append(
                    f"{'Replaced' if replaced else 'Spawned'} {names} — will auto-restore on restart"
                )
            if bg_names:
                system_msg_parts.append(
                    f"Installing packages for {', '.join(bg_names)} — will appear shortly"
                )
        if deleted:
            system_msg_parts.append(f"Deleted {', '.join(deleted)}")
        if missing:
            system_msg_parts.append(
                f"Could not delete {', '.join(missing)} — not currently registered"
            )
        system_msg = " | ".join(system_msg_parts)

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
        """Resolve an agent by name, auto-spawning it from a catalog recipe if it
        isn't running yet. Shared by @mention delegations and <delegate> blocks.

        Returns (target_actor_or_None, spawnable_bool).
        """
        target = self._registry.find_by_name(agent_name) if self._registry else None
        spawnable = False
        if not target:
            manifest = self._agent_manifests.get(agent_name, {})
            # Fall back to a fuzzy catalog match so 'smart energy agent' or
            # 'smart-energy-agent' still resolve to the 'smart-energy' recipe.
            if not manifest:
                matched = self._match_catalog_recipe(agent_name)
                if matched:
                    agent_name = matched
                    manifest = self._agent_manifests.get(matched, {})
            spawnable = bool(manifest.get("spawnable") and manifest.get("catalog"))
            if spawnable:
                catalog_actor = (
                    self._registry.find_by_name(manifest["catalog"]) if self._registry else None
                )
                if catalog_actor and hasattr(catalog_actor, "_action_spawn"):
                    logger.info("[%s] Auto-spawning %r via catalog...", self.name, agent_name)
                    try:
                        spawn_result = await catalog_actor._action_spawn(agent_name, {})  # pyright: ignore[reportAttributeAccessIssue]
                        if spawn_result and spawn_result.get("ok"):
                            await asyncio.sleep(0.5)
                            target = (
                                self._registry.find_by_name(agent_name) if self._registry else None
                            )
                            logger.info("[%s] %r spawned successfully", self.name, agent_name)
                        else:
                            err = (
                                spawn_result.get("message", "unknown")
                                if spawn_result
                                else "no response"
                            )
                            logger.warning(
                                "[%s] Spawn failed for %r: %s", self.name, agent_name, err
                            )
                    except Exception:
                        logger.exception("[%s] Spawn error for %r", self.name, agent_name)
        return target, spawnable

    async def _run_delegation(self, agent_name: str, payload: Any) -> str:
        """Dispatch one already-resolved delegation and format the result string.
        Shared by @mention delegations and <delegate> blocks.
        """
        json_str = json.dumps(payload)
        logger.info("[%s] Executing LLM delegation → @%s %s", self.name, agent_name, json_str[:80])
        try:
            result = await self.delegate_task(agent_name, json_str, timeout=300.0)
        except Exception as e:
            return f"[{agent_name} error: {e}]"
        # Formatting the reply is outside the guard on purpose: only the
        # delegation can fail, and reporting a formatting bug as an agent error
        # would send the reader looking at the wrong machine.
        if not result:
            return f"[{agent_name} did not respond]"
        if not isinstance(result, dict):
            return f"✅ {agent_name}: {result}"
        error = result.get("error")
        if error:
            return f"❌ {agent_name} failed: {error}"
        for key in ("pptx_path", "image_path", "result", "message", "output", "text"):
            if result.get(key):
                return f"✅ {agent_name} completed: {key}={result[key]}"
        return f"✅ {agent_name} completed: {result}"

    async def _process_delegate_commands(
        self, response: str, restricted: bool = False
    ) -> tuple[str, list[str]]:
        """Scan the LLM response for structured delegation blocks and execute them:

            <delegate>{"agent": "manual-agent", "task": "search for the Philips 2200 manual"}</delegate>
            <delegate>{"agent": "weather-agent", "payload": {"city": "Athens"}}</delegate>

        This is the orchestrator-side counterpart of <spawn>/<delete> and the
        PREFERRED way for the LLM to delegate: unlike a free-text @mention it is
        unambiguous, never truncated, and carries a structured payload.

        Accepted block fields:
          - "agent"  (required) — target agent name
          - "task"   — free-text task; wrapped as {"text": task}
          - "payload"— explicit dict payload (takes precedence over "task")

        Returns (cleaned_response, [result_strings]). The <delegate> block is
        replaced in-place with its result so the user sees the outcome.
        """
        pattern = r"<delegate>(.*?)</delegate>"
        results: list[str] = []

        matches = list(re.finditer(pattern, response, re.DOTALL))
        for m in matches:
            block = m.group(1).strip()
            try:
                cfg = json.loads(block)
            except json.JSONDecodeError:
                logger.exception(
                    "[%s] Invalid <delegate> JSON\nRaw block: %s", self.name, block[:200]
                )
                response = response.replace(m.group(0), "[delegate: malformed block]")
                continue

            agent_name = (cfg.get("agent") or cfg.get("name") or "").strip()
            if not agent_name:
                logger.warning("[%s] <delegate> block missing 'agent': %s", self.name, block[:200])
                response = response.replace(m.group(0), "[delegate: no agent named]")
                continue
            if agent_name == self.name:
                response = response.replace(m.group(0), "")
                continue

            if isinstance(cfg.get("payload"), dict):
                payload = cfg["payload"]
            else:
                payload = {"text": str(cfg.get("task", "")).strip()}

            if restricted:
                # Resolve-only (no spawn), allow-listed targets only.
                if agent_name.lower() not in self._RESTRICTED_DELEGATION_ALLOW:
                    result_str = f"[{agent_name} isn't available from this channel]"
                    results.append(result_str)
                    response = response.replace(m.group(0), result_str)
                    continue
                target = self._registry.find_by_name(agent_name) if self._registry else None
            else:
                target, _spawnable = await self._resolve_or_spawn(agent_name)
            if not target:
                result_str = f"[Could not reach {agent_name}]"
            else:
                # Use the resolved actor's real name so delegation lands even
                # when the LLM used a fuzzy alias ('smart energy agent').
                result_str = await self._run_delegation(target.name, payload)

            results.append(result_str)
            response = response.replace(m.group(0), result_str)

        return response, results

    async def _execute_llm_delegations(self, response: str) -> str:
        """Scan the LLM response for @agent-name delegation patterns and execute them.
        Replaces the matched pattern in the response with the actual result.

        Prefer <delegate>{...}</delegate> blocks (see _process_delegate_commands).
        This handles the looser @mention forms the LLM still produces:

          1. Explicit JSON payload, anywhere in the text (original behavior — the
             form that already worked):
                 @weather-agent {"city": "Athens"}

          2. Bare conversational mention at the start of a line OR a sentence:
                 ...sending that now.  @manual-agent search for the Philips 2200 manual.
             The text from the mention up to the next sentence terminator / newline
             is wrapped as {"text": "..."}.

        Previously only form (1) was recognized, so a bare mention matched nothing,
        was streamed to the user verbatim, and was never dispatched — which is why
        nothing showed up in the logs.
        """
        # (full_match_str, agent_name, payload_dict, is_bare)
        delegations = []
        json_spans = []  # char spans consumed by form (1), so form (2) skips them

        # ── Form 1: @agent-name {json} — ANYWHERE. Brace-scan the matching close
        #    over the full response (payload may span lines / contain } in strings).
        for m in re.finditer(r"@([\w][\w\-]*)\s+(\{)", response):
            agent_name = m.group(1)
            if agent_name == self.name:
                continue
            start = m.start(2)
            depth = 0
            end = start
            for i, ch in enumerate(response[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if depth != 0:
                continue  # unmatched braces — skip
            try:
                payload = json.loads(response[start:end])
            except json.JSONDecodeError:
                continue
            delegations.append((response[m.start() : end], agent_name, payload, False))
            json_spans.append((m.start(), end))

        # ── Form 2: bare mention at a line OR sentence boundary. The '@' must be
        #    preceded by start-of-text, a newline, or sentence-ending punctuation,
        #    so an @name buried mid-sentence ("you can use @x to ...") — which the
        #    prompt treats as the WRONG, user-instructing pattern — is ignored.
        trigger = re.compile(r"(?:^|\n|[.!?:]\s+)(@([\w][\w\-]*)[ \t]+)")
        for m in trigger.finditer(response):
            agent_name = m.group(2)
            if agent_name == self.name:
                continue
            at_start = m.start(1)  # index of the '@'
            payload_start = m.end(1)  # first char after "@name "
            if any(s <= at_start < e for s, e in json_spans):
                continue  # already captured as the JSON form
            if payload_start < len(response) and response[payload_start] == "{":
                continue  # JSON form — owned by form (1)
            tail = response[payload_start:]
            stop = re.search(r"[.!?\n]", tail)
            cut = stop.start() if stop else len(tail)
            task = tail[:cut].strip()
            if not task:
                continue
            end = payload_start + cut
            delegations.append((response[at_start:end], agent_name, {"text": task}, True))

        replacements = []
        for full_match, agent_name, payload, is_bare in delegations:
            target, spawnable = await self._resolve_or_spawn(agent_name)
            if not target:
                # A bare prose mention of an unknown, non-spawnable name is almost
                # certainly ordinary text (or an agent the LLM only imagined), so
                # leave the line untouched rather than clobbering the reply. The
                # explicit JSON form is unambiguous machine intent — surface it.
                if is_bare and not spawnable:
                    logger.debug(
                        "[%s] Ignoring bare mention of unknown agent %r", self.name, agent_name
                    )
                    continue
                replacements.append((full_match, f"[Could not reach {agent_name}]"))
                continue
            result_str = await self._run_delegation(target.name, payload)
            replacements.append((full_match, result_str))

        for original, replacement in replacements:
            response = response.replace(original, replacement)

        return response

    async def _process_spawn_commands(self, response: str) -> tuple[str, list[Any]]:
        spawned = []
        pattern = r"<spawn>(.*?)</spawn>"

        for match in re.findall(pattern, response, re.DOTALL):
            try:
                config = _parse_spawn_config(match.strip())

                # ── Guard: don't let the LLM reimplement a catalog recipe ──────
                # If the requested name maps to an existing catalog recipe, spawn
                # THAT (the maintained, tested version) instead of the LLM's
                # freshly-written duplicate. This is what stops "spawn the smart
                # energy agent" from producing a brand-new 'smart-energy-agent'
                # when the catalog already provides 'smart-energy'.
                req_name = config.get("name", "")
                if not config.get("replace"):
                    recipe_name = self._match_catalog_recipe(req_name)
                    # Log catalog recipe resolution for spawn-routing diagnostics.
                    logger.info(
                        "[%s] spawn %r: catalog-dedup match = %r", self.name, req_name, recipe_name
                    )
                    if recipe_name:
                        existing = (
                            self._registry.find_by_name(recipe_name) if self._registry else None
                        )
                        if existing:
                            logger.info(
                                "[%s] %r already covered by running catalog agent %r — skipping LLM reimplementation",
                                self.name,
                                req_name,
                                recipe_name,
                            )
                            spawned.append(existing)
                            continue
                        catalog_actor = (
                            self._registry.find_by_name("catalog") if self._registry else None
                        )
                        if catalog_actor and hasattr(catalog_actor, "_action_spawn"):
                            logger.info(
                                "[%s] LLM tried to write %r; spawning catalog recipe %r instead",
                                self.name,
                                req_name,
                                recipe_name,
                            )
                            spawn_result = await catalog_actor._action_spawn(recipe_name, {})  # pyright: ignore[reportAttributeAccessIssue]
                            if spawn_result and spawn_result.get("ok"):
                                await asyncio.sleep(0.5)
                                actor = (
                                    self._registry.find_by_name(recipe_name)
                                    if self._registry
                                    else None
                                )
                                if actor:
                                    spawned.append(actor)
                                    continue
                            logger.warning(
                                "[%s] Catalog spawn of %r failed (%s); falling back to LLM code",
                                self.name,
                                recipe_name,
                                spawn_result,
                            )

                # LLM agents have no "code" — only check for code if type is dynamic
                agent_type = config.get("type", "dynamic")
                has_code = bool(config.get("code", "").strip())
                has_prompt = bool(config.get("system_prompt", "").strip())
                if agent_type == "dynamic" and not has_code:
                    logger.error(
                        "[%s] Dynamic agent has no code: %s", self.name, config.get("name")
                    )
                    continue
                if agent_type == "llm" and not has_prompt:
                    logger.warning(
                        "[%s] LLM agent has no system_prompt, using default: %s",
                        self.name,
                        config.get("name"),
                    )
                actor = await self._spawn_from_config(config, save=True)
                if actor:
                    spawned.append(actor)
            except Exception:
                logger.exception("[%s] Spawn failed\nRaw block:\n%s", self.name, match[:500])

        clean = re.sub(pattern, "", response, flags=re.DOTALL).strip()
        return clean, spawned

    async def _process_delete_commands(self, response: str) -> tuple[str, list[str], list[str]]:
        """Scan the LLM response for <delete>{"name": "agent-name"}</delete> blocks
        and execute them. Mirrors _process_spawn_commands so deletion has the same
        UX as spawn: the LLM emits a tagged block, we parse and execute, and the
        block is stripped from the user-visible response.

        Returns (cleaned_response, [deleted_names], [missing_names]):
          - cleaned_response: response with <delete> blocks removed
          - deleted_names:    names that were actually running and got removed
          - missing_names:    names the LLM asked to delete that didn't exist

        We track the missing list separately so the response footer can tell the
        user "you asked me to delete X but it wasn't running" instead of silently
        dropping the request.
        """
        pattern = r"<delete>(.*?)</delete>"
        deleted: list[str] = []
        missing: list[str] = []

        # Build the set of currently-known agent names ONCE up front, so a delete
        # block that lists a name we then delete doesn't accidentally appear as
        # "missing" if a later block references the same name.
        known_names = set(self._agent_manifests.keys())
        if self._registry:
            known_names |= {a.name for a in self._registry.all_actors()}
        # Spawn registry is the strongest signal — if it's persisted there, deletion
        # is meaningful even if the live actor isn't currently up.
        known_names |= set(self._get_spawn_registry().keys())

        # Names main itself never deletes (housekeeping/system actors).
        protected = {
            "main",
            "monitor",
            "installer",
            "home-assistant-agent",
            "anomaly-detector",
            "code-agent",
            "catalog",
        }

        for match in re.findall(pattern, response, re.DOTALL):
            block = match.strip()
            try:
                # Accept either a JSON object {"name": "x"} or a bare string "x"
                # so the LLM has a forgiving format.
                name: str | None = None
                stripped = block.strip()
                if stripped.startswith("{"):
                    payload = json.loads(stripped)
                    if isinstance(payload, dict):
                        name = payload.get("name") or payload.get("agent")
                else:
                    # Bare token form: <delete>math-agent</delete>
                    name = stripped.strip("\"'").split()[0] if stripped else None
                if not name or not isinstance(name, str):
                    logger.warning(
                        "[%s] Empty or malformed <delete> block: %s", self.name, block[:200]
                    )
                    continue
                name = name.strip()

                if name in protected:
                    logger.warning("[%s] Refused to delete protected agent %r", self.name, name)
                    continue

                if name not in known_names:
                    logger.info("[%s] LLM requested deletion of unknown agent %r", self.name, name)
                    missing.append(name)
                    continue

                logger.info("[%s] LLM-requested deletion of %r", self.name, name)
                # Reuse the existing helper — it handles spawn registry, stop,
                # manifest cleanup, history note, and remote-vs-local routing.
                await self.delete_spawned_agent(name)
                deleted.append(name)
            except json.JSONDecodeError:
                logger.exception(
                    "[%s] Invalid <delete> JSON\nRaw block: %s", self.name, block[:200]
                )
            except Exception:
                logger.exception("[%s] Delete failed\nRaw block:\n%s", self.name, block[:500])

        clean = re.sub(pattern, "", response, flags=re.DOTALL).strip()
        return clean, deleted, missing

    async def _spawn_from_config(
        self, config: dict[str, Any], save: bool = True, *, from_registry: bool = False
    ) -> Actor | SpawnPlaceholder | None:
        """Spawn one agent from a config dict.

        Remote (node-targeted) spawns go out over MQTT via ``_spawn_remote``;
        everything local is handled by the shared ``SpawnMixin`` so main and the
        planner construct agents identically.

        ``from_registry`` says the config was read back from the spawn registry,
        which is what entitles it to a ``trusted`` flag. It defaults to False so
        a new call site is untrusted until someone decides otherwise.
        """
        node = config.get("node", "").strip()
        if node:
            return await self._spawn_remote(config, node, save)
        return await self._spawn_local_from_config(
            config, register=save, from_registry=from_registry
        )

    # Synthetic bridge code shipped with type:"llm" agents when they're spawned
    # on a remote node. The runner compiles this and finds handle_task, so the
    # agent can actually respond to tasks instead of returning the "no
    # handle_task function" error.
    #
    # Design points:
    #   - All persistence is via agent.persist / agent.recall (these write to
    #     the remote node's JSON state file under conversation_history).
    #     conversation_history shipped via _initial_state from main is picked
    #     up by recall() on first use, so memory survives migrate-back too.
    #   - LLM calls go through agent.chat(messages, system=...) — on the remote
    #     side this is _RemoteAgentAPI.chat, which RPCs back to main via the
    #     main/llm_request bridge. No API key leaves main.
    #   - System prompt is interpolated as a string literal at synthesis time
    #     so the agent code doesn't need to look it up from config at runtime.
    #     We use json.dumps to safely escape quotes / newlines / backslashes.
    #   - History gets bounded at max_history turns (32 by default, matches
    #     LLMAgent's default) so the prompt doesn't grow without bound.
    _LLM_BRIDGE_CODE_TEMPLATE = """
# ── Auto-generated LLM bridge (synthesized by main when this agent was spawned
# remotely with type: "llm"). Do not edit; replace the agent if you need to
# change behavior. ─────────────────────────────────────────────────────────
import time

_SYSTEM_PROMPT = {system_prompt_literal}
_MAX_HISTORY   = {max_history}


def _now_block():
    # Recomputed every task so the agent always sees the real present moment
    # (process-local zone — honors the host TZ env var). Kept self-contained so
    # the synthesized agent has no import dependency on llm_agent. Newlines are
    # built with chr(10) to avoid backslash-escaping through the code template.
    from datetime import datetime as _dt
    _n = _dt.now().astimezone()
    _nl = chr(10)
    return (
        "== CURRENT DATE & TIME (live) ==" + _nl
        + "It is now " + _n.strftime("%A, %d %B %Y, %H:%M %Z")
        + " (UTC" + _n.strftime("%z") + ")." + _nl
        + "Trust this over training data; resolve 'today'/'tomorrow' against it."
        + _nl + _nl
    )

async def setup(agent):
    # Conversation history may have been shipped via _initial_state at spawn
    # time — agent.recall() finds it. If this is a fresh spawn, default to
    # an empty list.
    agent.state["history"] = list(agent.recall("conversation_history", []) or [])

async def handle_task(agent, payload):
    # Accept the same shapes LLMAgent does: dict with text/task/message/query,
    # or a bare string. Anything else gets str()'d so the LLM at least gets
    # something to look at instead of crashing on a non-string.
    if isinstance(payload, dict):
        text = (
            payload.get("text")
            or payload.get("task")
            or payload.get("message")
            or payload.get("query")
            or str(payload)
        )
    else:
        text = str(payload) if payload is not None else ""

    started = time.time()
    history = agent.state.setdefault("history", [])
    history.append({{"role": "user", "content": text, "ts": started}})

    # Keep the prompt bounded — only send the last _MAX_HISTORY turns to the LLM.
    safe_history = [
        {{"role": m["role"], "content": str(m["content"])}}
        for m in history[-_MAX_HISTORY:]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ]

    response = await agent.chat(safe_history, system=_now_block() + _SYSTEM_PROMPT)
    duration = time.time() - started

    history.append({{"role": "assistant", "content": response, "ts": time.time()}})
    # Trim in-memory list too so it doesn't grow forever between persists.
    if len(history) > _MAX_HISTORY * 2:
        del history[: len(history) - _MAX_HISTORY * 2]
    agent.persist("conversation_history", history)

    return {{"text": response, "task": text[:60], "duration": duration}}
"""

    def _inject_llm_bridge_code(self, config: dict[str, Any]) -> dict[str, Any]:
        """If ``config`` is for an LLM-typed agent without code, return a copy
        with synthesized bridge code so the remote runner can actually run it.

        No-op (returns the original config) when:
          - the config already has code (caller provided their own logic)
          - the config isn't type "llm" (DynamicAgent, ha_actuator, etc. are
            handled directly by the runner already)

        Why a copy: callers may pass the same config dict for multiple writes
        (spawn registry, desired_state, MQTT publish) and we don't want to
        mutate it under their feet.
        """
        if not isinstance(config, dict):
            return config
        if (config.get("code") or "").strip():
            return config
        if (config.get("type") or "").strip().lower() != "llm":
            return config

        system_prompt = config.get("system_prompt", "You are a helpful assistant.")
        max_history = int(config.get("max_history", 32) or 32)
        bridge = self._LLM_BRIDGE_CODE_TEMPLATE.format(
            system_prompt_literal=json.dumps(system_prompt),
            max_history=max_history,
        )

        out = dict(config)
        out["code"] = bridge
        # description and capabilities are commonly already set by the LLM
        # at spawn time; fill in sensible defaults if not so list_capabilities
        # still works on the remote side.
        out.setdefault(
            "description",
            f"LLM-driven agent (remote bridge). System prompt: "
            f"{system_prompt[:80].rstrip()}{'...' if len(system_prompt) > 80 else ''}",
        )
        out.setdefault("input_schema", {"text": "str — the question or request"})
        out.setdefault("output_schema", {"text": "str — the LLM response"})
        logger.info(
            "[%s] Synthesized LLM bridge code for %r: %s chars, max_history=%s",
            self.name,
            out.get("name"),
            len(bridge),
            max_history,
        )
        return out

    async def _spawn_remote(self, config: dict[str, Any], node: str, save: bool) -> None:
        """Publish a spawn command to a remote node via MQTT.
        The remote_runner.py on that machine will receive it and run the agent.
        Remote agents appear in the dashboard exactly like local ones
        because they connect to the same MQTT broker.

        Also updates nodes/{node}/desired_state (retained) with ALL agents for
        this node so the runner can self-heal after a reboot.

        If the spawn config has an 'install' list, packages are installed on the
        remote node via SSH BEFORE the agent is spawned — so setup() won't fail
        with 'No module named X'.
        """
        # For LLM-type agents we ship a synthesized bridge code field over MQTT
        # (the remote runner only knows how to run DynamicAgent-shaped code).
        # We keep the registry-saved config CLEAN — just system_prompt, no code
        # — so the bridge is re-synthesized fresh on every spawn / restart /
        # migrate. This avoids carrying the bridge as dead baggage into local
        # spawn configs on migrate-back, and keeps the spawn registry small.
        wire_config = self._inject_llm_bridge_code(config)

        name = wire_config.get("name", "remote-agent")
        packages = wire_config.get("install", [])
        if isinstance(packages, str):
            packages = [p.strip() for p in packages.replace(",", " ").split()]

        logger.info("[%s] Spawning %r on remote node %r", self.name, name, node)

        # ── Install packages on remote node first ─────────────────────────────
        if packages:
            # Find the node's address — known_nodes, then the spawn registry,
            # then what the installer recorded at deploy time. Only the address:
            # the installer resolves the SSH user and credentials from the
            # node's configured deploy target.
            host = self._known_nodes.get(node, {}).get("host")
            if not host:
                reg = self._get_spawn_registry()
                for cfg in reg.values():
                    if cfg.get("node") == node and cfg.get("host"):
                        host = cfg["host"]
                        break
            if not host and self._registry:
                installer = self._registry.find_by_name("installer")
                if installer:
                    host = installer.recall(f"node_host_{node}")

            if host and self._registry:
                installer = self._registry.find_by_name("installer")
                if installer:
                    logger.info(
                        "[%s] Installing %s on %s (%s) before spawn...",
                        self.name,
                        packages,
                        node,
                        host,
                    )
                    task_id = f"remote_install_{uuid.uuid4().hex[:8]}"
                    future = asyncio.get_running_loop().create_future()
                    self._result_futures[task_id] = future
                    # No credentials in the payload: node_name is enough for the
                    # installer to resolve the configured target itself. This
                    # used to read the password out of the installer's persisted
                    # state and send it back as message data.
                    install_payload = {
                        "action": "node_install",
                        "host": host,
                        "packages": packages,
                        "node_name": node,
                        "_task_id": task_id,
                        "task": task_id,
                    }
                    await self.send(installer.actor_id, MessageType.TASK, install_payload)
                    try:
                        result = await asyncio.wait_for(future, timeout=180.0)
                        if result.get("success"):
                            logger.info("[%s] Remote install OK: %s", self.name, packages)
                        else:
                            logger.warning(
                                "[%s] Remote install issue: %s", self.name, result.get("error", "?")
                            )
                    except asyncio.TimeoutError:
                        logger.warning("[%s] Remote install timed out — spawning anyway", self.name)
                    finally:
                        self._result_futures.pop(task_id, None)
                else:
                    logger.warning(
                        "[%s] installer not found — skipping remote package install for %r",
                        self.name,
                        name,
                    )
            else:
                logger.warning(
                    "[%s] No host known for node %r — cannot pre-install %s. Install manually: ssh into %s and run: pip install %s --break-system-packages",
                    self.name,
                    node,
                    packages,
                    node,
                    " ".join(packages),
                )

        # Publish individual spawn (for immediate delivery).
        # Uses wire_config so any LLM-bridge synthesis is included on the wire.
        await self._mqtt_publish(
            f"nodes/{node}/spawn",
            wire_config,
            retain=True,
            qos=1,
        )

        # Update desired state for the whole node (retained — survives Pi reboot).
        # Also uses wire_config so the runner can reconcile the agent into
        # existence with usable bridge code after a reboot.
        await self._update_node_desired_state(node, wire_config)

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {
                "type": "spawned",
                "message": f"Spawned '{name}' on node '{node}'",
                "child_name": name,
                "node": node,
                "timestamp": time.time(),
            },
        )

        if save:
            # Persist the CLEAN config (no synthesized bridge code) so the
            # registry stays small and bridge regenerates fresh per spawn.
            self._save_to_spawn_registry(config)

        return

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
        """Agents on `node_name` that have missed enough heartbeats to count as gone."""
        return self.nodes.agents_to_prune(node_name, curr_agents, prev_agents)

    @property
    def _agent_manifests(self) -> dict[str, dict[str, Any]]:
        """Latest manifest per agent, owned by `self.nodes`.

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
        """Topic to publishing agents, owned by `self.nodes`."""
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
        """Follow agent manifests. Owned by `self.nodes`."""
        await self.manifests.manifest_listener()

    async def _state_return_listener(self) -> None:
        """Receive agents returning from a node. Owned by `self.nodes`."""
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
        """Periodically check for nodes that have gone silent. If a node has not
        sent a heartbeat in NODE_OFFLINE_GRACE_S, treat all its agents as gone
        and drop the node from our tracking.

        Uses a longer threshold (90s) than the visual "offline" indicator (30s)
        so brief network blips don't trigger false deletion notes. The visual
        indicator stays at 30s for snappy UX; this watcher waits long enough
        to be sure the node is genuinely down.
        """
        NODE_OFFLINE_GRACE_S = 90.0
        CHECK_INTERVAL_S = 15.0

        while self.state.value not in ("stopped", "failed"):
            try:
                await asyncio.sleep(CHECK_INTERVAL_S)
                now = time.time()
                # Snapshot to avoid mutation-during-iteration
                stale_nodes = [
                    (name, info)
                    for name, info in list(self._known_nodes.items())
                    if (now - info.get("last_seen", 0)) > NODE_OFFLINE_GRACE_S
                ]
                if not stale_nodes:
                    continue

                reg = self._get_spawn_registry()
                for node_name, _info in stale_nodes:
                    logger.warning(
                        "[main] Node %r has been silent for >%.0fs — treating as offline",
                        node_name,
                        NODE_OFFLINE_GRACE_S,
                    )
                    # Find all agents that belong to this node according to the
                    # spawn registry (the heartbeat's last-known agent list may
                    # be stale).
                    lost = [n for n, cfg in reg.items() if cfg.get("node", "").strip() == node_name]
                    for agent_name in lost:
                        self._remove_from_spawn_registry(agent_name)
                        await self._clear_agent_manifest(agent_name)
                        self._record_agent_deletion(
                            agent_name,
                            reason=f"node '{node_name}' went offline",
                        )
                    # Drop the node from our tracking. If it comes back, the
                    # heartbeat listener will re-add it as a fresh entry.
                    self._known_nodes.pop(node_name, None)
                    if lost:
                        self._queue_notification(
                            {
                                "_monitor_notification": True,
                                "message": (
                                    f"Node '{node_name}' is offline. "
                                    f"Lost agents: {', '.join(lost)}."
                                ),
                                "severity": "warning",
                                "timestamp": now,
                            }
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[main] Node offline watcher error: %s", e)

    # ── Delegation ─────────────────────────────────────────────────────────

    async def _llm_bridge_listener(self) -> None:
        """Serve LLM calls for remote agents. Owned by `self.llm_bridge`."""
        await self.llm_bridge.listen()

    async def _remote_observed_samples_listener(self) -> None:
        """Subscribe to all MQTT topics published by remote agents and update
        their TopicContract.observed_samples with real payload field names.

        This is what gives the planner accurate schema context for remote
        agents — the declared produces_schema may use wrong field names, but
        the observed_samples reflect what the code ACTUALLY publishes.

        We subscribe to:
          agents/+/data/#    — agent.publish_data() calls from remote agents
          custom/#           — agent-to-agent data streams
          sensors/#          — common IoT namespace
        Additional topic patterns can be added as needed.
        """

        # Build a reverse map: topic prefix → agent name, kept fresh from spawn registry
        def _topic_to_agent(topic: str) -> str | None:
            """Best-effort: find which remote agent publishes to this topic."""
            try:
                from ..core.topic_bus import get_topic_bus

                bus = get_topic_bus()
                if bus:
                    producers = bus.registry.producers_of(topic)
                    if producers:
                        return producers[0].name
            except Exception as exc:
                logger.debug("[%s] TopicBus producer lookup failed: %s", self.name, exc)
            return None

        while self.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(self._mqtt_broker, self._mqtt_port) as client:
                    for pattern in ("agents/+/data/#", "custom/#", "sensors/#"):
                        await client.subscribe(pattern)
                    async for msg in client.messages:
                        topic = str(msg.topic)
                        # Skip internal wactorz topics
                        if any(topic.startswith(p) for p in ("agents/by-name", "nodes/", "main/")):
                            continue
                        payload = _decoded_json(msg.payload)
                        if payload is None:
                            continue
                        if not isinstance(payload, dict):
                            continue

                        agent_name = _topic_to_agent(topic)
                        if not agent_name:
                            continue

                        try:
                            from ..core.topic_bus import get_topic_bus

                            bus = get_topic_bus()
                            if bus:
                                contract = bus.registry.get(agent_name)
                                if contract:
                                    contract.update_observed(topic, payload)
                        except Exception as exc:
                            logger.debug(
                                "[%s] Recording observed sample for %r failed: %s",
                                self.name,
                                agent_name,
                                exc,
                            )

            except asyncio.CancelledError:
                break
            except Exception:
                if self.state.value not in ("stopped", "failed"):
                    await asyncio.sleep(5)

    async def delegate_to_installer(
        self, payload: dict[str, Any], timeout: float = 300.0
    ) -> dict[str, Any]:
        """Send a task to the installer agent and wait for the result.
        Handles node_deploy, node_install, node_run, install, check actions.
        timeout is generous (300s) because deploys involve SSH + pip installs.
        """
        if not self._registry:
            return {"error": "No registry available"}
        installer = self._registry.find_by_name("installer")
        if not installer:
            return {"error": "installer agent not found"}

        task_id = f"inst_{uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._result_futures[task_id] = future

        payload = dict(payload)
        payload["_task_id"] = task_id
        payload["task"] = task_id

        await self.send(installer.actor_id, MessageType.TASK, payload)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"error": f"Installer timed out after {timeout}s"}
        finally:
            self._result_futures.pop(task_id, None)

    async def delegate_task(
        self, target_name: str, task: str, timeout: float = 60.0
    ) -> dict | None:
        """Send a task to a named agent and wait for its result.

        Routing priority:
          1. Local registry — fast in-process mailbox path
          2. Remote node   — MQTT request/reply via agents/by-name/{name}/task
          3. Returns None if the agent is unknown in both
        """
        if not self._registry:
            return None

        target = self._registry.find_by_name(target_name)
        if target:
            # ── Local path (fast, in-process) ────────────────────────────────
            task_id = uuid.uuid4().hex
            future = asyncio.get_event_loop().create_future()
            self._result_futures[task_id] = future
            await self.send(
                target.actor_id,
                MessageType.TASK,
                {
                    "text": task,
                    "_task_id": task_id,
                    "task": task_id,
                    "reply_to": self.actor_id,
                },
            )
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                self._result_futures.pop(task_id, None)

        # ── Remote path: check if agent is on a known node ───────────────────
        remote_node = None
        for node_name, nd in self._known_nodes.items():
            if target_name in nd.get("agents", []):
                remote_node = node_name
                break

        if not remote_node:
            logger.warning(
                "[%s] delegate_task: %r not found locally or remotely", self.name, target_name
            )
            return None

        async with self._reply_topic() as (reply_topic, future):
            await self._mqtt_publish(
                f"agents/by-name/{target_name}/task",
                {"text": task, "payload": task, "_reply_topic": reply_topic, "_remote_task": True},
            )
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] delegate_task: %r on %r timed out after %ss",
                    self.name,
                    target_name,
                    remote_node,
                    timeout,
                )
                return None

    #: How long to wait for the reply subscription before publishing anyway.
    #: Short: a broker that is up answers a subscribe immediately, and one that
    #: is not will not answer at all.
    _SUBSCRIBE_TIMEOUT = 5.0

    @contextlib.asynccontextmanager
    async def _reply_topic(self) -> AsyncGenerator[tuple[str, asyncio.Future]]:
        """A reply topic that is already subscribed, and the future its answer lands in.

        ⚠ **Subscribing before the caller publishes is the whole point.** Both
        call sites used to publish first and subscribe after, so an agent that
        answered quickly answered into nothing: the reply was published to a
        topic with no subscriber, dropped by the broker, and the caller waited
        out its full timeout for a task that had in fact succeeded. It reads as
        a flaky node, which is why it survived so long. The monitor's own copy
        of this logic was corrected years ago and the fix was never brought back
        here.

        Publishes anyway if the subscription does not come up in time. The task
        still gets done, and its side effects are usually what the caller wanted;
        losing the answer is worse than losing the work. The reason is logged, so
        a timeout that follows is explained rather than mysterious.
        """
        topic = f"main/reply/{self.actor_id}/{uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._result_futures[topic] = future
        subscribed = asyncio.Event()

        async def _listen() -> None:
            try:
                async with mqtt_client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe(topic)
                    subscribed.set()
                    async for msg in client.messages:
                        try:
                            if not future.done():
                                future.set_result(json.loads(msg.payload.decode()))
                        except Exception as exc:
                            logger.debug("[%s] Undecodable reply on %r: %s", self.name, topic, exc)
                        return
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                # Whatever happened, stop the caller waiting on a listener that
                # is no longer going to subscribe.
                subscribed.set()

        task = asyncio.create_task(_listen())
        try:
            try:
                await asyncio.wait_for(subscribed.wait(), timeout=self._SUBSCRIBE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] reply subscription for %s did not come up in %.0fs — sending anyway, "
                    "so a reply may be missed",
                    self.name,
                    topic,
                    self._SUBSCRIBE_TIMEOUT,
                )
            yield topic, future
        finally:
            task.cancel()
            self._result_futures.pop(topic, None)

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
        """Permanently delete an agent.

        Unlike a stop (which preserves state so the agent can resume later),
        delete removes EVERY trace so a future spawn with the same name
        starts truly clean:

          - Spawn registry entry removed (so no auto-respawn on restart).
          - For remote agents: the `nodes/<node>/stop` message carries
            ``delete=True`` so the runner unlinks <name>_state.json on disk
            and purges this agent's retained MQTT topics from the broker.
          - For local agents: the underlying PersistenceAPI.purge() wipes
            SQLite kv_store rows, in-memory ephemeral keys, and the agent's
            state.pkl directory.
          - Either way, main also publishes empty retained payloads on the
            per-agent MQTT topics as a defensive second pass — if the runner
            is offline or main is acting alone, the broker is still cleared.
        """
        # Find node before removing from registry. If the registry has no record
        # of the agent (e.g. already pruned), fall back to live heartbeat
        # telemetry — otherwise a remote agent would take the local-only branch
        # below and keep running on its node.
        reg = self._get_spawn_registry()
        node = reg.get(name, {}).get("node", "").strip()
        if not node:
            node = self._node_running_agent(name)

        # Capture/derive the deterministic actor_id BEFORE we tear anything down,
        # so we can purge per-agent retained topics even after the local actor
        # is gone from the registry.
        actor_id: str | None = None
        if self._registry:
            target = self._registry.find_by_name(name)
            if target:
                actor_id = target.actor_id
        if not actor_id:
            # Remote agents (and local ones missing from the registry) follow the
            # same uuid5 scheme used by _RemoteAgent and Actor — derive it.
            actor_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.actor.{name}"))

        self._remove_from_spawn_registry(name)

        # Remote path — let the runner do the heavy work on the edge node.
        if node:
            await self._update_node_desired_state(node, remove_name=name)
            await self._mqtt_publish(
                f"nodes/{node}/stop",
                {"name": name, "delete": True},
                qos=1,
            )
            await self._clear_agent_manifest(name, actor_id)
            await self._purge_agent_retained_topics(actor_id)
            self._record_agent_deletion(name, reason=f"deleted from node '{node}'")
            return

        # Local path — stop the actor and wipe its persistence directly.
        if self._registry:
            target = self._registry.find_by_name(name)
            if target:
                actor_id = target.actor_id
                sv = getattr(self._registry, "_supervisor_ref", None)
                if sv is not None:
                    sv.release(name)
                await self._registry.unregister(actor_id)
                await target.stop()
                await self._purge_local_agent_persistence(target, name)
                await self._clear_agent_manifest(name, actor_id)
                await self._purge_agent_retained_topics(actor_id)
                self._record_agent_deletion(name, reason="deleted")
                return

        # Agent wasn't in the registry — still purge the broker side so any
        # stale retained messages from a previous incarnation are cleared.
        await self._clear_agent_manifest(name, actor_id)
        await self._purge_agent_retained_topics(actor_id)
        self._record_agent_deletion(name, reason="deleted (no live actor found)")

    async def _purge_agent_retained_topics(self, actor_id: str) -> None:
        """Publish empty retained payloads on every per-agent MQTT topic so the
        broker stops re-delivering them on later reconnects.

        Mirrors the same purge done by the remote runner on delete and by the
        monitor process — running it from all three sides is intentional, so
        deletion succeeds even when one side is offline.
        """
        if not actor_id:
            return
        for metric in (
            "status",
            "heartbeat",
            "metrics",
            "logs",
            "spawned",
            "manifest",
            "errors",
            "detections",
            "results",
            "completed",
        ):
            try:
                await self._mqtt_publish(f"agents/{actor_id}/{metric}", b"", retain=True)
            except Exception as e:
                logger.debug(
                    "[%s] Failed to clear retained agents/%s/%s: %s", self.name, actor_id, metric, e
                )

    async def _purge_local_agent_persistence(
        self,
        actor: Any,
        name: str,
    ) -> None:
        """For a local actor: hard-delete its persisted state across all
        backends (SQLite kv_store rows, in-memory ephemeral keys, pickle file).

        Uses the actor's own PersistenceAPI when available so the right
        databases are touched. Falls back to a best-effort filesystem cleanup
        if the new API isn't wired up (legacy pickle-only mode).
        """
        # Preferred path: actor has the unified PersistenceAPI.
        api = getattr(actor, "_persistence_api", None) or getattr(actor, "_persistence", None)
        if api is not None and hasattr(api, "purge"):
            try:
                api.purge()
            except Exception as e:
                logger.warning(
                    "[%s] PersistenceAPI.purge() failed for %r: %s — falling back to filesystem cleanup",
                    self.name,
                    name,
                    e,
                )
            else:
                return

        # Legacy fallback: nuke the agent's pickle directory directly.
        pdir = getattr(actor, "_persistence_dir", None)
        if pdir is not None:
            try:
                pdir_path = str(pdir)
                shutil.rmtree(pdir_path, ignore_errors=True)
                logger.info(
                    "[%s] Removed local persistence dir for %r: %s", self.name, name, pdir_path
                )
            except Exception as e:
                logger.warning(
                    "[%s] Could not remove persistence dir for %r: %s", self.name, name, e
                )
