# MQTT Auto-Wiring in Wactorz

**How agents discover, connect, and validate data flows — without hardcoded names**

---

## The Problem

In a multi-agent system where the LLM generates agent code at runtime, wiring agents together is fragile. The traditional approach — hardcoding agent names in routing logic — breaks the moment an agent is renamed, replaced, or spawned dynamically. Worse, when one LLM writes a producer and another writes a consumer, they frequently disagree on field names: one publishes `{"temp": 30.5}`, the other reads `payload["temperature"]`.

Wactorz solves this with **topic-based auto-wiring**: agents declare what data they produce and consume via MQTT topics, and the system wires them by data compatibility — not by name.

---

## Architecture Overview

```
                    ┌─────────────────────────────────┐
                    │          TopicBus                │
                    │  (singleton, init at startup)    │
                    │                                  │
                    │  ┌───────────────────────────┐   │
                    │  │     TopicRegistry         │   │
                    │  │  name → TopicContract     │   │
                    │  │  + observed_samples       │   │
                    │  └───────────────────────────┘   │
                    │                                  │
                    │  ┌───────────────────────────┐   │
                    │  │    SharedStateHub         │   │
                    │  │  retained MQTT topics     │   │
                    │  └───────────────────────────┘   │
                    │                                  │
                    │  ┌───────────────────────────┐   │
                    │  │   StreamWindow factory    │   │
                    │  │  sliding windows over     │   │
                    │  │  topic streams            │   │
                    │  └───────────────────────────┘   │
                    └────────────┬────────────────────┘
                                 │
           ┌─────────────────────┼─────────────────────┐
           │                     │                     │
    ┌──────▼──────┐      ┌───────▼──────┐      ┌──────▼──────┐
    │  Producer   │      │   Planner    │      │  Consumer   │
    │  Agent      │      │   Agent      │      │  Agent      │
    │             │      │              │      │             │
    │ publish()   │      │ reads        │      │ subscribe() │
    │ → auto-     │      │ contracts +  │      │ → wired by  │
    │   registers │      │ observed     │      │   planner   │
    │   contract  │      │ schemas      │      │   using     │
    │ + captures  │      │ before code  │      │   real      │
    │   schema    │      │ generation   │      │   field     │
    └─────────────┘      └──────────────┘      │   names     │
                                               └─────────────┘
```

---

## Step 1: TopicContract — What an Agent Declares

Every agent that publishes or subscribes to MQTT topics has a `TopicContract` — a dataclass that declares its data interface:

```python
@dataclass
class TopicContract:
    name:             str                    # agent name
    publishes:        list[str]              # topics this agent writes to
    subscribes:       list[str]              # topics this agent reads from
    triggers_when:    dict                   # conditions that trigger action
    produces_schema:  dict                   # declared field names + types
    consumes_schema:  dict                   # expected input field names
    observed_samples: dict                   # AUTO-CAPTURED real payloads
    node:             Optional[str]          # remote node name (if edge)
    actor_id:         Optional[str]          # unique actor ID
```

Contracts are registered in the `TopicRegistry` — a global in-memory index accessible from anywhere via `get_topic_bus().registry`.

### How Contracts Get Registered

Contracts are registered through three paths:

**Path 1 — Implicit via `agent.publish()`:**
The first time a DynamicAgent publishes to a topic, `_AgentAPI.publish()` auto-creates a minimal contract:

```python
# Inside _AgentAPI.publish():
contract = TopicContract(
    name      = self.name,
    publishes = list(self._published_topics | {topic}),
    actor_id  = self.actor_id,
)
if isinstance(data, dict):
    contract.update_observed(topic, data)  # capture real fields
bus.register_contract(contract)
```

**Path 2 — Implicit via `agent.subscribe()`:**
When `agent.subscribe(topic, callback)` is called, the topic is added to the contract's `subscribes` list:

```python
# Inside _AgentAPI.subscribe():
existing = bus.registry.get(self.name)
if existing:
    if topic not in existing.subscribes:
        existing.subscribes.append(topic)
```

**Path 3 — Explicit via `agent.declare_contract()`:**
Agents can declare their full contract in `setup()`:

```python
async def setup(agent):
    agent.declare_contract(
        publishes     = ['custom/detections/camera'],
        subscribes    = ['homeassistant/state_changes/#'],
        triggers_when = {'person_detected': True},
        produces_schema = {'detected': 'bool', 'confidence': 'float'},
    )
```

`declare_contract()` accepts common LLM kwarg variants (`schema` → `produces_schema`, `topics` → `publishes`, etc.) and coerces bare strings to lists.

---

## Step 2: Observed Schema Capture — The Vocabulary Solution

### The Problem in Detail

When the planner asks an LLM to write a producer agent, the LLM might use any reasonable field name:

