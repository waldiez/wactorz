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

from ...core.mqtt import client_id, install_id, mqtt_client

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

#: Identifies the code synthesized for an LLM agent sent out to a node, so it
#: can be dropped when the agent comes back. Hand-written code never has it.
BRIDGE_CODE_MARKER = "Auto-generated LLM bridge"


class Migration:
    """Agents in flight between main and a node."""

    def __init__(self, host: MigrationHost | None = None, nodes: NodeReaders | None = None) -> None:
        self.host = host
        #: The live node view. Read for "is the target up" and "where is this
        #: agent" — questions the heartbeat table answers and nothing else can.
        self.host_nodes = nodes
        #: return token -> the migration main started and is waiting on.
        self.pending_returns: dict[str, dict[str, Any]] = {}

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
                ) as client:
                    await client.subscribe("nodes/+/state_return")
                    logger.info("[main] Subscribed to state_return topics.")
                    last_error = None
                    async for message in client.messages:
                        await self.receive_state_return(str(message.topic), message.payload)
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

        await self._respawn_locally(agent_name, from_node, cfg, state)

    def _expire_tokens(self) -> None:
        """Forget tokens whose migration never completed."""
        now = time.time()
        for token, started in list(self.pending_returns.items()):
            if now - started.get("started_at", 0) > TOKEN_TTL_S:
                self.pending_returns.pop(token, None)

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
    ) -> None:
        """Spawn the returned agent here, and say whether it worked.

        A failure is announced rather than logged alone: the agent is now on
        neither machine, so silence loses it.
        """
        host = self.host
        if host is None:
            return
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
            return
        self._announce(f"Migration of '{agent_name}' from '{from_node}' → local succeeded.", "info")

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
            await self.host._mqtt_publish(
                f"nodes/{current_node}/migrate",
                {"name": agent_name, "target_node": target_node},
            )
            # Pre-stage the spawn registry and desired-state updates so a
            # source-node restart between the migrate command and the
            # spawn-completes-on-target window doesn't re-spawn the agent
            # on the wrong side.
            if config:
                updated = dict(config)
                updated["node"] = target_node
                self.host._save_to_spawn_registry(updated)
                # Remove from source's desired_state (forget the agent there)
                # and add to target's desired_state (remember it on arrival).
                await self.update_desired_state(current_node, remove_name=agent_name)
                await self.update_desired_state(target_node, updated)

        elif current_node and is_target_local:
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

        else:
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

            # Stop the local instance, then purge its persistence.
            #
            # We've already snapshotted `initial_state` above — that's the
            # authoritative copy now being shipped to the remote node. The
            # local SQLite rows / pickle / in-memory values are about to become
            # stale ghosts. If the user later migrates the agent back here
            # without those being cleared, they'd merge with the freshly
            # arrived state and produce duplicate conversation entries.
            if self.host._registry:
                local = self.host._registry.find_by_name(agent_name)
                if local:
                    try:
                        await self.host._registry.unregister(local.actor_id)
                        await local.stop()
                        self.host._agent_manifests.pop(agent_name, None)
                        # Wipe SQLite / memory / pickle for this agent. Uses
                        # the same purge primitive as permanent delete — the
                        # difference is the agent is being re-created on the
                        # target node with the snapshot we already have.
                        try:
                            await self.host._purge_local_agent_persistence(local, agent_name)
                        except Exception as e:
                            logger.warning(
                                "[%s] Could not purge local persistence for %r "
                                "after local→remote migration: %s",
                                self.host.name,
                                agent_name,
                                e,
                            )
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

            await self.host._spawn_remote(new_config, target_node, save=True)
            # _spawn_remote already saved the full new_config (including state +
            # live contract). The subsequent _save_to_spawn_registry below would
            # OVERWRITE it with the stale `config`, so skip it for this branch.
            msg = (
                f"Migrating '{agent_name}' from 'local' "
                f"→ '{target_node}'. It will appear in the dashboard shortly."
            )
            logger.info("[%s] %s", self.host.name, msg)
            return {"success": True, "message": msg}

        # NOTE: with the @main sentinel now handling all remote→local cases
        # and the inline remote→remote registry update above, this trailing
        # block is no longer reached in normal flow. Kept as a defensive
        # net for any future path that finishes without updating the
        # spawn registry — the write is idempotent.
        if config:
            updated = dict(config)
            updated["node"] = target_node
            self.host._save_to_spawn_registry(updated)

        msg = (
            f"Migrating '{agent_name}' from '{current_node or 'local'}' "
            f"→ '{target_node or 'local'}'. It will appear in the dashboard shortly."
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

        # Apply pending change before publishing
        if new_config:
            agents[new_config["name"]] = new_config
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
