"""Turning a vague request into concrete topics and entities.

A task names things the way a person would - "the door", "temperature". The
planner needs the MQTT topics and Home Assistant entity ids behind them, and
the field names their payloads actually use.
"""

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from ...config import CONFIG
from ...core.actor import ask_through
from ...core.mqtt import mqtt_client
from ..lookup import find_main_actor

if TYPE_CHECKING:
    from .hosts import PlannerHost

    # Typing-only base: it states what the host must provide and is gone
    # at runtime, so the real MRO is exactly what it was.
    _Host = PlannerHost
else:
    _Host = object

logger = logging.getLogger(__name__)


class ContextMixin(_Host):
    """Resolving vague references. Mix into a PlannerAgent host."""

    async def _resolve_data_references(self, task: str) -> tuple[str, str]:
        """Resolve vague data references in a task to concrete MQTT topics or HA entities.

        Examples:
          "log when temperature > 22"
            → finds sensors/test/temperature in TopicRegistry
            → enriches: "log when temperature > 22 [subscribe to: sensors/test/temperature]"

          "alert when motion detected"
            → finds rpi-kitchen/camera/detections in TopicRegistry
            → enriches: "alert when motion detected [subscribe to: rpi-kitchen/camera/detections]"

          "log when temperature > 22"  (no registered topics)
            → falls back to HA entity search
            → finds sensor.living_room_temperature
            → enriches: "log when temperature > 22 [HA entity: sensor.living_room_temperature]"

          "log when temperature > 22"  (ambiguous — multiple sources)
            → returns the task unchanged + a note listing candidates
            → planner LLM receives the candidates and picks the best one

        Returns: (enriched_task, resolution_note)
          enriched_task   — task with concrete topic/entity appended as context
          resolution_note — human-readable summary of what was found (shown to user)
        """
        # ── Data concept keywords → search terms ──────────────────────────
        # Maps natural language concepts to TopicRegistry search keywords
        CONCEPT_MAP = {
            r"\btemp(erature)?\b": ["temperature", "temp", "thermal"],
            r"\bhumid(ity)?\b": ["humidity", "humid"],
            r"\bmotion\b": ["motion", "pir", "presence", "detect"],
            r"\bpresence\b": ["presence", "motion", "occupancy"],
            r"\benergy\b": ["energy", "power", "kwh", "watt"],
            r"\bcpu\b": ["cpu", "processor"],
            r"\bmemory\b": ["memory", "ram"],
            r"\bdisk\b": ["disk", "storage"],
            r"\bco2\b": ["co2", "carbon"],
            r"\bair quality\b": ["air", "quality", "voc", "pm25"],
            r"\blight level\b": ["light", "lux", "illumin"],
            r"\bnoise\b": ["noise", "sound", "db"],
            r"\bdetect(ion)?\b": ["detect", "yolo", "camera", "vision"],
            r"\bdoor\b": ["door", "entry", "contact"],
            r"\bwindow\b": ["window", "contact"],
            r"\bwater\b": ["water", "flood", "leak"],
            r"\bgas\b": ["gas", "methane", "smoke"],
            r"\bvoltage\b": ["voltage", "power", "electric"],
        }

        task_lower = task.lower()

        # Find which concepts are mentioned in the task
        matched_concepts = []
        for pattern, keywords in CONCEPT_MAP.items():
            if re.search(pattern, task_lower):
                matched_concepts.extend(keywords)

        if not matched_concepts:
            return task, ""  # No vague data references found

        # A named machine's own readings are read there, whatever else matches:
        # a Home Assistant "processor" sensor measures Home Assistant's host.
        named = [
            node for node in self._node_names() if re.search(rf"\b{re.escape(node)}\b", task_lower)
        ]
        if named and set(matched_concepts) <= MACHINE_CONCEPTS:
            return unresolved(task, matched_concepts)

        # ── Search TopicRegistry first ─────────────────────────────────────
        try:
            from ...core.topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus:
                resolved = describe_topic_source(task, topic_candidates(bus, matched_concepts))
                if resolved:
                    return resolved
        except Exception as e:
            logger.debug("[%s] TopicRegistry search failed: %s", self.name, e)

        # ── Fallback: search HA entities ───────────────────────────────────
        # No registered agent topics found — check if HA has relevant sensors
        try:
            entities_raw = await self._fetch_ha_entities()
            resolved = describe_ha_source(
                task, ha_entity_candidates(entities_raw, matched_concepts)
            )
            if resolved:
                return resolved
        except Exception as e:
            logger.debug("[%s] HA entity search failed: %s", self.name, e)

        # ── Nothing found — return task unchanged with a note ──────────────
        return unresolved(task, matched_concepts)

    def _node_names(self) -> list[str]:
        """The nodes main knows of, lower-cased: those it has heard from and its deploy targets."""
        main = find_main_actor(self._registry) if self._registry else None
        known = set(main.nodes.known) if main is not None else set()
        return sorted({n.lower() for n in known | {t.name for t in CONFIG.deploy_targets}})

    async def _fetch_ha_entities(self) -> list[dict[str, Any]]:
        """Ask home-assistant-agent for its entity list, or an empty list.

        Bounded by a short timeout: this runs while the user waits for a plan,
        so an unresponsive HA agent costs a fallback rather than the request.
        """
        try:
            result = await ask_through(
                self, "home-assistant-agent", {"text": "list entities"}, timeout=8.0
            )
        except (LookupError, RuntimeError, asyncio.TimeoutError):
            return []
        if not isinstance(result, dict):
            return []
        # home-assistant-agent returns {"entities": [...]} — a flat list of
        # entity dicts with entity_id, name, state, etc. NOT a nested
        # devices-to-entities structure.
        entities_raw = (
            result.get("entities", [])
            or result.get("result", [])
            or result.get("devices", [])  # legacy fallback
        )
        return [] if isinstance(entities_raw, str) else entities_raw

    async def _sample_live_topics(self, bus: Any) -> list[str]:
        """Peek at one live MQTT message from each registered publish topic.
        Returns formatted lines with actual field names and an example value.

        This is the fallback when observed_samples haven't been captured yet
        (e.g. the producer started before the schema-capture code was deployed).

        Uses a single MQTT connection with a short per-topic timeout so it
        doesn't block planning. Topics that don't publish within the window
        are silently skipped.
        """
        topics_to_sample = topics_worth_sampling(bus)
        if not topics_to_sample:
            return []

        received = await collect_one_payload_per_topic(
            getattr(self, "_mqtt_broker", "localhost"),
            getattr(self, "_mqtt_port", 1883),
            topics_to_sample,
            self.name,
        )
        sample_lines = describe_samples(bus, received, dict(topics_to_sample))

        if sample_lines:
            logger.info(
                "[%s] Sampled %s live topic(s) for schema introspection",
                self.name,
                len(sample_lines),
            )
        return sample_lines

    async def _sample_named_topics(self, task: str) -> list[str]:
        """One live payload from each MQTT topic ``task`` names, wildcards included.

        A topic is sampled from its contract when an agent declares it; a device
        or a script that publishes without being an agent declares nothing, and
        its topic is known only from the request. Without a sample the model
        writes field names it imagines, and code reading fields no payload has
        does nothing at all.
        """
        named = topics_named_in(task)
        if not named:
            return []
        received = await collect_one_payload_per_topic(
            getattr(self, "_mqtt_broker", "localhost"),
            getattr(self, "_mqtt_port", 1883),
            [(topic, "") for topic in named],
            self.name,
            max_wait=NAMED_TOPIC_WAIT_S,
        )
        return [
            _sample_line(topic, "named in the request", payload)
            for topic, payload in received.items()
        ]