```python
# Producer (written by LLM call #1):
await agent.publish('sensors/data', {'temp': 30.5, 'humidity': 47.7})
```

Later, when the planner asks the LLM to write a consumer for that same topic, a different LLM call might use different names:

```python
# Consumer (written by LLM call #2):
async def on_message(payload):
    temperature = payload['temperature']  # KeyError! Field is 'temp', not 'temperature'
```

The `produces_schema` declared in the contract doesn't help because it was also written by the LLM — it suffers the same vocabulary problem.

### The Solution: `observed_samples`

Instead of trusting LLM-declared schemas, Wactorz captures the **actual** field names from real published messages:

```python
# TopicContract.update_observed():
def update_observed(self, topic: str, payload: dict):
    fields = {
        k: type(v).__name__
        for k, v in payload.items()
        if not k.startswith("_")
    }
    self.observed_samples[topic] = {
        "fields":  fields,           # {'temp': 'float', 'humidity': 'float'}
        "example": {k: v for ...},   # {'temp': 30.5, 'humidity': 47.7}
    }
```

This is called automatically by `_AgentAPI.publish()` on every publish — no agent code changes needed.

### The Data Flow

```
1. Producer publishes {'temp': 30.5, 'humidity': 47.7}
          │
          ▼
2. _AgentAPI.publish() calls contract.update_observed()
          │
          ▼
3. TopicContract.observed_samples now contains:
   {'sensors/data': {
       'fields':  {'temp': 'float', 'humidity': 'float'},
       'example': {'temp': 30.5, 'humidity': 47.7}
   }}
          │
          ▼
4. TopicRegistry.to_planner_context() includes:
   "OBSERVED on 'sensors/data': fields={'temp': 'float'} example={'temp': 30.5}"
          │
          ▼
5. Planner LLM sees exact field names in its prompt:
   "═══ LIVE TOPIC SAMPLES ═══
    Topic: sensors/data (published by temp-simulator)
      Fields: {'temp': 'float', 'humidity': 'float'}
      Example: {'temp': 30.5, 'humidity': 47.7}
    CRITICAL: Use payload['temp'] — NOT payload['temperature']"
          │
          ▼
6. Consumer code uses correct field names:
   temperature = payload['temp']  ✓
```

### Fallback: Live Topic Sampling

