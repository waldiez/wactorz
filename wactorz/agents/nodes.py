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
from typing import TYPE_CHECKING, Any, Protocol

from ..core.mqtt import mqtt_client

if TYPE_CHECKING:
    from ..core.actor import ActorState

logger = logging.getLogger(__name__)

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0


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
