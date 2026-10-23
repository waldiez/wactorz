"""The manifests agents publish, and the topic registry built from them.

An agent announces what it publishes and subscribes to as a retained message on
`agents/+/manifest`. For a local agent that is a courtesy — it registers its own
contract directly. For one on another machine it is the only channel: main
learns a remote agent's real topics and schemas here or not at all.

What a manifest says reaches the planner, which writes agents that act with
main's credentials, and any node may publish under any agent's id. So a manifest
counts only from where the agent is: one for main's own agent is read from the
agent itself, never the broker's copy, and one for an agent on a node must be
signed by the node main places it on (see `voices`). The same holds for the data
samples recorded for a remote agent.

Three tables move together, which is why they live in one place: what each agent
declared, which agents publish a given topic, and the TopicBus the planner wires
from. A deletion has to reach all three, or the agent keeps being offered as a
wiring target after it is gone.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ...core.mqtt import (
    SERVER_SESSION_EXPIRY_SECONDS,
    client_id,
    install_id,
    mqtt_client,
    session_kwargs,
    user_properties,
)
from ...core.node_publications import admit
from .voices import placed_node, speaks_for

if TYPE_CHECKING:
    from .hosts import ManifestHost

logger = logging.getLogger(__name__)

#: Where a remote agent's data might turn up. Broad on purpose: an agent picks
#: its own topics, and the point is to learn what it actually publishes.
OBSERVED_TOPIC_PATTERNS = ("agents/+/data/#", "custom/#", "sensors/#")

#: Traffic that is machinery rather than data. Recording it would teach the
#: planner that an agent publishes spawn commands.
INTERNAL_TOPIC_PREFIXES = ("agents/by-name", "nodes/", "main/")

#: How long to wait before reconnecting after the broker goes away.
RECONNECT_DELAY_S = 5.0


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


def _claimed_node(known_nodes: dict[str, dict[str, Any]], name: str, data: dict[str, Any]) -> str:
    """The node an agent main placed nowhere is on: the one it names, or whose heartbeat lists it."""
    named = str(data.get("node") or "").strip()
    if named:
        return named
    return next((node for node, info in known_nodes.items() if name in info.get("agents", [])), "")


def _as_sent(manifest: dict[str, Any]) -> dict[str, Any]:
    """``manifest`` as a subscriber would read it back: through JSON, as it is published."""
    return json.loads(json.dumps(manifest, default=str))


class ManifestRegistry:
    """What every known agent says it publishes and subscribes to."""

    def __init__(self, host: ManifestHost | None = None) -> None:
        self.host = host
        #: agent name -> its latest manifest, including declared schemas.
        self.manifests: dict[str, dict[str, Any]] = {}
        #: topic -> the manifests of the agents publishing it.
        self.topic_registry: dict[str, list[dict[str, Any]]] = {}

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
                async with mqtt_client(
                    host._mqtt_broker,
                    host._mqtt_port,
                    identifier=client_id("srv", install_id(), "manifests"),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    await client.subscribe("agents/+/manifest", qos=1)
                    logger.info("[main] Subscribed to agent manifests.")
                    last_error = None
                    async for message in client.messages:
                        await self.receive_manifest(
                            str(message.topic), message.payload, user_properties(message)
                        )
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

    async def receive_manifest(
        self, topic: str, payload: bytes | None, properties: Mapping[str, str] | None = None
    ) -> None:
        """Take one manifest message, or a tombstone.

        An empty retained payload is how a deleted agent announces itself. It
        has to clear every table, or the agent keeps appearing — reported as not
        running, and never going away. Whether a withdrawal stands is the
        lifecycle's call, made from where the agent runs, so it is not checked
        here.

        ``properties`` are the user properties the message arrived with; None
        for one that did not come off the broker, which has no signature to
        check. See :meth:`_trusted`.
        """
        if not payload:
            await self._forget_agent(self._actor_id_from(topic))
            return
        try:
            data = json.loads(payload)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        trusted = self._trusted(topic, payload, data, properties)
        if trusted is not None:
            self._accept_manifest(trusted)

    def _trusted(
        self,
        topic: str,
        payload: bytes,
        data: dict[str, Any],
        properties: Mapping[str, str] | None,
    ) -> dict[str, Any] | None:
        """The manifest to record for ``data``, or None when it is not the agent's word.

        For main's own agent, what it published itself, whatever arrived: the
        copy on the broker may be anyone's. For an agent on a node, ``data`` as
        long as the node main places it on signed it -- or, for one main placed
        nowhere, the node it names or whose heartbeat lists it, while no other
        node's manifest for it is held.
        """
        host = self.host
        name = str(data.get("name") or "")
        if host is None or not name:
            return data
        local = host._registry.find_by_name(name) if host._registry is not None else None
        if local is not None:
            own = getattr(local, "_published_manifest", None)
            return _as_sent(own) if isinstance(own, dict) else None
        if properties is None:
            return data
        placed = placed_node(host, name)
        node = placed or _claimed_node(host._known_nodes, name, data)
        if not speaks_for(host, node, name):
            logger.warning(
                "[main] Ignoring a manifest for %r from %r: main does not place it there",
                name,
                node or "an unknown node",
            )
            return None
        held = None if placed else self.manifests.get(name)
        if held is not None and held.get("node") not in (None, node):
            logger.warning(
                "[main] Ignoring a manifest for %r from %r: node %r already speaks for it",
                name,
                node,
                held.get("node"),
            )
            return None
        if not admit(node, topic, payload, properties, "an agent manifest"):
            return None
        return {**data, "node": node}

    @staticmethod
    def _actor_id_from(topic: str) -> str:
        """The actor id in `agents/{actor_id}/manifest`, or "" if malformed."""
        parts = topic.split("/")
        return parts[1] if len(parts) > 1 else ""

    async def _forget_agent(self, target_id: str) -> None:
        """Drop an agent from every table it appears in, once its loss is accepted.

        The lifecycle decides whether a withdrawal stands; one it refuses or
        holds leaves the tables as they are, so main goes on offering the
        agent's capabilities while it still runs.
        """
        if not target_id:
            return
        name = next(
            (
                n
                for n, m in self.manifests.items()
                if m.get("actor_id") == target_id or n == target_id
            ),
            "",
        )
        # With nothing in the cache — a withdrawal can arrive before this
        # process ever saw the manifest it withdraws, and after a restart the
        # retained message is gone — the id is enough: it derives from the name
        # and nothing else, so the lifecycle can still match it.
        if not await self._report_withdrawal(target_id, name) or not name:
            return
        self.manifests.pop(name, None)
        for topic, entries in list(self.topic_registry.items()):
            kept = [m for m in entries if m.get("name") != name]
            if kept:
                self.topic_registry[topic] = kept
            else:
                self.topic_registry.pop(topic, None)
        self._drop_contract(name)
        logger.info("[main] Manifest tombstone — removed %r", name)

    async def _report_withdrawal(self, actor_id: str, name: str = "") -> bool:
        """Tell the actor an agent withdrew, and whether the withdrawal stands.

        The name is passed only when this registry happened to hold a manifest
        for it; after a restart it will not, and the id is enough. A host that
        cannot be asked, or fails answering, leaves the withdrawal standing.
        """
        host = self.host
        if host is None:
            return True
        try:
            return await host.agent_withdrew(actor_id, name) is not False
        except Exception as exc:
            logger.warning("[main] Could not act on the withdrawal of %s: %s", actor_id[:8], exc)
            return True

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
        from ...core.topic_bus import TopicContract, get_topic_bus

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
                # What tells the planner apart topics that differ only by an
                # id, such as one button's serial from another's.
                description=str(data.get("description") or ""),
            )
            if hasattr(contract, "observed_samples") and observed:
                contract.observed_samples = dict(observed)
            bus.register_contract(contract)
        except Exception as exc:
            logger.debug("[main] TopicBus register from manifest failed for %r: %s", name, exc)

    @staticmethod
    def _drop_contract(name: str) -> None:
        """Stop the planner seeing a deleted agent as a wiring target."""
        from ...core.topic_bus import get_topic_bus

        try:
            bus = get_topic_bus()
            if bus:
                bus.unregister(name)
        except Exception as exc:
            logger.debug("[main] TopicBus unregister failed for %r: %s", name, exc)

    @property
    def host_name(self) -> str:
        """The owning actor's name, for log lines, or a placeholder without one."""
        return self.host.name if self.host is not None else "main"

    def _producer_of(self, topic: str) -> str | None:
        """The agent the bus says publishes `topic`, or None.

        Best-effort: an answer nobody can give is not a fault, and guessing an
        owner would attribute one agent's fields to another.
        """
        try:
            from ...core.topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus:
                producers = bus.registry.producers_of(topic)
                if producers:
                    return producers[0].name
        except Exception as exc:
            logger.debug("[%s] TopicBus producer lookup failed: %s", self.host_name, exc)
        return None

    def _record_sample(self, agent_name: str, topic: str, payload: dict[str, Any]) -> None:
        """Add one real payload to what `agent_name` is known to publish."""
        try:
            from ...core.topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus:
                contract = bus.registry.get(agent_name)
                if contract:
                    contract.update_observed(topic, payload)
        except Exception as exc:
            logger.debug(
                "[%s] Recording observed sample for %r failed: %s",
                self.host_name,
                agent_name,
                exc,
            )

    def _sample_from(self, message: Any) -> tuple[str, str, dict[str, Any]] | None:
        """The agent, topic and payload a message carries, if it carries one.

        None for everything that is not a data sample: internal machinery, a
        payload that is not a JSON object, or a topic no contract claims.
        """
        topic = str(message.topic)
        if topic.startswith(INTERNAL_TOPIC_PREFIXES):
            return None
        payload = _decoded_json(message.payload)
        if not isinstance(payload, dict):
            return None
        agent_name = self._producer_of(topic)
        if not agent_name or not self._from_producer(agent_name, topic, message):
            return None
        return agent_name, topic, payload

    def _from_producer(self, agent_name: str, topic: str, message: Any) -> bool:
        """Whether a sample may be recorded for ``agent_name``: signed by its node, if it has one.

        A sample for main's own agent is taken as it comes: main's agents do not
        sign, so there is nothing to tell their messages from another's by.
        """
        host = self.host
        if host is None:
            return True
        if host._registry is not None and host._registry.find_by_name(agent_name):
            return True
        contract_node = placed_node(host, agent_name) or _claimed_node(
            host._known_nodes, agent_name, {}
        )
        if not speaks_for(host, contract_node, agent_name):
            return False
        return admit(
            contract_node,
            topic,
            bytes(message.payload or b""),
            user_properties(message),
            "a data sample",
        )

    async def _remote_observed_samples_listener(self) -> None:
        """Record what remote agents actually publish, as they publish it.

        An agent's declared `produces_schema` is written by whoever wrote the
        agent and is often wrong. What arrives on the wire is not, so the
        planner is given the observed field names instead.
        """
        host = self.host
        if host is None:
            return

        while host.state.value not in ("stopped", "failed"):
            try:
                async with mqtt_client(
                    host._mqtt_broker,
                    host._mqtt_port,
                    identifier=client_id("srv", install_id(), "samples"),
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                ) as client:
                    for pattern in OBSERVED_TOPIC_PATTERNS:
                        await client.subscribe(pattern, qos=1)
                    async for message in client.messages:
                        found = self._sample_from(message)
                        if found is not None:
                            self._record_sample(*found)
            except asyncio.CancelledError:
                break
            except Exception:
                if host.state.value not in ("stopped", "failed"):
                    await asyncio.sleep(RECONNECT_DELAY_S)