If `observed_samples` is empty (the producer started before the schema-capture code was deployed, or the agent hasn't published yet), the planner falls back to `_sample_live_topics()`:

```python
async def _sample_live_topics(self, bus) -> list[str]:
    # Single MQTT connection subscribes to ALL known publish topics
    # Collects one real message per topic with a global timeout
    # Stores results back into contracts for future calls
```

This method:
1. Gathers all publish topics from all registered contracts
2. Opens one MQTT connection and subscribes to all of them
3. Waits up to 15 seconds total (not per-topic) for messages to arrive
4. Parses each payload, extracts field names and types
5. Stores results back into the contracts via `contract.update_observed()`
6. Returns formatted lines for the LLM prompt

Stale topics (no active publisher) are silently skipped after the timeout.

---

## Step 3: Auto-Wiring Discovery

The `TopicRegistry` can find wiring opportunities — pairs of agents where one publishes a topic that another subscribes to:

```python
def find_wiring_opportunities(self) -> list[tuple]:
    for producer in contracts:
        for pub_topic in producer.publishes:
            for consumer in contracts:
                if consumer.matches_topic(pub_topic):
                    opportunities.append((producer, consumer, pub_topic))
```

MQTT wildcards (`#` and `+`) are supported:

```python
# Producer publishes: 'sensors/kitchen/temperature'
# Consumer subscribes: 'sensors/#'
# → Match! Auto-wiring opportunity detected.
```

### When Wiring is Logged

Every time a new contract is registered, the TopicBus checks for new wiring opportunities and logs them:

```
[TopicBus] Auto-wiring opportunity: temp-simulator → mean-logger via sensors/data
[TopicBus] Auto-wiring opportunity: camera-detect → lamp-actuator via custom/detections/cam
```

This is informational — the TopicBus doesn't force-wire agents. The actual wiring happens through:
1. The planner reading the registry and designing the pipeline
2. Or the user explicitly creating agents that subscribe to matching topics

---

## Step 4: Planner Integration — How It All Comes Together

When the planner receives a pipeline request like *"if temp > 20 turn off the lamp"*, it goes through this sequence:

### 4.1 Topic Resolution

`_resolve_data_references()` scans the user's task for data-related keywords and searches the TopicRegistry:

```python
# User says "temperature" → CONCEPT_MAP matches → search keywords: ["temperature", "temp", "thermal"]
# TopicRegistry.find_by_capability("temp") → finds TopicContract for temp-simulator
# Enriches task: "if temp > 20 turn off lamp [DATA SOURCE: subscribe to 'sensors/data']"
```

If multiple topics match, all candidates are provided to the LLM to pick the most relevant one. If none match, the task is enriched with a note and the planner proceeds anyway (it may create a new producer).

### 4.2 Schema Context Injection

Before generating code, the planner builds schema context from two sources:

**Source A — `observed_samples` on contracts:**

```python
for contract in bus.registry.all_contracts():
    samples = contract.observed_samples or {}
    for topic, info in samples.items():
        # Add to prompt: "Topic: sensors/data Fields: {'temp': 'float'} Example: {'temp': 30.5}"
```

**Source B — Live sampling fallback:**

```python
if not sample_lines:
    sample_lines = await self._sample_live_topics(bus)
```

**Source C — Worker manifests:**

```python
# _discover_workers() includes observed_samples in each worker description
workers.append({
    "name": actor.name,
    "observed_samples": manifest.get("observed_samples", {}),
    ...
})

# _fmt_worker() surfaces them in the prompt:
# "  - temp-simulator (DynamicAgent): Publishes random temp/humidity
#      topic 'sensors/data' payload fields: {'temp': 'float'}  example: {'temp': 30.5}"
```

### 4.3 Code Generation with Real Field Names

The LLM prompt includes all three sources, plus an explicit instruction:

```
═══ LIVE TOPIC SAMPLES (use EXACTLY these field names in code!) ═══
  Topic: sensors/data  (published by temp-simulator)
    Fields: {'temp': 'float', 'humidity': 'float'}
    Example payload: {'temp': 30.5, 'humidity': 47.7}

CRITICAL: When subscribing to a topic listed above, use the EXACT field names
from the sample payload. For example if the sample shows {'temp': 30.5},
use payload['temp'] — NOT payload['temperature']. The field names in the
samples are authoritative.
```

The LLM then generates consumer code with the correct field names:

```python
async def on_temp(payload):
    temp = payload.get('temp', 0)     # ← correct: matches observed schema
    if temp > 20:
        await agent.publish('custom/triggers/lamp-temp', {'triggered': True})
```

### 4.4 Post-Generation Validation

After the LLM generates the plan, `_validate_pipeline_code()` scans each dynamic agent's code for common mistakes:

| Check | Action |
| --- | --- |
| `await agent.subscribe(...)` | Strip the `await` (subscribe is sync) |
| `await agent.persist(...)` | Strip the `await` (persist is sync) |
| `aiomqtt.Client()` | Rewrite to `agent.subscribe()` pattern |
| `httpx.post('/api/services/...')` | Flag — should use `ha_actuator` instead |

---

## Step 5: Runtime — How the Wiring Holds Up

Once agents are spawned and running, the wiring is maintained through several mechanisms:

### Contract Updates on Publish

Every `publish()` call updates the contract's `observed_samples` and re-registers it in the TopicBus. This means:
- If a producer changes its payload format, the contract is updated immediately
- The next time the planner generates a consumer, it sees the new field names
- Existing consumers are NOT automatically updated (they use the field names baked into their code)

### Manifest Propagation

Each agent publishes a retained MQTT manifest at `agents/{id}/manifest` that includes `observed_samples`. This means:
- MainActor's manifest listener picks it up and stores it in `_agent_manifests`
- The planner can query manifests even for agents it didn't spawn
- Schema data survives agent restarts (retained MQTT messages persist in the broker)

### The `/bus` Command

Users can inspect the full wiring state at any time:

```
/bus
```

Output:

```
TopicBus — Reactive Pub/Sub Registry
  agents with contracts : 3
  published topics      : 2
  subscribed topics     : 2
  auto-wiring pairs     : 2

  [temp-simulator]
    publishes : sensors/data
    OBSERVED on 'sensors/data': fields={'temp': 'float', 'humidity': 'float'}
                                example={'temp': 30.5, 'humidity': 47.7}

  [mean-logger]
    subscribes: sensors/data

  [lamp-monitor]
    subscribes: sensors/data, lamp/status

Auto-wiring opportunities:
  temp-simulator → mean-logger   via sensors/data
  temp-simulator → lamp-monitor  via sensors/data
```

---

## Safety Guards

### String-to-List Coercion

LLMs frequently write `publishes="custom/topic"` instead of `publishes=["custom/topic"]`. Iterating a string produces individual characters — so `"custom/topic"` would register 12 single-character "topics" (`c`, `u`, `s`, `t`, ...).

`TopicContract.__post_init__()` catches this:

```python
def __post_init__(self):
    if isinstance(self.publishes, str):
        self.publishes = [self.publishes]
    if isinstance(self.subscribes, str):
        self.subscribes = [self.subscribes]
```

### Bogus Topic Filter

LLMs sometimes pass kwarg names as values: `declare_contract(subscribes="subscribes")`. The `__post_init__` filter strips known bogus entries:

```python
_BOGUS = {"publishes", "subscribes", "publish", "subscribe",
           "topics", "topic", "produces_schema", "consumes_schema",
           "schema", "triggers_when", "name", "description", "type"}
self.publishes  = [t for t in self.publishes  if t not in _BOGUS]
self.subscribes = [t for t in self.subscribes if t not in _BOGUS]
```

### Serialization Roundtrip

`observed_samples` is a proper `dataclass` field (not a monkey-patched attribute), so it survives `to_dict()` → `from_dict()` serialization:

```python
contract = TopicContract(name="test", publishes=["sensors/data"])
contract.update_observed("sensors/data", {"temp": 30.5})

d = contract.to_dict()          # includes observed_samples
c2 = TopicContract.from_dict(d) # preserves observed_samples
assert c2.observed_samples == contract.observed_samples  # ✓
```

---

## End-to-End Example

User says: *"spawn an agent to log the mean of the last 5 temperature values"*

```
1. MainActor classifies intent → PIPELINE
2. PlannerAgent spawned

3. Topic resolution:
   _resolve_data_references() finds "temperature" keywords
   TopicRegistry.find_by_capability("temp")
   → finds TopicContract for 'temp-simulator' publishing 'sensors/data'
   → enriches task: "...  [DATA SOURCE: subscribe to 'sensors/data']"

4. Schema sampling:
   contract.observed_samples['sensors/data'] = {
     'fields': {'temp': 'float', 'humidity': 'float'},
     'example': {'temp': 30.5, 'humidity': 47.7}
   }

5. LLM prompt includes:
   "OBSERVED on 'sensors/data': fields={'temp': 'float', 'humidity': 'float'}"
   "CRITICAL: Use payload['temp'] — NOT payload['temperature']"

6. LLM generates spawn config:
   {
     "name": "temp-mean-logger",
     "type": "dynamic",
     "code": "
       async def setup(agent):
           agent.state['buffer'] = []
           async def on_temp(payload):
               agent.state['buffer'].append(payload.get('temp', 0))  # ← correct field name
               if len(agent.state['buffer']) > 5:
                   agent.state['buffer'] = agent.state['buffer'][-5:]
               if len(agent.state['buffer']) == 5:
                   mean = sum(agent.state['buffer']) / 5
                   await agent.log(f'Mean of last 5: {mean:.2f}°C')
           agent.subscribe('sensors/data', on_temp)
     "
   }

7. _validate_pipeline_code():
   - No 'await agent.subscribe' found (already correct ✓)
   - No raw aiomqtt ✓
   - No direct HA API calls ✓

8. Agent spawned, TopicBus logs:
   [TopicBus] Auto-wiring opportunity: temp-simulator → temp-mean-logger via sensors/data

9. Agent receives messages, computes means:
   [temp-mean-logger] Temperature received: 30.5°C | Buffer: [30.5]
   [temp-mean-logger] Temperature received: 22.1°C | Buffer: [30.5, 22.1]
   ...
   [temp-mean-logger] Mean of last 5: 24.8°C
```

---

## Comparison: Before and After Auto-Wiring

| Aspect | Before (name-based) | After (topic-based) |
| --- | --- | --- |
| **Agent discovery** | Planner must know agent names | Planner queries TopicRegistry by data type |
| **Field name matching** | LLM guesses → frequent KeyError | Real field names captured from live payloads |
| **Adding a new producer** | Must update all consumers | New producer auto-appears in registry |
| **Removing a producer** | Consumers silently break | TopicBus shows which consumers are orphaned |
| **Schema documentation** | Manual, always outdated | Auto-captured from real messages |
| **Multi-node** | Requires name→node mapping | Topics are node-agnostic (MQTT handles routing) |

---

## File Reference

| File | What It Does |
| --- | --- |
| `core/topic_bus.py` | `TopicContract`, `TopicRegistry`, `SharedStateHub`, `StreamWindow`, `TopicBus` |
| `agents/dynamic_agent.py` | `_AgentAPI.publish()` — auto-registers contracts and captures schemas |
| `agents/dynamic_agent.py` | `_AgentAPI.subscribe()` — auto-registers subscription in TopicBus |
| `agents/dynamic_agent.py` | `_AgentAPI.declare_contract()` — explicit contract declaration with kwarg aliases |
| `agents/planner_agent.py` | `_resolve_data_references()` — topic resolution from natural language |
| `agents/planner_agent.py` | `_sample_live_topics()` — fallback MQTT sampling for schema capture |
| `agents/planner_agent.py` | `_decompose_pipeline()` — injects schema context into LLM prompt |
| `agents/planner_agent.py` | `_validate_pipeline_code()` — post-generation code validator |
| `agents/main_actor.py` | `_manifest_listener()` — subscribes to `agents/+/manifest` for schema propagation |