#: How many topics a request names are listened to, and for how long in all:
#: long enough for a sensor that publishes every second or so, short enough not
#: to hold up a plan for a topic nothing publishes on.
NAMED_TOPICS = 3
NAMED_TOPIC_WAIT_S = 4.0

#: An MQTT topic as a request writes it: segments of letters, digits, ``_``,
#: ``-`` and ``$``, a ``+`` standing for one segment and a final ``#`` for any.
_TOPIC_SEGMENT = re.compile(r"^(?:[A-Za-z0-9_$-]+|\+|#)$")
#: What surrounds a topic in a sentence, rather than being part of it.
_AROUND_A_TOPIC = "`'\"()[]{}<>,;:!?."


def topics_named_in(task: str) -> list[str]:
    """The MQTT topics ``task`` names, in order, without repeats.

    A word is one when it has at least two segments and each is a topic segment,
    so a URL, a file name or a Windows path is not. ``#`` may only end one.
    """
    topics: list[str] = []
    for word in task.split():
        topic = word.strip(_AROUND_A_TOPIC)
        segments = topic.split("/")
        if (
            len(segments) < 2
            or not all(_TOPIC_SEGMENT.match(segment) for segment in segments)
            or "#" in segments[:-1]
            or topic in topics
        ):
            continue
        topics.append(topic)
        if len(topics) >= NAMED_TOPICS:
            break
    return topics


