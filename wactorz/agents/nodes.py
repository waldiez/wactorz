"""What main knows about remote nodes, and the questions it answers about them.

A node reports itself by heartbeat: who it is, which agents it is running, and
some system metrics. Everything here reads that one dict — whether a node counts
as up, which ones do, what to show, and which node is running a given agent.

Freshness is one constant rather than a number written at each call site. The
four readers have to agree: a node that the listing calls online while the
migration check calls it gone is a node nothing can be done with, and the
disagreement would only appear at the boundary.

Deliberately knows nothing about MQTT, the registry or the actor system. It is a
dict of heartbeats with questions attached, so it can be tested as one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from ..core.mqtt import mqtt_client
from .hosts import NodeHost
from .manifests import ManifestRegistry

logger = logging.getLogger(__name__)

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0

#: Consecutive heartbeats an agent must be missing from before it counts as
#: gone rather than late.
#:
#: One miss is a gap — a slow node, a dropped packet, a heartbeat that crossed
#: a restart. Acting on the first one deletes agents that are alive, and a
#: deletion here is not reversible: the spawn registry entry goes with it.
VANISH_MISS_THRESHOLD = 3

#: How recently a node must have reported to count as online.
#:
#: Short on purpose: this drives the indicator a person is looking at, so a node
#: that has gone away should say so quickly. It is not the window for acting on
#: a node's absence — deciding its agents are gone waits considerably longer, so
#: a brief network gap costs a grey dot rather than a deletion.
ONLINE_WINDOW_S = 30.0

#: How long a node may stay silent before its agents are treated as lost.
#:
#: Longer than the window above on purpose. That one drives the indicator in the
#: dashboard, where being quick to say "offline" costs nothing; this one deletes
#: agents, where being quick would turn a network blip into a resurrection loop.
OFFLINE_GRACE_S = 90.0

#: How often the watcher looks for nodes that have gone quiet.
OFFLINE_CHECK_INTERVAL_S = 15.0


def remote_actor_id(agent_name: str) -> str:
    """The actor id a remote agent has, derived from its name.

    Deterministic so main and the node arrive at the same id without either
    telling the other — the node builds it the same way for `_RemoteAgent`.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.actor.{agent_name}"))


