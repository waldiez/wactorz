"""Bringing an agent back from the node that was running it.

A migration home asks the node for everything it holds — the spawn config and
the persistent state — and spawns the agent locally from what comes back.

**The config in that message is executed**: it carries the agent's code. The
token is what makes that acceptable. Main mints one per migration it starts, and
only a message quoting it is acted on; it is consumed on use and expires, so a
replayed message spawns nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    session_kwargs,
)
from .spawns import without_transient_keys

if TYPE_CHECKING:
    from .hosts import MigrationHost, NodeReaders

logger = logging.getLogger(__name__)

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0

#: How long a migration's return token stays usable.
#:
#: A node that never answers would otherwise leave its token in the pending map
#: for the life of the process. Five minutes is far longer than a migration
#: takes and short enough that the map stays small.
TOKEN_TTL_S = 300.0

#: How often to look for migrations that stalled. Half the token lifetime, so a
#: stalled one is recovered within roughly one and a half TTLs rather than
#: waiting for a message that is never coming.
SWEEP_INTERVAL_S = TOKEN_TTL_S / 2

#: Identifies the code synthesized for an LLM agent sent out to a node, so it
#: can be dropped when the agent comes back. Hand-written code never has it.
BRIDGE_CODE_MARKER = "Auto-generated LLM bridge"

#: `from_node` for an agent that was running here rather than on a node. The
#: source of a migration is a node name everywhere else, so the sentinel is what
#: tells the completion and rollback paths to act on this process instead of
#: publishing to a node topic that does not exist.
LOCAL_SOURCE = "local"

#: Where the migrations in flight are recorded, so a restart does not drop them.
PENDING_MIGRATIONS_KEY = "_pending_migrations"

#: Entry fields that cannot outlive the process, and so are never written.
#:
#: `local_actor` is the stopped local instance, carried so the ack can purge its
#: persistence through the actor's own API. A restart loses it, and there is
#: nothing to fall back to: `_purge_local_agent_persistence` takes the agent name
#: for its log lines only, and every deletion goes through the actor. So a
#: restored local→remote migration purges through whatever copy startup brought
#: back instead, and leaves the state behind only when there is none — see
#: `_purge_local_source`.
_TRANSIENT_ENTRY_FIELDS = ("local_actor",)


class Migration:
    """Agents in flight between main and a node."""

    def __init__(self, host: MigrationHost | None = None, nodes: NodeReaders | None = None) -> None:
        self.host = host
        #: The live node view. Read for "is the target up" and "where is this
        #: agent" — questions the heartbeat table answers and nothing else can.
        self.host_nodes = nodes
        #: return token -> the migration main started and is waiting on.
        self.pending_returns: dict[str, dict[str, Any]] = {}
        #: Migrations waiting for a target node to confirm the spawn started.
        self.pending_spawns: dict[str, dict[str, Any]] = {}

    def _save_pending(self) -> None:
        """Record both maps. Called after every change to either.

        A failure here is logged and swallowed: losing the record degrades a
        restart back to the behaviour that existed before this, and that is not
        a reason to fail the migration currently in flight.
        """
        if self.host is None:
            return
        try:
            self.host.persist(
                PENDING_MIGRATIONS_KEY,
                {
                    "returns": self.pending_returns,
                    "spawns": {
                        token: {
                            key: value
                            for key, value in entry.items()
                            if key not in _TRANSIENT_ENTRY_FIELDS
                        }
                        for token, entry in self.pending_spawns.items()
                    },
                },
            )
        except Exception:
            logger.exception("[main] Could not record the migrations in flight")

    def restore(self) -> None:
        """Take back the migrations that were in flight when the process stopped.

        Without this a restart drops the token, and the two halves of the
        choreography both go quiet: an ack arriving afterwards finds nothing
        waiting and is ignored, and no rollback ever fires. The agent comes back
        locally from the spawn registry while the target may also be running it —
        the duplicate the confirmation work exists to prevent, reached through
        the one door it left open.

        Nothing is resolved here. The sweep already runs on a timer and already
        knows the rules, so an entry that is past its TTL is handled on its first
        pass rather than needing a second implementation at startup.
        """
        if self.host is None:
            return
        try:
            stored = self.host.recall(PENDING_MIGRATIONS_KEY) or {}
        except Exception:
            logger.exception("[main] Could not read back the migrations in flight")
            return
        if not isinstance(stored, dict):
            return
        returns = stored.get("returns") or {}
        spawns = stored.get("spawns") or {}
        if isinstance(returns, dict):
            self.pending_returns.update(returns)
        if isinstance(spawns, dict):
            self.pending_spawns.update(spawns)
        if self.pending_returns or self.pending_spawns:
            logger.info(
                "[main] Took back %s migration(s) in flight: %s waiting to come back, "
                "%s waiting to be confirmed",
                len(self.pending_returns) + len(self.pending_spawns),
                len(self.pending_returns),
                len(self.pending_spawns),
            )

    async def state_return_listener(self) -> None:
        """Follow `nodes/+/state_return` until the actor stops."""
        host = self.host
        if host is None:
            return

        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(
                    host._mqtt_broker,
                    host._mqtt_port,
                    identifier=client_id("srv", install_id(), "migration"),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    await client.subscribe("nodes/+/state_return", qos=1)
                    await client.subscribe("nodes/+/spawn_ack", qos=1)
                    logger.info("[main] Subscribed to state_return topics.")
                    last_error = None
                    async for message in client.messages:
                        if str(message.topic).endswith("/spawn_ack"):
                            await self.receive_spawn_ack(str(message.topic), message.payload)
                        else:
                            await self.receive_state_return(str(message.topic), message.payload)
                        await self.expire_pending_spawns()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[main] state_return listener error: %s. Reconnecting in %ss…",
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                else:
                    logger.debug(
                        "[main] state_return listener still unavailable — retrying in %ss…",
                        int(RECONNECT_DELAY_S),
                    )
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def receive_state_return(self, topic: str, payload: bytes | None) -> None:
        """Take an agent back from the node that was running it.

        The config in this message is executed — it carries the agent's code.
        The token is what makes that acceptable: main mints one per migration it
        starts, and only a message quoting it is acted on. Consumed on use, so a
        replay finds nothing waiting.
        """
        if not payload:
            return
        try:
            data = json.loads(payload.decode())
        except Exception:
            return
        if not isinstance(data, dict):
            return

        self._expire_tokens()
        token = data.get("return_token", "")
        if not token or token not in self.pending_returns:
            logger.warning(
                "[main] state_return with unknown/expired token %r from %s — ignoring",
                f"{token[:8]}…",
                topic,
            )
            return

        started = self.pending_returns.pop(token)
        self._save_pending()
        agent_name = data.get("agent") or started.get("agent_name", "?")
        from_node = started.get("from_node", "?")
        cfg = data.get("config") or {}
        state = data.get("state") or {}

        if not cfg or not isinstance(cfg, dict):
            logger.warning(
                "[main] state_return for %r from %r has no config — cannot spawn locally",
                agent_name,
                from_node,
            )
            return

        target_node = started.get("target_node", "")
        if target_node:
            await self._place_on_target(agent_name, from_node, target_node, cfg, state)
            return
        if await self._respawn_locally(agent_name, from_node, cfg, state):
            # Local again, and confirmed: the source may now drop its copy.
            await self._tell_source_to_delete(agent_name, from_node)

    async def _place_on_target(
        self,
        agent_name: str,
        from_node: str,
        target_node: str,
        cfg: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        """Spawn a returned agent on another node, and wait to be told it started.

        The source still holds its state file: nothing is deleted until the
        target confirms, so a migration that fails here leaves the agent exactly
        where it was rather than between two nodes.
        """
        if self.host is None:
            return
        token = secrets.token_hex(8)
        config = dict(cfg)
        config["name"] = agent_name
        config["node"] = target_node
        config.pop("replace", None)
        if state:
            config["_initial_state"] = state
        config["_migration_token"] = token

        self.pending_spawns[token] = {
            "agent_name": agent_name,
            "from_node": from_node,
            "target_node": target_node,
            "config": config,
            "started_at": time.time(),
        }
        self._save_pending()
        await self.host._spawn_remote(config, target_node, save=False)
        logger.info(
            "[%s] Placed %r on %r; waiting for it to confirm it started",
            self.host.name,
            agent_name,
            target_node,
        )

    async def receive_spawn_ack(self, topic: str, payload: bytes | None) -> None:
        """A target node confirms an agent it was handed is running.

        Only now is the migration complete: the registry moves, both nodes'
        desired state is rewritten, and the source is told to drop its copy.
        """
        if not payload or self.host is None:
            return
        try:
            data = json.loads(payload.decode())
        except Exception:
            return
        if not isinstance(data, dict):
            return

        self._expire_tokens()
        token = data.get("migration_token", "")
        pending = self.pending_spawns.pop(token, None) if token else None
        self._save_pending()
        if pending is None:
            logger.debug("[main] spawn_ack with unknown/expired token from %s — ignoring", topic)
            return

        agent_name = pending["agent_name"]
        from_node = pending["from_node"]
        target_node = pending["target_node"]
        config = pending["config"]

        committed = {k: v for k, v in config.items() if not k.startswith("_migration")}
        self.host._save_to_spawn_registry(committed)
        if from_node != LOCAL_SOURCE:
            # There is no `nodes/local/desired_state`: a local source is this
            # process, and dropping the agent from it is the purge below.
            await self.update_desired_state(from_node, remove_name=agent_name)
        await self.update_desired_state(target_node, committed)
        await self._release_source(pending)
        self._announce(
            f"Migration of '{agent_name}' from '{from_node}' → '{target_node}' complete.",
            "info",
        )

    async def _release_source(self, pending: dict[str, Any]) -> None:
        """Drop the source's copy, now that the agent is confirmed elsewhere.

        Where that copy lives is the only difference between the legs: a node
        is told to delete, and a local source is purged here.
        """
        from_node = pending.get("from_node", "")
        if from_node == LOCAL_SOURCE:
            await self._purge_local_source(pending)
            return
        await self._tell_source_to_delete(pending["agent_name"], from_node)

    async def _purge_local_source(self, pending: dict[str, Any]) -> None:
        """Wipe what the local instance persisted, once the target has it.

        Held until the ack for the reason a node's copy is: until the target
        says it started, this is the only intact copy of the agent. Purged at
        all because the snapshot has moved on — left behind, it would merge
        with the state that comes home on a migrate-back and duplicate
        everything in it.

        The stopped actor is carried in the pending entry rather than looked up
        again: it is unregistered by now, and its own persistence API is what
        knows which backends to clear.
        """
        actor = pending.get("local_actor")
        if self.host is None:
            return
        agent_name = pending.get("agent_name", "?")
        if actor is None:
            # The migration outlived the process that started it, so the
            # instance it stopped is gone — but startup may have brought the
            # agent back. The registry was never moved to the target (that waits
            # for this ack), so restoring spawned agents saw a local agent and
            # restarted it. Left alone, the agent then runs here *and* on the
            # target, for good. Stopped the way `migrate_agent` stopped the
            # original, and then purged through: a stopped actor is exactly what
            # the purge needs, so this also settles the purge instead of skipping.
            registry = self.host._registry
            revived = registry.find_by_name(agent_name) if registry else None
            if revived is None:
                logger.info(
                    "[%s] %r moved to %r across a restart — its local state is left "
                    "in place, and will merge if it ever migrates back",
                    self.host.name,
                    agent_name,
                    pending.get("target_node", "?"),
                )
                return
            try:
                await registry.unregister(revived.actor_id)
                await revived.stop()
            except Exception as exc:
                # Not purged: an instance that failed to stop may still be
                # running, and wiping its state underneath it is worse.
                logger.warning(
                    "[%s] %r is confirmed on %r but the copy restarted here would not stop: %s",
                    self.host.name,
                    agent_name,
                    pending.get("target_node", "?"),
                    exc,
                )
                return
            self.host._agent_manifests.pop(agent_name, None)
            logger.info(
                "[%s] Stopped the copy of %r restarted here — it is confirmed on %r",
                self.host.name,
                agent_name,
                pending.get("target_node", "?"),
            )
            actor = revived
        try:
            await self.host._purge_local_agent_persistence(actor, agent_name)
        except Exception as exc:
            logger.warning(
                "[%s] Could not purge local persistence for %r after it moved to %r: %s",
                self.host.name,
                agent_name,
                pending.get("target_node", "?"),
                exc,
            )

    async def _tell_source_to_delete(self, agent_name: str, from_node: str) -> None:
        """Drop the source's copy, now that the agent is confirmed elsewhere."""
        if self.host is None or not from_node or from_node == LOCAL_SOURCE:
            return
        await self.host._mqtt_publish(
            f"nodes/{from_node}/stop",
            {"name": agent_name, "delete": True},
            qos=1,
        )

    async def expire_pending_spawns(self) -> None:
        """Put back any agent whose target never confirmed it started.

        The source was stopped but not deleted, so recovery is a re-spawn there
        rather than anything to reconstruct -- which is what makes "a failed
        migration leaves the system where it started" structural rather than
        something rollback logic has to get right.
        """
        if self.host is None:
            return
        now = time.time()
        for token, pending in list(self.pending_spawns.items()):
            if now - pending.get("started_at", 0) <= TOKEN_TTL_S:
                continue
            self.pending_spawns.pop(token, None)
            self._save_pending()
            agent_name = pending["agent_name"]
            from_node = pending["from_node"]
            target_node = pending["target_node"]
            logger.warning(
                "[%s] %r never confirmed on %r — putting it back on %r",
                self.host.name,
                agent_name,
                target_node,
                from_node,
            )
            # Clear the target first, best effort. The ack may have been lost
            # rather than never sent, in which case the target is running the
            # agent and putting it back on the source would leave two -- the
            # duplicate this whole choreography exists to avoid. An agent that
            # never started ignores the stop.
            await self.host._mqtt_publish(
                f"nodes/{target_node}/stop",
                {"name": agent_name, "delete": True},
                qos=1,
            )
            # The stop is a one-off, but placing the agent added it to the
            # target's desired state, and that message is retained. Left there,
            # the next time the node reboots it reconciles the agent back into
            # existence beside the copy this rollback is restoring, and the two
            # of them carry the same name and diverge from the same state.
            await self.update_desired_state(target_node, remove_name=agent_name)
            if from_node == LOCAL_SOURCE:
                await self._restore_local(pending)
            else:
                restored = {k: v for k, v in pending["config"].items() if k != "_migration_token"}
                restored["node"] = from_node
                await self.host._spawn_remote(restored, from_node, save=True)
            self._announce(
                f"Migration of '{agent_name}' to '{target_node}' failed — "
                f"it is back on '{from_node}'.",
                "warning",
            )

    async def _restore_local(self, pending: dict[str, Any]) -> None:
        """Start the agent here again after its target never confirmed.

        No snapshot is carried back in. The local state was never purged --
        that is what the ack was holding up -- so what is on disk is both
        intact and newer than the copy that was shipped out.
        """
        if self.host is None:
            return
        restored = {
            k: v
            for k, v in pending["config"].items()
            if k not in ("_migration_token", "_initial_state", "node")
        }
        restored["replace"] = True
        await self.host._spawn_from_config(restored, save=True)

    def _expire_tokens(self) -> None:
        """Forget return tokens whose migration never completed.

        Only forgetting them. Putting the agent back is
        :meth:`expire_pending_returns`, which needs to publish and so cannot run
        from the synchronous paths that call this.
        """
        now = time.time()
        for token, started in list(self.pending_returns.items()):
            if now - started.get("started_at", 0) > TOKEN_TTL_S:
                self.pending_returns.pop(token, None)
                self._save_pending()

    async def expire_pending_returns(self) -> None:
        """Restart an agent whose node was asked to hand it back and never did.

        The source stops the agent before publishing `state_return`, so a source
        that dies in between leaves it stopped and intact but running nowhere.
        Forgetting the token is not enough: the registry still places the agent
        on that node, so the recovery is to spawn it there again.
        """
        if self.host is None:
            return
        now = time.time()
        for token, started in list(self.pending_returns.items()):
            if now - started.get("started_at", 0) <= TOKEN_TTL_S:
                continue
            self.pending_returns.pop(token, None)
            self._save_pending()
            agent_name = started.get("agent_name", "")
            from_node = started.get("from_node", "")
            if not agent_name or not from_node:
                continue
            config = self.host._get_spawn_registry().get(agent_name)
            if not config:
                logger.warning(
                    "[%s] %r never came back from %r and is not in the registry — "
                    "it cannot be restarted automatically",
                    self.host.name,
                    agent_name,
                    from_node,
                )
                continue
            logger.warning(
                "[%s] %r never came back from %r — restarting it there",
                self.host.name,
                agent_name,
                from_node,
            )
            restored = dict(config)
            restored["node"] = from_node
            await self.host._spawn_remote(restored, from_node, save=True)
            self._announce(
                f"'{agent_name}' did not come back from '{from_node}' — "
                f"it has been restarted there.",
                "warning",
            )

    async def stalled_migration_watcher(self) -> None:
        """Sweep for stalled migrations on a timer, until the actor stops.

        Sweeps before the first wait, not after it: a migration taken back by
        `restore` may have run out its time while the process was down, and
        waiting a full interval would leave its rollback pending for no reason.
        """
        while self.host is not None and self.host.state.value not in ("stopped", "failed"):
            try:
                await self.sweep_stalled_migrations()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[main] Migration sweep failed")
            await asyncio.sleep(SWEEP_INTERVAL_S)

    async def sweep_stalled_migrations(self) -> None:
        """Recover from either leg going quiet.

        Called on a timer rather than when a message arrives: `state_return` and
        `spawn_ack` carry migration traffic and nothing else, so the very
        failure this recovers from -- a node going away mid-migration -- produces
        no message to trigger it. Driven by the message loop alone, the net
        would spring only on the next unrelated migration, which may never come.
        """
        await self.expire_pending_returns()
        await self.expire_pending_spawns()

    @staticmethod
    def local_spawn_config(
        agent_name: str, cfg: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        """The returned config, rewritten to run here.

        The node is dropped because the agent is coming home, and leaving it on
        would send it straight back. `replace` is set so a leftover local
        instance of the same name is swapped rather than colliding.

        An LLM agent loses the code synthesized for it on the way out: locally
        the type routes to the real class and the code is ignored, so keeping it
        would put something misleading in the spawn registry. The marker comment
        identifies it; hand-written code never carries one.
        """
        local_cfg = dict(cfg)
        local_cfg.pop("node", None)
        local_cfg.pop("_initial_state", None)
        if state:
            local_cfg["_initial_state"] = state
        local_cfg["replace"] = True
        local_cfg.setdefault("name", agent_name)
        if local_cfg.get("type") == "llm" and BRIDGE_CODE_MARKER in (local_cfg.get("code") or ""):
            local_cfg.pop("code", None)
        return local_cfg

    async def _respawn_locally(
        self,
        agent_name: str,
        from_node: str,
        cfg: dict[str, Any],
        state: dict[str, Any],
    ) -> bool:
        """Spawn the returned agent here, and say whether it worked.

        A failure is announced rather than logged alone, and the answer decides
        whether the source may drop its copy: if this fails, the node's stopped
        agent is the only one left, and deleting it would lose the agent.
        """
        host = self.host
        if host is None:
            return False
        local = self.local_spawn_config(agent_name, cfg, state)
        earned = host._restore_earned_trust(agent_name, local)
        logger.info(
            "[main] Received state_return for %r from %r (%s state key(s)) — spawning locally",
            agent_name,
            from_node,
            len(state) if isinstance(state, dict) else 0,
        )
        try:
            await host._spawn_from_config(local, save=True, from_registry=earned)
        except Exception as exc:
            logger.exception("[main] Local re-spawn after state_return failed for %r", agent_name)
            self._announce(
                f"Migration of '{agent_name}' from '{from_node}' → local FAILED: {exc}", "warning"
            )
            return False
        self._announce(f"Migration of '{agent_name}' from '{from_node}' → local succeeded.", "info")
        return True

    def _announce(self, message: str, severity: str) -> None:
        host = self.host
        if host is None:
            return
        host._queue_notification(
            {
                "_monitor_notification": True,
                "message": message,
                "severity": severity,
                "timestamp": time.time(),
            }
        )

    async def migrate_agent(self, agent_name: str, target_node: str) -> dict[str, Any]:
        """Move a running agent to a different node.

        Sources of truth, in priority order:
          1. Spawn registry — has the full config including code.
          2. Agent manifest — has node, description, schemas (no code).
          3. Node heartbeats — minimum: tells us which node the agent runs on.

        If we have code (case 1), we can do any migration unilaterally.
        If we only have node info (cases 2/3), remote→local migration uses the
        '@main' sentinel — the remote node ships its full config back and main
        re-spawns locally.

        Returns {"success": bool, "message": str}
        """
        if self.host is None or self.host_nodes is None:
            return {}
        reg = self.host._get_spawn_registry()
        config = reg.get(agent_name)
        have_code = bool(config and config.get("code"))

        # ── Locate the agent via every source we have ─────────────────────────
        # Registry first, then manifest, then heartbeats. We need at least to
        # know WHERE the agent is to do anything useful.
        manifest = self.host._agent_manifests.get(agent_name, {})
        current_node = ""
        if config:
            current_node = (config.get("node") or "").strip()
        if not current_node and manifest:
            current_node = (manifest.get("node") or "").strip()
        if not current_node:
            # Last resort — the live heartbeats, which may know a node the spawn
            # registry does not record.
            current_node = self.host_nodes.running_agent(agent_name)

        # ── Verify the agent exists *somewhere* ───────────────────────────────
        # If we found nothing — not in registry, not in manifest, not heart-
        # beating from any node, not in the local registry — we genuinely
        # can't migrate what doesn't exist.
        local_alive = bool(self.host._registry and self.host._registry.find_by_name(agent_name))
        if not config and not manifest and not current_node and not local_alive:
            return {
                "success": False,
                "message": f"Agent '{agent_name}' not found anywhere (no registry "
                f"entry, no manifest, no heartbeat). Nothing to migrate.",
            }

        if current_node == target_node:
            return {
                "success": False,
                "message": f"Agent '{agent_name}' is already on '{target_node or 'local'}'.",
            }

        # Normalise "local" as a target so users can type /migrate agent-name local
        is_target_local = target_node.strip().lower() in ("", "local", "main")

        # ── Verify the target node exists and is online ────────────────────────
        # This must happen BEFORE anything destructive. Without it, a typo'd or
        # offline target made the agent vanish: the source stopped the agent
        # (deleting its state file) and published the hand-off to a node topic
        # nobody was listening on. Refuse up front instead — the agent keeps
        # running where it is.
        if not is_target_local and not self.host._node_is_online(target_node):
            if target_node in self.host_nodes.known:
                reason = f"node '{target_node}' is known but offline (no heartbeat in the last 30s)"
            else:
                reason = f"node '{target_node}' does not exist (never sent a heartbeat)"
            online = self.host._online_node_names()
            hint = (
                f"Online nodes: {', '.join(online)}."
                if online
                else "No remote nodes are currently online."
            )
            return {
                "success": False,
                "message": f"Cannot migrate '{agent_name}': {reason}. {hint} "
                f"Migration aborted — the agent stays on "
                f"'{current_node or 'local'}'.",
            }

        if current_node and not is_target_local:
            # ── Remote → Remote migration ────────────────────────────────────
            # The source node still has the agent's compiled code and state;
            # it does the heavy lifting via its own _migrate_agent handler.
            # Main needs to update BOTH nodes' desired_state retained messages
            # so neither tries to re-spawn the agent in the wrong place after
            # a restart: source must forget the agent, target must remember it.
            logger.info(
                "[%s] Migrating %r from node %r → %r",
                self.host.name,
                agent_name,
                current_node,
                target_node,
            )
            # Routed through main, in two legs: ask the source to hand the
            # agent back (the `@main` machinery), then place it on the target
            # ourselves. The source no longer publishes to another node's spawn
            # topic -- that was lateral remote code execution, and the ACL that
            # closes it forbids the write anyway.
            return_token = secrets.token_hex(8)
            self.pending_returns[return_token] = {
                "agent_name": agent_name,
                "from_node": current_node,
                "target_node": target_node,
                "started_at": time.time(),
            }
            self._save_pending()
            await self.host._mqtt_publish(
                f"nodes/{current_node}/migrate",
                {"name": agent_name, "target_node": "@main", "return_token": return_token},
                qos=1,
            )
            # Nothing is pre-staged. The registry and both nodes' desired state
            # move only once the target says the agent started -- until then the
            # agent is stopped but intact on the source, which is what a
            # rollback needs.
            msg = (
                f"Migrating '{agent_name}' from '{current_node}' → '{target_node}' "
                f"(via main; waiting for the source to hand it over)."
            )
            logger.info("[%s] %s", self.host.name, msg)
            return {"success": True, "message": msg}

        if current_node and is_target_local:
            # ── Remote → Local migration ─────────────────────────────────────
            # Always use the '@main' sentinel mechanism so the remote node
            # ships its persistent state (conversation history, counters,
            # calibration, etc.) back to main BEFORE the agent is stopped.
            #
            # Earlier versions had a "fast path" when main already had the
            # code in its spawn registry — it just sent a plain stop and
            # re-spawned locally, but that silently lost ALL of the agent's
            # accumulated memory. For LLM-based agents like chat-agent this
            # was particularly bad: every migrate-back wiped their history.
            #
            # The @main path is slightly slower (one MQTT round-trip) but
            # always correct. The state_return listener handles the spawn
            # once the remote node replies.
            logger.info(
                "[%s] Migrating %r from node %r → local (via @main sentinel; %s)",
                self.host.name,
                agent_name,
                current_node,
                "spawn-registry code available as fallback"
                if have_code
                else "no local code, fully remote-driven",
            )
            return_token = secrets.token_hex(8)
            # Stash the token so the listener knows this return is ours
            # and not from some other concurrent migration.
            self.pending_returns[return_token] = {
                "agent_name": agent_name,
                "from_node": current_node,
                "started_at": time.time(),
            }
            self._save_pending()
            await self.host._mqtt_publish(
                f"nodes/{current_node}/migrate",
                {"name": agent_name, "target_node": "@main", "return_token": return_token},
                qos=1,
            )
            msg = (
                f"Migration of '{agent_name}' from '{current_node}' → local "
                f"initiated (waiting for state from remote node)."
            )
            logger.info("[%s] %s", self.host.name, msg)
            return {"success": True, "message": msg}

        # ── Local → Remote migration ─────────────────────────────────────
        # Requires the agent to be running locally. If it isn't, the
        # spawn-registry config would also be useless (no code shipped
        # over MQTT) so error early.
        if not local_alive and not have_code:
            return {
                "success": False,
                "message": f"Agent '{agent_name}' not running locally and "
                f"no config in registry — cannot migrate.",
            }

        logger.info(
            "[%s] Migrating LOCAL agent %r → remote node %r",
            self.host.name,
            agent_name,
            target_node,
        )

        # Snapshot the local agent's persisted state before stopping it.
        # Only JSON-serialisable keys survive the MQTT trip.
        initial_state: dict[str, Any] = {}
        if self.host._registry:
            local = self.host._registry.find_by_name(agent_name)
            if local and hasattr(local, "_persistence_api") and local._persistence_api:
                try:
                    raw = local._persistence_api.all()
                    dropped = []
                    for k, v in raw.items():
                        try:
                            json.dumps(v)
                            initial_state[k] = v
                        except (TypeError, ValueError):
                            dropped.append(k)
                    if dropped:
                        logger.warning(
                            "[%s] Local→remote migrate %r: dropping non-JSON state keys %s",
                            self.host.name,
                            agent_name,
                            dropped,
                        )
                    if initial_state:
                        logger.info(
                            "[%s] Carrying %s state key(s) from local to %r: %s",
                            self.host.name,
                            len(initial_state),
                            target_node,
                            list(initial_state.keys()),
                        )
                except Exception as e:
                    logger.warning(
                        "[%s] Could not snapshot local state for %r: %s",
                        self.host.name,
                        agent_name,
                        e,
                    )

        # Snapshot the live topic contract from the TopicBus, then merge it
        # into the config we ship to the remote node. The spawn registry
        # only has what was REQUESTED at spawn time, but the local agent
        # may have learned new topics at runtime (via publish/subscribe
        # auto-registration or declare_contract). Without this merge those
        # topics would be lost across the migration and the remote agent
        # would publish a manifest missing them — breaking auto-wiring.
        live_contract: dict[str, Any] = {}
        try:
            from ...core.topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus:
                c = bus.registry.get(agent_name)
                if c is not None:
                    live_contract = {
                        "publishes": list(c.publishes or []),
                        "subscribes": list(c.subscribes or []),
                        "triggers_when": dict(c.triggers_when or {}),
                        "produces_schema": dict(c.produces_schema or {}),
                        "consumes_schema": dict(c.consumes_schema or {}),
                    }
                    # observed_samples is a separate field on the contract
                    if hasattr(c, "observed_samples") and c.observed_samples:
                        live_contract["observed_samples"] = dict(c.observed_samples)
                    logger.info(
                        "[%s] Captured live contract for %r: pub=%s sub=%s",
                        self.host.name,
                        agent_name,
                        live_contract["publishes"],
                        live_contract["subscribes"],
                    )
        except Exception as _e:
            logger.debug("[%s] Could not capture live contract: %s", self.host.name, _e)

        # Stop the local instance, and keep everything it persisted.
        #
        # `initial_state` above is the copy being shipped out; this one is what
        # the migration comes back to if the target never confirms. Purging it
        # here would put the agent nowhere the moment a spawn failed on the
        # node -- the same window the other legs close by deleting only on the
        # ack -- so it is purged there instead, in `_purge_local_source`.
        stopped: Any | None = None
        if self.host._registry:
            local = self.host._registry.find_by_name(agent_name)
            if local:
                try:
                    await self.host._registry.unregister(local.actor_id)
                    await local.stop()
                    self.host._agent_manifests.pop(agent_name, None)
                    stopped = local
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.warning(
                        "[%s] Could not stop local %r: %s",
                        self.host.name,
                        agent_name,
                        e,
                    )

        # Update config with new node target and inject captured state +
        # live contract data. Live values take precedence over stale spawn
        # config values (e.g. a topic the local agent actually published
        # to is more authoritative than what was declared at spawn time).
        new_config = dict(config or {})
        new_config.setdefault("name", agent_name)
        new_config["node"] = target_node
        new_config.pop("replace", None)
        if initial_state:
            new_config["_initial_state"] = initial_state
        for k, v in live_contract.items():
            if v:  # don't overwrite with empty values
                new_config[k] = v

        token = secrets.token_hex(8)
        new_config["_migration_token"] = token
        self.pending_spawns[token] = {
            "agent_name": agent_name,
            "from_node": LOCAL_SOURCE,
            "target_node": target_node,
            "config": new_config,
            "local_actor": stopped,
            "started_at": time.time(),
        }
        self._save_pending()
        # Nothing is committed yet: the registry still places this agent here,
        # so a restart before the ack brings it back rather than losing it.
        await self.host._spawn_remote(new_config, target_node, save=False)
        msg = (
            f"Migrating '{agent_name}' from 'local' → '{target_node}' "
            f"(waiting for it to confirm it started)."
        )
        logger.info("[%s] %s", self.host.name, msg)
        return {"success": True, "message": msg}

    async def update_desired_state(
        self, node: str, new_config: dict[str, Any] | None = None, remove_name: str | None = None
    ) -> None:
        """Maintain nodes/{node}/desired_state as a retained MQTT message containing
        ALL agents that should run on this node. The runner reads this on startup
        and reconciles — spawning missing agents, ignoring already-running ones.
        """
        if self.host is None:
            return
        # Build desired state from spawn registry filtered to this node
        reg = self.host._get_spawn_registry()
        agents = {name: cfg for name, cfg in reg.items() if cfg.get("node", "").strip() == node}

        # Apply pending change before publishing. Stripped, because this message
        # is retained: the runner reconciles from it on every reboot, and a
        # migration snapshot left in it would be re-applied each time.
        if new_config:
            agents[new_config["name"]] = without_transient_keys(new_config)
        if remove_name:
            agents.pop(remove_name, None)

        # The spawn registry holds CLEAN configs (no synthesized bridge code).
        # But the runner that consumes desired_state on reboot will compile
        # whatever code field it finds and call handle_task() on it. So pass
        # every config through _inject_llm_bridge_code() here, which is a
        # no-op for non-llm-type agents and idempotent if code is already
        # present. Without this, restarting a remote node would silently
        # bring back all LLM agents in a non-functional state — same as the
        # original "no handle_task" bug, just delayed by one reboot.
        wire_agents = [self.host._inject_llm_bridge_code(cfg) for cfg in agents.values()]

        await self.host._mqtt_publish(
            f"nodes/{node}/desired_state",
            {"node": node, "agents": wire_agents, "timestamp": time.time()},
            retain=True,
            qos=1,
        )
        logger.info("[%s] Desired state for %r: %s", self.host.name, node, list(agents.keys()))