def _sample_line(topic: str, source: str, payload: dict[str, Any]) -> str:
    fields = {k: type(v).__name__ for k, v in payload.items() if not k.startswith("_")}
    return f"  Topic: {topic}  ({source})\n    Fields: {fields}\n    Example payload: {payload}"


#: How many topics are listened to for a sample, and how many of them one
#: agent may take: the wait is shared, so a few agents with many topics each
#: must not crowd out the rest.
SAMPLED_TOPICS = 10
SAMPLED_PER_AGENT = 5


def topics_worth_sampling(bus: Any) -> list[tuple[str, str]]:
    """Distinct published topics to sample, each with the agent publishing it.

    At most `SAMPLED_TOPICS` in all and `SAMPLED_PER_AGENT` from any one agent.
    """
    topics: list[tuple[str, str]] = []
    for contract in bus.registry.all_contracts():
        for topic in (contract.publishes or [])[:SAMPLED_PER_AGENT]:
            if len(topics) >= SAMPLED_TOPICS:
                return topics
            if not any(t == topic for t, _ in topics):
                topics.append((topic, contract.name))
    return topics


async def collect_one_payload_per_topic(
    broker: str,
    port: int,
    topics: list[tuple[str, str]],
    log_name: str,
    *,
    max_wait: float | None = None,
) -> dict[str, dict[str, Any]]:
    """First dict payload seen on each topic, within one shared deadline.

    One connection for all topics, and one global timeout rather than a
    per-topic one: the wait is for producers that publish every few seconds, so
    a topic that is idle is skipped rather than holding up planning. A topic may
    be a filter with wildcards; what is kept is then keyed by the topic each
    payload came on. ``max_wait`` overrides the deadline, which otherwise grows
    with the number of topics.
    """
    received: dict[str, dict[str, Any]] = {}

    async def _collect() -> None:
        try:
            async with mqtt_client(broker, port) as client:
                for topic, _ in topics:
                    await client.subscribe(topic)
                async for msg in client.messages:
                    t = str(msg.topic)
                    if t not in received:
                        try:
                            payload = json.loads(msg.payload.decode())
                        except Exception:
                            payload = msg.payload.decode()
                        if isinstance(payload, dict):
                            received[t] = payload
                    if len(received) >= len(topics):
                        return
        except Exception as e:
            logger.debug("[%s] _sample_live_topics connection error: %s", log_name, e)

    if max_wait is None:
        max_wait = min(15.0, 5.0 + 2.0 * len(topics))
    try:
        await asyncio.wait_for(_collect(), timeout=max_wait)
    except asyncio.TimeoutError:
        pass  # whatever arrived before the deadline is enough

    return received


def describe_samples(
    bus: Any, received: dict[str, dict[str, Any]], topic_to_agent: dict[str, str]
) -> list[str]:
    """Render each sample for the prompt, and remember it on its contract."""
    lines = []
    for topic, payload in received.items():
        agent_name = topic_to_agent.get(topic, "?")
        for contract in bus.registry.all_contracts():
            if topic in (contract.publishes or []):
                contract.update_observed(topic, payload)
                break
        lines.append(_sample_line(topic, f"published by {agent_name}", payload))
    return lines


def topic_candidates(bus: Any, matched_concepts: list[str]) -> list[dict[str, Any]]:
    """Registered topics published by agents claiming any of these capabilities."""
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for kw in matched_concepts:
        if kw in seen:
            continue
        seen.add(kw)
        for contract in bus.registry.find_by_capability(kw):
            for topic in contract.publishes:
                if not any(c["topic"] == topic for c in candidates):
                    candidates.append(
                        {
                            "topic": topic,
                            "agent": contract.name,
                            "node": contract.node,
                            "schema": contract.produces_schema,
                            "source": "topic_registry",
                        }
                    )
    return candidates


