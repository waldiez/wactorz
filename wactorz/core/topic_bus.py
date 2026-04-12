"""
TopicBus — Reactive Pub/Sub Coordination Layer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This module is the core of Wactorz's shift from name-based RPC to
topic-based reactive coordination.

ARCHITECTURE OVERVIEW
─────────────────────
OLD (request/response):
  User → Main → decides which agent → sends task → waits for result

NEW (reactive pub/sub):
  Agents declare what data they PRODUCE (publishes) and CONSUME (subscribes).
  The broker becomes the coordination primitive — agents self-wire based on
  data compatibility, not hardcoded names.

  User → Main → publishes intent to shared state topic
  Agent A sees a topic it can handle → reacts → publishes result
  Agent B sees A's result on a topic it watches → reacts further
  Main sees final output on result topic → responds to user

COMPONENTS
──────────
  TopicContract   — what an agent declares it produces and consumes
  TopicRegistry   — global index of all live contracts, queryable by topic
  SharedStateHub  — retained MQTT topics for world state (HA, energy, presence)
  StreamWindow    — sliding time window over a topic stream for temporal reasoning
  TopicBus        — ties everything together, wires agents automatically

TOPIC NAMESPACES
────────────────
  home/state/{domain}/{entity_id}   — HA entity states (retained, updated by bridge)
  home/presence/{zone}              — occupancy/presence per zone (retained)
  home/energy/current               — current energy consumption (retained)
  agents/{name}/data/{key}          — agent-published data (retained world state)
  custom/{agent}/{stream}           — agent-to-agent data streams
  wactorz/intents/{id}              — planner-published task intents
  wactorz/results/{id}              — agent-published results to planner intents

WIRING RULES
────────────
  1. An agent that declares subscribes=["home/state/#"] will receive ALL HA
     state changes without any configuration from the planner.
  2. An agent that declares publishes=["custom/detections/{name}"] automatically
     appears as a data source when another agent queries agent.topics("detection").
  3. The planner uses TopicRegistry to wire agents by matching published topics
     to subscribed topics — zero hardcoding required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ── Topic Contract ─────────────────────────────────────────────────────────────

@dataclass
class TopicContract:
    """
    Declares what an agent produces and consumes via MQTT topics.

    Included in spawn config:
        {
          "name": "person-detector",
          "publishes": ["rpi-kitchen/camera/detections"],
          "subscribes": ["homeassistant/state_changes/#"],
          "triggers_when": {"person_detected": true},
          "produces_schema": {"person_detected": "bool", "confidence": "float"},
          "consumes_schema": {"entity_id": "str", "new_state": "dict"}
        }

    The planner uses this to:
      - Discover what data is available without hardcoding agent names
      - Auto-wire: if agent A publishes X and agent B subscribes to X, connect them
      - Build pipelines by topic compatibility, not by name knowledge

    observed_samples is auto-populated from real publish() calls and contains
    the ACTUAL field names the code uses (e.g. {"temp": "float"} not
    {"temperature": "float"}). This is authoritative over produces_schema
    when wiring consumers.
    """
    name:            str
    publishes:       list[str]       = field(default_factory=list)
    subscribes:      list[str]       = field(default_factory=list)
    triggers_when:   dict            = field(default_factory=dict)
    produces_schema: dict            = field(default_factory=dict)
    consumes_schema: dict            = field(default_factory=dict)
    node:            Optional[str]   = None
    actor_id:        Optional[str]   = None
    timestamp:       float           = field(default_factory=time.time)

    # ── Observed payload schemas ───────────────────────────────────────────
    # Auto-captured from real publish() calls. Maps topic → {fields, example}.
    # Unlike produces_schema (declared by LLM, may use wrong field names),
    # observed_samples reflect what the code ACTUALLY publishes.
    #
    # Example:
    #   {"sensors/data": {
    #       "fields":  {"temp": "float", "humidity": "float"},
    #       "example": {"temp": 30.5, "humidity": 47.7}
    #   }}
    observed_samples: dict           = field(default_factory=dict)

    def __post_init__(self):
        """
        Guard against LLM mistakes:
          - Coerce bare strings to single-element lists
          - Filter out bogus entries like literal "publishes"/"subscribes" that
            leak from LLM code passing kwarg names as values
        """
        if isinstance(self.publishes, str):
            self.publishes = [self.publishes]
        if isinstance(self.subscribes, str):
            self.subscribes = [self.subscribes]
        # Strip entries that are clearly kwarg names, not real topics
        _BOGUS = {"publishes", "subscribes", "publish", "subscribe",
                  "topics", "topic", "produces_schema", "consumes_schema",
                  "schema", "triggers_when", "name", "description", "type"}
        self.publishes  = [t for t in self.publishes  if t not in _BOGUS]
        self.subscribes = [t for t in self.subscribes if t not in _BOGUS]

    def matches_topic(self, topic: str) -> bool:
        """Check if this agent subscribes to a given topic (supports # and + wildcards)."""
        for pattern in self.subscribes:
            if _topic_matches(pattern, topic):
                return True
        return False

    def produces_topic(self, topic: str) -> bool:
        """Check if this agent publishes to a given topic pattern."""
        for pattern in self.publishes:
            if _topic_matches(pattern, topic) or _topic_matches(topic, pattern):
                return True
        return False

    def update_observed(self, topic: str, payload: dict):
        """
        Record the actual field names and types from a real published message.
        Called automatically by _AgentAPI.publish() — agents don't need to
        call this themselves.

        This is what solves "temp" vs "temperature": the schema reflects
        what the code ACTUALLY publishes, not what the LLM declared.
        """
        if not isinstance(payload, dict):
            return
        fields = {
            k: type(v).__name__
            for k, v in payload.items()
            if not k.startswith("_")
        }
        self.observed_samples[topic] = {
            "fields":  fields,
            "example": {k: v for k, v in payload.items()
                        if not k.startswith("_")},
        }

    def to_dict(self) -> dict:
        return {
            "name":             self.name,
            "publishes":        self.publishes,
            "subscribes":       self.subscribes,
            "triggers_when":    self.triggers_when,
            "produces_schema":  self.produces_schema,
            "consumes_schema":  self.consumes_schema,
            "node":             self.node,
            "actor_id":         self.actor_id,
            "timestamp":        self.timestamp,
            "observed_samples": self.observed_samples,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TopicContract":
        return cls(
            name             = d.get("name", ""),
            publishes        = d.get("publishes", []),
            subscribes       = d.get("subscribes", []),
            triggers_when    = d.get("triggers_when", {}),
            produces_schema  = d.get("produces_schema", {}),
            consumes_schema  = d.get("consumes_schema", {}),
            node             = d.get("node"),
            actor_id         = d.get("actor_id"),
            timestamp        = d.get("timestamp", time.time()),
            observed_samples = d.get("observed_samples", {}),
        )

    @classmethod
    def from_spawn_config(cls, config: dict) -> "TopicContract":
        """Extract a TopicContract from a spawn config dict."""
        return cls(
            name            = config.get("name", ""),
            publishes       = config.get("publishes", []),
            subscribes      = config.get("subscribes", []),
            triggers_when   = config.get("triggers_when", {}),
            produces_schema = config.get("produces_schema",
                              config.get("output_schema", {})),
            consumes_schema = config.get("consumes_schema",
                              config.get("input_schema", {})),
            node            = config.get("node"),
        )


# ── MQTT wildcard matching ──────────────────────────────────────────────────────

def _topic_matches(pattern: str, topic: str) -> bool:
    """
    Match an MQTT topic against a pattern with # and + wildcards.
    # matches any number of levels. + matches exactly one level.
    """
    if pattern == topic:
        return True
    p_parts = pattern.split("/")
    t_parts = topic.split("/")
    return _match_parts(p_parts, t_parts)


def _match_parts(p: list[str], t: list[str]) -> bool:
    if not p and not t:
        return True
    if p and p[0] == "#":
        return True
    if not p or not t:
        return False
    if p[0] == "+" or p[0] == t[0]:
        return _match_parts(p[1:], t[1:])
    return False


# ── Topic Registry ─────────────────────────────────────────────────────────────

class TopicRegistry:
    """
    Global index of all live TopicContracts, queryable by topic pattern.

    Agents register their contracts on startup. The planner and other agents
    query the registry to discover what data is available and who produces it —
    without knowing agent names in advance.

    Example queries:
        registry.producers_of("rpi-kitchen/camera/detections")
        → [TopicContract(name="person-detector", ...)]

        registry.consumers_of("homeassistant/state_changes/#")
        → [TopicContract(name="lamp-notifier", ...), ...]

        registry.find_wiring_opportunities()
        → [(producer, consumer, topic), ...]  # auto-wireable pairs
    """

    def __init__(self):
        self._contracts: dict[str, TopicContract] = {}  # name → contract

    def register(self, contract: TopicContract):
        self._contracts[contract.name] = contract
        logger.debug(f"[TopicRegistry] Registered '{contract.name}' | "
                     f"pub={contract.publishes} sub={contract.subscribes}")

    def unregister(self, name: str):
        self._contracts.pop(name, None)

    def get(self, name: str) -> Optional[TopicContract]:
        return self._contracts.get(name)

    def all_contracts(self) -> list[TopicContract]:
        return list(self._contracts.values())

    def producers_of(self, topic: str) -> list[TopicContract]:
        """Find all agents that publish to a given topic."""
        return [c for c in self._contracts.values() if c.produces_topic(topic)]

    def consumers_of(self, topic: str) -> list[TopicContract]:
        """Find all agents that subscribe to a given topic."""
        return [c for c in self._contracts.values() if c.matches_topic(topic)]

    def find_by_capability(self, keyword: str) -> list[TopicContract]:
        """Find contracts whose published topics contain keyword."""
        kw = keyword.lower()
        return [c for c in self._contracts.values()
                if any(kw in t.lower() for t in c.publishes + c.subscribes)
                or kw in c.name.lower()]

    def find_wiring_opportunities(self) -> list[tuple[TopicContract, TopicContract, str]]:
        """
        Find pairs of agents that can be automatically wired together:
        agent A publishes topic X, agent B subscribes to X.
        Returns list of (producer, consumer, matching_topic).
        """
        opportunities = []
        contracts = list(self._contracts.values())
        for producer in contracts:
            for pub_topic in producer.publishes:
                for consumer in contracts:
                    if consumer.name == producer.name:
                        continue
                    if consumer.matches_topic(pub_topic):
                        opportunities.append((producer, consumer, pub_topic))
        return opportunities

    def summary(self) -> dict:
        """Return a human-readable summary of the registry."""
        return {
            "total_agents":    len(self._contracts),
            "total_published": sum(len(c.publishes)  for c in self._contracts.values()),
            "total_subscribed":sum(len(c.subscribes) for c in self._contracts.values()),
            "wiring_pairs":    len(self.find_wiring_opportunities()),
            "agents":          [c.to_dict() for c in self._contracts.values()],
        }

    def to_planner_context(self) -> str:
        """
        Format the registry as context for the planner LLM prompt.
        Shows what data flows are available, who can be wired to whom,
        and the ACTUAL payload schemas observed from real messages.
        """
        if not self._contracts:
            return "No topic contracts registered yet."

        lines = ["LIVE DATA FLOWS (topic contracts):"]
        for c in sorted(self._contracts.values(), key=lambda x: x.name):
            lines.append(f"\n  [{c.name}]" + (f" on {c.node}" if c.node else ""))
            if c.publishes:
                lines.append(f"    publishes : {', '.join(c.publishes)}")
            if c.subscribes:
                lines.append(f"    subscribes: {', '.join(c.subscribes)}")
            if c.produces_schema:
                lines.append(f"    produces  : {c.produces_schema}")
            if c.triggers_when:
                lines.append(f"    triggers  : {c.triggers_when}")
            # ── Observed payload samples (authoritative field names) ────
            if c.observed_samples:
                for topic, info in c.observed_samples.items():
                    fields  = info.get("fields", {})
                    example = info.get("example", {})
                    lines.append(
                        f"    OBSERVED on '{topic}': "
                        f"fields={fields}  example={example}"
                    )

        pairs = self.find_wiring_opportunities()
        if pairs:
            lines.append("\nAUTO-WIREABLE PAIRS (producer → consumer via topic):")
            for prod, cons, topic in pairs[:10]:  # limit for prompt size
                lines.append(f"  {prod.name} → {cons.name}  via {topic}")

        return "\n".join(lines)


# ── Shared State Hub ───────────────────────────────────────────────────────────

class SharedStateHub:
    """
    Maintains retained MQTT topics for shared world state.

    Instead of agents asking each other for state, they read from retained
    topics that are always up to date. Any agent can read current state
    without making a request/response round-trip.

    Retained topic namespaces:
      home/state/{domain}/{entity_id}   — HA entity states
      home/presence/{zone}              — occupancy per zone
      home/energy/current               — energy consumption
      agents/{name}/data/{key}          — agent-published world state
    """

    # Standard shared state topics
    PRESENCE_TOPIC  = "home/presence/{zone}"
    ENERGY_TOPIC    = "home/energy/current"
    HA_STATE_TOPIC  = "home/state/{domain}/{entity_id}"
    AGENT_DATA_TOPIC= "agents/{name}/data/{key}"

    def __init__(self, mqtt_client):
        self._mqtt = mqtt_client
        self._cache: dict[str, Any] = {}  # local cache of retained values

    async def publish_state(self, topic: str, data: Any, retain: bool = True):
        """Publish to a shared state topic (retained by default)."""
        self._cache[topic] = data
        if self._mqtt:
            import json as _json
            payload = _json.dumps(data) if not isinstance(data, (str, bytes)) else data
            await self._mqtt.publish(topic, payload, retain=retain, qos=1)

    async def publish_presence(self, zone: str, present: bool,
                               people: list[str] = None, source: str = ""):
        """Publish occupancy state for a zone."""
        topic = self.PRESENCE_TOPIC.format(zone=zone)
        await self.publish_state(topic, {
            "zone":    zone,
            "present": present,
            "people":  people or [],
            "source":  source,
            "ts":      time.time(),
        })

    async def publish_energy(self, kwh: float, cost_per_hour: float = 0.0,
                             source: str = ""):
        """Publish current energy consumption."""
        await self.publish_state(self.ENERGY_TOPIC, {
            "kwh":           kwh,
            "cost_per_hour": cost_per_hour,
            "source":        source,
            "ts":            time.time(),
        })

    async def publish_ha_state(self, entity_id: str, state: str,
                                domain: str = "", attributes: dict = None):
        """Mirror an HA entity state to a shared retained topic."""
        if not domain:
            domain = entity_id.split(".")[0] if "." in entity_id else "sensor"
        topic = self.HA_STATE_TOPIC.format(domain=domain, entity_id=entity_id)
        await self.publish_state(topic, {
            "entity_id":  entity_id,
            "state":      state,
            "attributes": attributes or {},
            "ts":         time.time(),
        })

    async def publish_agent_data(self, agent_name: str, key: str, data: Any):
        """Publish a named piece of world state from an agent."""
        topic = self.AGENT_DATA_TOPIC.format(name=agent_name, key=key)
        await self.publish_state(topic, data)

    def get_cached(self, topic: str) -> Optional[Any]:
        """Return locally cached value for a topic (may be stale)."""
        return self._cache.get(topic)


# ── Stream Window ──────────────────────────────────────────────────────────────

class StreamWindow:
    """
    Sliding time window over an MQTT topic stream.

    Allows agents to reason about temporal patterns without implementing
    their own ring buffers. The window is updated every time a message
    arrives on the subscribed topic.

    Usage in agent code:
        async def setup(agent):
            agent.state['temp_window'] = agent.window('sensors/temperature', seconds=600)

        async def process(agent):
            w = agent.state['temp_window']
            if w.rising(threshold=3.0):
                await agent.alert('Temperature rising fast!')
            avg = w.mean('value')
    """

    def __init__(self, topic: str, seconds: float = 300, max_size: int = 1000):
        self.topic    = topic
        self.seconds  = seconds
        self.max_size = max_size
        self._buffer: deque = deque(maxlen=max_size)
        self._task: Optional[asyncio.Task] = None

    def _trim(self):
        """Remove entries older than the window duration."""
        cutoff = time.time() - self.seconds
        while self._buffer and self._buffer[0]["_ts"] < cutoff:
            self._buffer.popleft()

    def push(self, payload: Any):
        """Add a new data point to the window."""
        entry = {"_ts": time.time()}
        if isinstance(payload, dict):
            entry.update(payload)
        else:
            entry["value"] = payload
        self._buffer.append(entry)

    def values(self, key: str = "value") -> list:
        """Return all values for a key in the current window."""
        self._trim()
        return [e[key] for e in self._buffer if key in e]

    def latest(self) -> Optional[dict]:
        """Return the most recent entry."""
        self._trim()
        return self._buffer[-1] if self._buffer else None

    def mean(self, key: str = "value") -> Optional[float]:
        """Compute mean of a numeric field over the window."""
        vals = self.values(key)
        return sum(vals) / len(vals) if vals else None

    def min(self, key: str = "value") -> Optional[float]:
        vals = self.values(key)
        return min(vals) if vals else None

    def max(self, key: str = "value") -> Optional[float]:
        vals = self.values(key)
        return max(vals) if vals else None

    def count(self) -> int:
        """Number of data points in the current window."""
        self._trim()
        return len(self._buffer)

    def rising(self, key: str = "value", threshold: float = 1.0) -> bool:
        """True if the field has risen by more than threshold over the window."""
        vals = self.values(key)
        if len(vals) < 2:
            return False
        return (vals[-1] - vals[0]) >= threshold

    def falling(self, key: str = "value", threshold: float = 1.0) -> bool:
        """True if the field has fallen by more than threshold over the window."""
        vals = self.values(key)
        if len(vals) < 2:
            return False
        return (vals[0] - vals[-1]) >= threshold

    def stable(self, key: str = "value", tolerance: float = 0.5) -> bool:
        """True if the field has not varied by more than tolerance over the window."""
        vals = self.values(key)
        if len(vals) < 2:
            return True
        return (max(vals) - min(vals)) <= tolerance

    def absent_for(self, seconds: float) -> bool:
        """True if no data has arrived in the last N seconds."""
        latest = self.latest()
        if latest is None:
            return True
        return (time.time() - latest["_ts"]) >= seconds

    def event_count(self, key: str = None, value: Any = None,
                    seconds: float = None) -> int:
        """Count events matching optional key=value in the last N seconds."""
        self._trim()
        cutoff = time.time() - (seconds or self.seconds)
        count = 0
        for e in self._buffer:
            if e["_ts"] < cutoff:
                continue
            if key is None:
                count += 1
            elif key in e and (value is None or e[key] == value):
                count += 1
        return count

    def start(self, mqtt_broker: str, mqtt_port: int):
        """Start the background MQTT listener for this window."""
        self._task = asyncio.create_task(
            self._listen(mqtt_broker, mqtt_port)
        )
        return self

    async def _listen(self, broker: str, port: int):
        try:
            import aiomqtt
        except ImportError:
            logger.error("[StreamWindow] aiomqtt not installed")
            return
        while True:
            try:
                async with aiomqtt.Client(broker, port) as client:
                    await client.subscribe(self.topic)
                    async for msg in client.messages:
                        try:
                            payload = json.loads(msg.payload.decode())
                        except Exception:
                            payload = {"value": msg.payload.decode()}
                        self.push(payload)
            except asyncio.CancelledError:
                break
            except Exception as e:
                await asyncio.sleep(5)

    def stop(self):
        if self._task:
            self._task.cancel()


# ── Topic Bus ──────────────────────────────────────────────────────────────────

class TopicBus:
    """
    Central coordination hub — ties together TopicRegistry, SharedStateHub,
    and StreamWindow. Injected into ActorSystem at startup.

    Responsible for:
      1. Maintaining the global TopicRegistry
      2. Exposing SharedStateHub for world state
      3. Auto-wiring agents based on topic compatibility
      4. Publishing wiring opportunities to the planner
    """

    def __init__(self, mqtt_client=None, mqtt_broker: str = "localhost",
                 mqtt_port: int = 1883):
        self.registry    = TopicRegistry()
        self.state_hub   = SharedStateHub(mqtt_client)
        self._mqtt_broker = mqtt_broker
        self._mqtt_port   = mqtt_port
        self._mqtt        = mqtt_client

    def register_contract(self, contract: TopicContract):
        """Register an agent's topic contract."""
        self.registry.register(contract)
        self._log_wiring_opportunities(contract)

    def unregister(self, agent_name: str):
        """Remove an agent's contract when it stops."""
        self.registry.unregister(agent_name)

    def make_window(self, topic: str, seconds: float = 300,
                    max_size: int = 1000) -> StreamWindow:
        """Create and start a StreamWindow for an agent."""
        window = StreamWindow(topic, seconds=seconds, max_size=max_size)
        window.start(self._mqtt_broker, self._mqtt_port)
        return window

    def _log_wiring_opportunities(self, new_contract: TopicContract):
        """Log any new wiring opportunities created by this registration."""
        for pub_topic in new_contract.publishes:
            consumers = self.registry.consumers_of(pub_topic)
            for consumer in consumers:
                if consumer.name != new_contract.name:
                    logger.info(
                        f"[TopicBus] Auto-wiring opportunity: "
                        f"{new_contract.name} → {consumer.name} via {pub_topic}"
                    )
        for sub_topic in new_contract.subscribes:
            producers = self.registry.producers_of(sub_topic)
            for producer in producers:
                if producer.name != new_contract.name:
                    logger.info(
                        f"[TopicBus] Auto-wiring opportunity: "
                        f"{producer.name} → {new_contract.name} via {sub_topic}"
                    )

    def summary(self) -> dict:
        return {
            "registry":     self.registry.summary(),
            "mqtt_broker":  self._mqtt_broker,
            "mqtt_port":    self._mqtt_port,
        }

    def to_planner_context(self) -> str:
        """Full context string for the planner LLM prompt."""
        return self.registry.to_planner_context()


# ── Module-level singleton ──────────────────────────────────────────────────────
# Accessed via get_topic_bus() from anywhere in the codebase

_topic_bus: Optional[TopicBus] = None


def init_topic_bus(mqtt_client=None, mqtt_broker: str = "localhost",
                   mqtt_port: int = 1883) -> TopicBus:
    global _topic_bus
    _topic_bus = TopicBus(mqtt_client=mqtt_client,
                          mqtt_broker=mqtt_broker,
                          mqtt_port=mqtt_port)
    return _topic_bus


def get_topic_bus() -> Optional[TopicBus]:
    return _topic_bus