class NodeManager:
    """The known remote nodes, keyed by name.

    Each entry is what the node last sent: ``last_seen``, ``agents``, and
    whatever system metrics it included. Entries are written by the heartbeat
    listener and read from here.
    """

    def __init__(
        self, host: NodeHost | None = None, manifests: ManifestRegistry | None = None
    ) -> None:
        self.host = host
        self.known: dict[str, dict[str, Any]] = {}
        #: Consulted before filling a gap: a manifest, when one has arrived, is
        #: better than anything a spawn config can say.
        self.manifest_registry = manifests if manifests is not None else ManifestRegistry(host)
        #: (node, agent) -> consecutive heartbeats that agent has been missing.
        self.agent_misses: dict[tuple[str, str], int] = {}

    def list_nodes(self) -> list[dict[str, Any]]:
        """Every known node, with its age resolved to an `online` flag.

        The key names are a contract beyond this module: the dashboard reads
        them off the wire, so a renamed or dropped key is invisible from here
        and visible there.
        """
        now = time.time()
        return [
            {
                "node": name,
                "agents": info.get("agents", []),
                "last_seen": info.get("last_seen", 0),
                "online": self._is_fresh(info, now),
                "pid": info.get("pid"),
                "uptime_s": info.get("uptime_s"),
                "cpu_pct": info.get("cpu_pct"),
                "mem_used_mb": info.get("mem_used_mb"),
                "mem_free_mb": info.get("mem_free_mb"),
            }
            for name, info in self.known.items()
        ]

    def is_online(self, node_name: str) -> bool:
        """Whether `node_name` has reported inside the window.

        A node nobody has heard of is offline rather than an error: callers ask
        about names that came from a user or a stale registry entry.
        """
        info = self.known.get(node_name)
        return bool(info) and self._is_fresh(info, time.time())

    def online_names(self) -> list[str]:
        """The online nodes, sorted — this reaches a person in an error message."""
        return sorted(name for name in self.known if self.is_online(name))

    def running_agent(self, name: str) -> str:
        """The online node running `name`, or "" if none currently claims it.

        Empty string rather than None because callers test it as a string. A
        node outside the window does not claim its agents, which is what lets a
        migration proceed rather than refusing on behalf of a node that is gone.
        """
        now = time.time()
        for node_name, info in self.known.items():
            if self._is_fresh(info, now) and name in info.get("agents", []):
                return node_name
        return ""

    def running_agents(self) -> set[str]:
        """Every agent name an online node claims to be running.

        A node outside the window contributes nothing, so an agent counts as
        remote only while something is still reporting it.
        """
        now = time.time()
        return {
            agent
            for info in self.known.values()
            if self._is_fresh(info, now)
            for agent in info.get("agents", [])
        }

    @staticmethod
    def _is_fresh(info: dict[str, Any], now: float) -> bool:
        """Whether a heartbeat is recent enough to count.

        Missing `last_seen` reads as 0, which is 1970 and always stale — the
        honest answer for a node that has never reported.
        """
        return (now - info.get("last_seen", 0)) < ONLINE_WINDOW_S

    # ── Heartbeats ─────────────────────────────────────────────────────────

    async def heartbeat_listener(self) -> None:
        """Follow node heartbeats and migration results until the actor stops.

        Reconnects on failure, logging the first occurrence of a fault at
        warning and repeats at debug.
        """
        host = self.host
        if host is None:
            return

        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(host._mqtt_broker, host._mqtt_port) as client:
                    await client.subscribe("nodes/+/heartbeat")
                    await client.subscribe("nodes/+/migrate_result")
                    logger.info("[main] Subscribed to node heartbeats.")
                    last_error = None
                    async for message in client.messages:
                        await self.receive_node_message(str(message.topic), message.payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[main] Node heartbeat listener error: %s. Reconnecting in %ss…",
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                else:
                    logger.debug(
                        "[main] Node heartbeat listener still unavailable — retrying in %ss…",
                        int(RECONNECT_DELAY_S),
                    )
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def receive_node_message(self, topic: str, payload: bytes | None) -> None:
        """Route one message from a node to whichever half handles it."""
        try:
            data = json.loads(payload.decode()) if payload else None
        except Exception:
            return
        if not isinstance(data, dict):
            return
        parts = topic.split("/")
        if len(parts) < 3:
            return
        node_name = parts[1]
        if topic.endswith("/heartbeat"):
            await self.receive_heartbeat(node_name, data)
        elif topic.endswith("/migrate_result"):
            self.report_migration(data)

    async def receive_heartbeat(self, node_name: str, data: dict[str, Any]) -> None:
        """Take one heartbeat: prune what vanished, record the node, fill gaps."""
        agents = data.get("agents", [])
        previous = set(self.known.get(node_name, {}).get("agents", []))
        await self._prune_vanished(node_name, set(agents), previous)
        self.known[node_name] = {
            "last_seen": time.time(),
            "agents": agents,
            "node_id": data.get("node_id", ""),
            "pid": data.get("pid"),
            "uptime_s": data.get("uptime_s"),
            "cpu_pct": data.get("cpu_pct"),
            "mem_used_mb": data.get("mem_used_mb"),
            "mem_free_mb": data.get("mem_free_mb"),
        }
        self._bootstrap_contracts(node_name, agents)
        self._touch_monitor(agents)

    def agents_to_prune(self, node_name: str, current: set[str], previous: set[str]) -> list[str]:
        """Registry agents on this node that have now missed enough heartbeats.

        Presence resets the count, absence increments it. An agent that has not
        appeared yet is never started on — a freshly spawned one is absent
        because its first heartbeat is still coming, not because it died. An
        agent the registry now places elsewhere has migrated, and is not this
        node's to lose.
        """
        host = self.host
        if host is None:
            return []
        to_prune: list[str] = []
        for agent_name, cfg in list(host._get_spawn_registry().items()):
            if (cfg.get("node", "") or "").strip() != node_name:
                continue
            key = (node_name, agent_name)
            if agent_name in current:
                self.agent_misses.pop(key, None)
                continue
            if key not in self.agent_misses and agent_name not in previous:
                continue
            misses = self.agent_misses.get(key, 0) + 1
            if misses >= VANISH_MISS_THRESHOLD:
                self.agent_misses.pop(key, None)
                to_prune.append(agent_name)
            else:
                self.agent_misses[key] = misses
        return to_prune

    async def _prune_vanished(self, node_name: str, current: set[str], previous: set[str]) -> None:
        """Delete agents the node has stopped reporting.

        The same cleanup as a manual delete, minus the stop signal — there is
        nothing left on the node to send it to.
        """
        host = self.host
        if host is None:
            return
        for agent_name in self.agents_to_prune(node_name, current, previous):
            logger.warning(
                "[main] Agent %r silently disappeared from node %r (crash/kill suspected)",
                agent_name,
                node_name,
            )
            host._remove_from_spawn_registry(agent_name)
            await host._clear_agent_manifest(agent_name)
            host._record_agent_deletion(
                agent_name,
                reason=f"vanished from node '{node_name}' (crash or external kill)",
            )

    def _bootstrap_contracts(self, node_name: str, agents: list[str]) -> None:
        """Give the planner something for agents whose manifest has not arrived.

        The manifest is authoritative: it carries the agent's real topics and
        schemas. A spawn config only records what was asked for, which may never
        have been true, so it is used where there is nothing better and never
        over a manifest-derived contract.
        """
        from ..core.topic_bus import TopicContract, get_topic_bus

        host = self.host
        if host is None:
            return
        try:
            bus = get_topic_bus()
            if not bus:
                return
            spawn_registry = host._get_spawn_registry()
            for name in agents:
                if bus.registry.get(name) is not None:
                    continue
                if name in self.manifest_registry.manifests:
                    continue
                cfg = spawn_registry.get(name)
                if not cfg:
                    continue
                if not (cfg.get("publishes") or cfg.get("subscribes")):
                    continue
                contract = TopicContract.from_spawn_config(
                    {**cfg, "node": node_name, "actor_id": remote_actor_id(name)}
                )
                bus.register_contract(contract)
                logger.debug(
                    "[main] Bootstrapped TopicContract for %r from spawn config "
                    "(manifest pending).",
                    name,
                )
        except Exception as exc:
            logger.debug("[main] TopicContract bootstrap failed: %s", exc)

    def _touch_monitor(self, agents: list[str]) -> None:
        """Keep the monitor's heartbeat tracker current for remote agents.

        Without this the monitor raises a silence alert for every remote agent,
        since nothing else tells it they are alive.
        """
        host = self.host
        if host is None or not host._registry:
            return
        monitor = host._registry.find_by_name("monitor")
        if not monitor or not hasattr(monitor, "_last_seen"):
            return
        now = time.time()
        for name in agents:
            monitor._last_seen[remote_actor_id(name)] = now

    def report_migration(self, data: dict[str, Any]) -> None:
        """Turn a node's migration result into a notification."""
        host = self.host
        if host is None:
            return
        succeeded = data.get("success", False)
        agent = data.get("agent", "?")
        to_node = data.get("to_node", "?")
        host._queue_notification(
            {
                "_monitor_notification": True,
                "message": (
                    f"Migration of '{agent}' to '{to_node}' succeeded."
                    if succeeded
                    else f"Migration of '{agent}' failed: {data.get('error', '?')}"
                ),
                "severity": "info" if succeeded else "warning",
                "timestamp": time.time(),
            }
        )

    async def _node_offline_watcher(self) -> None:
        """Periodically check for nodes that have gone silent. If a node has not
        sent a heartbeat in OFFLINE_GRACE_S, treat all its agents as gone
        and drop the node from our tracking.

        Uses a longer threshold (90s) than the visual "offline" indicator (30s)
        so brief network blips don't trigger false deletion notes. The visual
        indicator stays at 30s for snappy UX; this watcher waits long enough
        to be sure the node is genuinely down.
        """
        host = self.host
        if host is None:
            return

        while host.state.value not in ("stopped", "failed"):
            try:
                await asyncio.sleep(OFFLINE_CHECK_INTERVAL_S)
                now = time.time()
                # Snapshot to avoid mutation-during-iteration
                stale_nodes = [
                    (name, info)
                    for name, info in list(self.known.items())
                    if (now - info.get("last_seen", 0)) > OFFLINE_GRACE_S
                ]
                if not stale_nodes:
                    continue

                reg = host._get_spawn_registry()
                for node_name, _info in stale_nodes:
                    logger.warning(
                        "[main] Node %r has been silent for >%.0fs — treating as offline",
                        node_name,
                        OFFLINE_GRACE_S,
                    )
                    # Find all agents that belong to this node according to the
                    # spawn registry (the heartbeat's last-known agent list may
                    # be stale).
                    lost = [n for n, cfg in reg.items() if cfg.get("node", "").strip() == node_name]
                    for agent_name in lost:
                        host._remove_from_spawn_registry(agent_name)
                        await host._clear_agent_manifest(agent_name)
                        host._record_agent_deletion(
                            agent_name,
                            reason=f"node '{node_name}' went offline",
                        )
                    # Drop the node from our tracking. If it comes back, the
                    # heartbeat listener will re-add it as a fresh entry.
                    self.known.pop(node_name, None)
                    if lost:
                        host._queue_notification(
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
                logger.warning("[%s] Node offline watcher error: %s", host.name, e)
