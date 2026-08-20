"""Deleting an agent, and everything that has to go with it.

Delete is not a stronger stop. A stop keeps the agent's state so it can resume;
delete removes every trace, so a later spawn under the same name starts clean
rather than inheriting a stranger's memory.

Three routes. A remote agent's own node does the work, told over MQTT with a
flag that distinguishes deletion from a stop. A local one is unregistered,
stopped and purged here. An agent in neither place still has its broker side
cleared, because a retained message outlives whatever published it.

Order matters inside each route: the node is found before the registry entry is
removed, and the actor id is derived before the actor is gone. Otherwise a
remote agent takes the local branch and keeps running, or its retained topics
can no longer be addressed.

The broker is cleared from every side at once — main here, the runner on the
node, and the monitor. That is deliberate duplication, so a delete still
completes when one of them is offline.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .hosts import LifecycleHost

logger = logging.getLogger(__name__)

#: Agents main never deletes, whoever asks.
#:
#: Housekeeping the system needs to keep working: without the catalogue or the
#: installer, a running system can no longer spawn or deploy anything, and the
#: request to remove one arrives as ordinary model output.
PROTECTED_AGENTS = frozenset(
    {
        "main",
        "monitor",
        "installer",
        "home-assistant-agent",
        "anomaly-detector",
        "code-agent",
        "catalog",
    }
)

#: The per-agent topics whose retained payloads are cleared on delete.
RETAINED_AGENT_TOPICS = (
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
)


class LifecycleService:
    """Removes an agent and every trace it left behind."""

    def __init__(self, host: LifecycleHost) -> None:
        self.host = host

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
            self.host._conversation_history.append({"role": "user", "content": note})
            self.host._conversation_history.append(
                {
                    "role": "assistant",
                    "content": f"Acknowledged — '{name}' has been removed from my view.",
                }
            )
            self.host.persist("conversation_history", self.host._conversation_history)
            logger.info("[%s] Recorded deletion note for %r in history", self.host.name, name)
        except Exception as e:
            logger.warning("[%s] Failed to record deletion note: %s", self.host.name, e)

    async def _clear_agent_manifest(self, name: str, actor_id: str | None = None) -> None:
        """Clear an agent's manifest from main's in-memory caches AND from the
        retained MQTT manifest topic. Without this, list_capabilities() will
        keep reporting the agent (with running=false but never disappearing),
        and on next restart it would be re-loaded from the retained message.

        Call this whenever an agent is stopped/deleted/replaced.
        """
        # Drop from in-memory caches immediately
        self.host._agent_manifests.pop(name, None)
        for topic, entries in list(self.host._topic_registry.items()):
            self.host._topic_registry[topic] = [m for m in entries if m.get("name") != name]
            if not self.host._topic_registry[topic]:
                self.host._topic_registry.pop(topic, None)
        # Publish empty retained payload to clear the broker-side retained manifest.
        # Need actor_id for the topic — fall back to looking it up from the registry
        # (only works if the actor is still alive — best-effort).
        if not actor_id and self.host._registry:
            target = self.host._registry.find_by_name(name)
            if target:
                actor_id = target.actor_id
        if actor_id:
            await self.host._mqtt_publish(f"agents/{actor_id}/manifest", b"", retain=True)
            logger.debug("[%s] Cleared retained manifest for %r", self.host.name, name)

    async def _purge_agent_retained_topics(self, actor_id: str | None) -> None:
        """Publish empty retained payloads on every per-agent MQTT topic so the
        broker stops re-delivering them on later reconnects.

        Mirrors the same purge done by the remote runner on delete and by the
        monitor process — running it from all three sides is intentional, so
        deletion succeeds even when one side is offline.
        """
        if not actor_id:
            return
        for metric in RETAINED_AGENT_TOPICS:
            try:
                await self.host._mqtt_publish(f"agents/{actor_id}/{metric}", b"", retain=True)
            except Exception as e:
                logger.debug(
                    "[%s] Failed to clear retained agents/%s/%s: %s",
                    self.host.name,
                    actor_id,
                    metric,
                    e,
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
                    self.host.name,
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
                    "[%s] Removed local persistence dir for %r: %s", self.host.name, name, pdir_path
                )
            except Exception as e:
                logger.warning(
                    "[%s] Could not remove persistence dir for %r: %s", self.host.name, name, e
                )

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
        reg = self.host._get_spawn_registry()
        node = reg.get(name, {}).get("node", "").strip()
        if not node:
            node = self.host._node_running_agent(name)

        # Capture/derive the deterministic actor_id BEFORE we tear anything down,
        # so we can purge per-agent retained topics even after the local actor
        # is gone from the registry.
        actor_id: str | None = None
        if self.host._registry:
            target = self.host._registry.find_by_name(name)
            if target:
                actor_id = target.actor_id
        if not actor_id:
            # Remote agents (and local ones missing from the registry) follow the
            # same uuid5 scheme used by _RemoteAgent and Actor — derive it.
            actor_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.actor.{name}"))

        self.host._remove_from_spawn_registry(name)

        # Remote path — let the runner do the heavy work on the edge node.
        if node:
            await self.host._update_node_desired_state(node, remove_name=name)
            await self.host._mqtt_publish(
                f"nodes/{node}/stop",
                {"name": name, "delete": True},
                qos=1,
            )
            await self._clear_agent_manifest(name, actor_id)
            await self._purge_agent_retained_topics(actor_id)
            self._record_agent_deletion(name, reason=f"deleted from node '{node}'")
            return

        # Local path — stop the actor and wipe its persistence directly.
        if self.host._registry:
            target = self.host._registry.find_by_name(name)
            if target:
                actor_id = target.actor_id
                sv = getattr(self.host._registry, "_supervisor_ref", None)
                if sv is not None:
                    sv.release(name)
                await self.host._registry.unregister(actor_id)
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
        known_names = set(self.host._agent_manifests.keys())
        if self.host._registry:
            known_names |= {a.name for a in self.host._registry.all_actors()}
        # Spawn registry is the strongest signal — if it's persisted there, deletion
        # is meaningful even if the live actor isn't currently up.
        known_names |= set(self.host._get_spawn_registry().keys())

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
                        "[%s] Empty or malformed <delete> block: %s", self.host.name, block[:200]
                    )
                    continue
                name = name.strip()

                if name in PROTECTED_AGENTS:
                    logger.warning(
                        "[%s] Refused to delete protected agent %r", self.host.name, name
                    )
                    continue

                if name not in known_names:
                    logger.info(
                        "[%s] LLM requested deletion of unknown agent %r", self.host.name, name
                    )
                    missing.append(name)
                    continue

                logger.info("[%s] LLM-requested deletion of %r", self.host.name, name)
                # Reuse the existing helper — it handles spawn registry, stop,
                # manifest cleanup, history note, and remote-vs-local routing.
                await self.delete_spawned_agent(name)
                deleted.append(name)
            except json.JSONDecodeError:
                logger.exception(
                    "[%s] Invalid <delete> JSON\nRaw block: %s", self.host.name, block[:200]
                )
            except Exception:
                logger.exception("[%s] Delete failed\nRaw block:\n%s", self.host.name, block[:500])

        clean = re.sub(pattern, "", response, flags=re.DOTALL).strip()
        return clean, deleted, missing