def describe_topic_source(task: str, candidates: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Task text plus a user-facing note, or None when nothing matched.

    One candidate is resolved outright; several are all handed to the model,
    which knows the user's intent and can pick better than a keyword count.
    """
    if not candidates:
        return None

    if len(candidates) == 1:
        c = candidates[0]
        node_str = f" on {c['node']}" if c.get("node") else ""
        enriched = (
            f"{task} "
            f"[DATA SOURCE: subscribe to MQTT topic '{c['topic']}' "
            f"published by {c['agent']}{node_str}. "
            f"Use agent.subscribe('{c['topic']}', callback) in setup().]"
        )
        note = (
            f"Found `{c['topic']}` from **{c['agent']}**{node_str} — using this as the data source."
        )
        return enriched, note

    sources = ", ".join(f"'{c['topic']}' ({c['agent']})" for c in candidates[:5])
    enriched = (
        f"{task} "
        f"[MULTIPLE DATA SOURCES FOUND: {sources}. "
        f"Pick the most relevant topic based on the user's intent. "
        f"Use agent.subscribe(chosen_topic, callback) in setup().]"
    )
    note = (
        f"Found {len(candidates)} matching topics: "
        + ", ".join(f"`{c['topic']}`" for c in candidates[:3])
        + (" and more" if len(candidates) > 3 else "")
        + " — planner will pick the most relevant."
    )
    return enriched, note


def ha_entity_candidates(
    entities_raw: list[Any], matched_concepts: list[str]
) -> list[dict[str, Any]]:
    """Entities whose id or name mentions any matched concept.

    Accepts the flat entity list home-assistant-agent returns today and the
    older nested device shape, because a node on an older build still answers
    with the latter.
    """
    candidates: list[dict[str, Any]] = []

    def _consider(e: dict[str, Any]) -> None:
        eid = e.get("entity_id", "")
        ename = e.get("friendly_name", "") or e.get("name", "")
        if any(kw in (eid + " " + ename).lower() for kw in matched_concepts):
            candidates.append(
                {
                    "entity_id": eid,
                    "name": ename,
                    "state": e.get("state", ""),
                    "source": "home_assistant",
                }
            )

    for entity in entities_raw:
        if not isinstance(entity, dict):
            continue
        if "entity_id" in entity:
            _consider(entity)
        elif "entities" in entity:
            for sub in entity.get("entities", []):
                _consider(sub)
    return candidates


def describe_ha_source(task: str, candidates: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Task text plus a user-facing note, or None when nothing matched."""
    if not candidates:
        return None

    if len(candidates) == 1:
        c = candidates[0]
        enriched = (
            f"{task} "
            f"[DATA SOURCE: Home Assistant entity '{c['entity_id']}' "
            f"(name: {c['name']}, current state: {c['state']}). "
            f"Subscribe to homeassistant/state_changes/# and filter "
            f"by payload.get('entity_id') == '{c['entity_id']}'. "
            f"The value is in payload.get('new_state', {{}}).get('state').]"
        )
        note = (
            f"No MQTT topic found — using HA entity "
            f"**{c['name']}** (`{c['entity_id']}`, currently: {c['state']})."
        )
        return enriched, note

    sources = ", ".join(f"'{c['entity_id']}' ({c['name']})" for c in candidates[:4])
    enriched = (
        f"{task} "
        f"[MULTIPLE HA ENTITIES FOUND: {sources}. "
        f"Pick the most relevant. Subscribe to homeassistant/state_changes/# "
        f"and filter by entity_id in the payload.]"
    )
    note = (
        f"No MQTT topic found — found {len(candidates)} HA entities: "
        + ", ".join(f"`{c['entity_id']}`" for c in candidates[:3])
        + (" and more" if len(candidates) > 3 else "")
        + " — planner will pick the most relevant."
    )
    return enriched, note


#: What a machine measures about itself: an agent on it reads these with psutil,
#: so no topic or entity has to publish them. Temperature is not among them: the
#: temperature "of" a node is as often a sensor wired to it as the board's own,
#: so it is searched for, and the model is told psutil can read the board's.
MACHINE_CONCEPTS = frozenset({"cpu", "processor", "memory", "ram", "disk", "storage"})


def unresolved(task: str, concepts: list[str]) -> tuple[str, str]:
    """``task`` with a note for the model, and one for the user, when no source was found.

    A machine's own readings have a source wherever an agent runs, so asking
    for one only says where. For anything else the model is told to report
    what is missing rather than plan around it, never to plan an agent that
    asks: the plan is shown before anything runs, which is the place to ask.
    """
    concepts_str = ", ".join(sorted(set(concepts))[:4])
    if set(concepts) <= MACHINE_CONCEPTS:
        enriched = (
            f"{task} [NOTE: {concepts_str} is a reading of a machine. An agent on that machine "
            f"reads it with psutil; see NODES for which machine.]"
        )
        return enriched, f"Read on the machine itself: {concepts_str}."
    enriched = (
        f"{task} [NOTE: No registered MQTT topics or HA entities found matching: {concepts_str}. "
        f"A sensor agent may not have published yet. If nothing listed can provide it, report "
        f"what is missing instead of planning an agent to ask for it.]"
    )
    note = (
        f"No data source found for: {concepts_str}. You may need to name the exact topic or entity."
    )
    return enriched, note
