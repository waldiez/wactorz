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
from typing import TYPE_CHECKING, Any, Protocol

from ..core.mqtt import mqtt_client

if TYPE_CHECKING:
    from ..core.actor import ActorState

logger = logging.getLogger(__name__)

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0

#: Identifies the code synthesized for an LLM agent sent out to a node, so it
#: can be dropped when the agent comes back. Hand-written code never has it.
BRIDGE_CODE_MARKER = "Auto-generated LLM bridge"

#: How long a migration's return token stays usable.
#:
#: A node that never answers would otherwise leave its token in the pending map
#: for the life of the process. Five minutes is far longer than a migration
#: takes and short enough that the map stays small.
TOKEN_TTL_S = 300.0

#: Consecutive heartbeats an agent must be missing from before it counts as
#: gone rather than late.
#:
#: One miss is a gap — a slow node, a dropped packet, a heartbeat that crossed
#: a restart. Acting on the first one deletes agents that are alive, and a
#: deletion here is not reversible: the spawn registry entry goes with it.
VANISH_MISS_THRESHOLD = 3


def remote_actor_id(agent_name: str) -> str:
    """The actor id a remote agent has, derived from its name.

    Deterministic so main and the node arrive at the same id without either
    telling the other — the node builds it the same way for `_RemoteAgent`.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.actor.{agent_name}"))


class NodeHost(Protocol):
    """What the node collaborator needs from the actor that owns it.

    Typing-only, in the spirit of `mixins/host.py`: nothing here runs, and the
    listener reads these rather than being handed values because both the state
    and the broker address can change after construction.
    """

    name: str
    state: ActorState
    _mqtt_broker: str
    _mqtt_port: int
    _registry: Any

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]: ...

    def _remove_from_spawn_registry(self, name: str) -> None: ...

    def _record_agent_deletion(self, name: str, reason: str = ...) -> None: ...

    def _restore_earned_trust(self, agent_name: str, local_cfg: dict[str, Any]) -> bool: ...

    async def _spawn_from_config(
        self, config: dict[str, Any], save: bool = ..., *, from_registry: bool = ...
    ) -> Any: ...

    def _queue_notification(self, notice: dict[str, Any]) -> None: ...

    async def _clear_agent_manifest(self, name: str, actor_id: str | None = ...) -> None: ...


#: How recently a node must have reported to count as online.
#:
#: Short on purpose: this drives the indicator a person is looking at, so a node
#: that has gone away should say so quickly. It is not the window for acting on
#: a node's absence — deciding its agents are gone waits considerably longer, so
#: a brief network gap costs a grey dot rather than a deletion.
ONLINE_WINDOW_S = 30.0


class NodeManager:
    """The known remote nodes, keyed by name.

    Each entry is what the node last sent: ``last_seen``, ``agents``, and
    whatever system metrics it included. Entries are written by the heartbeat
    listener and read from here.
    """

    def __init__(self, host: NodeHost | None = None) -> None:
        self.host = host
        self.known: dict[str, dict[str, Any]] = {}
        #: agent name -> its latest manifest, including declared schemas.
        self.manifests: dict[str, dict[str, Any]] = {}
        #: topic -> the manifests of the agents publishing it.
        self.topic_registry: dict[str, list[dict[str, Any]]] = {}
        #: (node, agent) -> consecutive heartbeats that agent has been missing.
        self.agent_misses: dict[tuple[str, str], int] = {}
        #: return token -> the migration main started and is waiting on.
        self.pending_returns: dict[str, dict[str, Any]] = {}

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

    # ── Manifests ──────────────────────────────────────────────────────────

    async def manifest_listener(self) -> None:
        """Follow `agents/+/manifest` and keep the manifest tables current.

        Retained manifests are redelivered the moment this subscribes, so the
        tables repopulate for agents that started before main did.

        Reconnects until the actor stops. The first failure is logged at
        warning and repeats of the same failure at debug: a broker that is down
        stays down, and one line per retry buries the outage that caused them.
        """
        host = self.host
        if host is None:
            return

        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(host._mqtt_broker, host._mqtt_port) as client:
                    await client.subscribe("agents/+/manifest")
                    logger.info("[main] Subscribed to agent manifests.")
                    last_error = None
                    async for message in client.messages:
                        self.receive_manifest(str(message.topic), message.payload)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if host.state.value in ("stopped", "failed"):
                    break
                text = str(exc)
                if text != last_error:
                    logger.warning(
                        "[main] Manifest listener error: %s. Reconnecting in %ss…",
                        exc,
                        int(RECONNECT_DELAY_S),
                    )
                    last_error = text
                else:
                    logger.debug(
                        "[main] Manifest listener still unavailable — retrying in %ss…",
                        int(RECONNECT_DELAY_S),
                    )
                await asyncio.sleep(RECONNECT_DELAY_S)

    def receive_manifest(self, topic: str, payload: bytes | None) -> None:
        """Take one manifest message, or a tombstone.

        An empty retained payload is how a deleted agent announces itself. It
        has to clear every table, or the agent keeps appearing — reported as not
        running, and never going away.
        """
        if not payload:
            self._forget_agent(self._actor_id_from(topic))
            return
        try:
            data = json.loads(payload)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        self._accept_manifest(data)

    @staticmethod
    def _actor_id_from(topic: str) -> str:
        """The actor id in `agents/{actor_id}/manifest`, or "" if malformed."""
        parts = topic.split("/")
        return parts[1] if len(parts) > 1 else ""

    def _forget_agent(self, target_id: str) -> None:
        """Drop an agent from every table it appears in."""
        if not target_id:
            return
        removed = ""
        for name, manifest in list(self.manifests.items()):
            if manifest.get("actor_id") == target_id or name == target_id:
                self.manifests.pop(name, None)
                removed = name
                break
        if not removed:
            return
        for topic, entries in list(self.topic_registry.items()):
            kept = [m for m in entries if m.get("name") != removed]
            if kept:
                self.topic_registry[topic] = kept
            else:
                self.topic_registry.pop(topic, None)
        self._drop_contract(removed)
        logger.info("[main] Manifest tombstone — removed %r", removed)

    def _accept_manifest(self, data: dict[str, Any]) -> None:
        """Record a manifest, replacing whatever that agent said before.

        Replacing rather than appending is what keeps the registry bounded:
        manifests are retained, so every reconnect delivers them all again.
        """
        name = data.get("name", "?")
        published = data.get("publishes", []) or []
        for topic in published:
            entries = self.topic_registry.setdefault(topic, [])
            for i, existing in enumerate(entries):
                if existing.get("name") == name:
                    entries[i] = data
                    break
            else:
                entries.append(data)
        self.manifests[name] = data
        self._register_contract(name, data, published)
        logger.debug("[main] Manifest from %r: %s", name, published)

    def _register_contract(self, name: str, data: dict[str, Any], published: list[str]) -> None:
        """Mirror a manifest into the TopicBus so the planner can wire it.

        A local agent registers its own contract; a remote one lives in another
        process, so this is the only route its topics take. Observed field names
        are folded over the declared schema because they are what the code
        actually puts on the wire.
        """
        from ..core.topic_bus import TopicContract, get_topic_bus

        try:
            bus = get_topic_bus()
            if not bus or not name or name == "?":
                return
            observed = data.get("observed_samples", {}) or {}
            produces = dict(data.get("produces_schema", {}) or {})
            for sample in observed.values():
                if isinstance(sample, dict):
                    produces.update(sample.get("fields") or {})
            contract = TopicContract(
                name=name,
                publishes=list(published),
                subscribes=list(data.get("subscribes", []) or []),
                triggers_when=data.get("triggers_when", {}) or {},
                produces_schema=produces,
                consumes_schema=dict(
                    data.get("consumes_schema", data.get("input_schema", {}) or {})
                ),
                actor_id=data.get("actor_id"),
                node=data.get("node"),
            )
            if hasattr(contract, "observed_samples") and observed:
                contract.observed_samples = dict(observed)
            bus.register_contract(contract)
        except Exception as exc:
            logger.debug("[main] TopicBus register from manifest failed for %r: %s", name, exc)

    @staticmethod
    def _drop_contract(name: str) -> None:
        """Stop the planner seeing a deleted agent as a wiring target."""
        from ..core.topic_bus import get_topic_bus

        try:
            bus = get_topic_bus()
            if bus:
                bus.unregister(name)
        except Exception as exc:
            logger.debug("[main] TopicBus unregister failed for %r: %s", name, exc)

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
                if name in self.manifests:
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

    # ── Bringing an agent home ─────────────────────────────────────────────

    async def state_return_listener(self) -> None:
        """Follow `nodes/+/state_return` until the actor stops."""
        host = self.host
        if host is None:
            return

        last_error: str | None = None
        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(host._mqtt_broker, host._mqtt_port) as client:
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
            logger.exception(
                "[main] Local re-spawn after state_return failed for %r: %s", agent_name, exc
            )
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
