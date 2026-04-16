# Pipelines

A pipeline is a set of persistent agents that react to events automatically — no user interaction required after setup. Describe what you want in plain language and Wactorz builds and wires the agents for you.

## Overview

Pipelines are created by **PlannerAgent**, which is spawned automatically whenever MainActor classifies a request as `PIPELINE`. The planner queries your Home Assistant entities, generates a plan, spawns the required agents, and registers a rule that persists across restarts.

```
You say:  "notify me on Discord when the front door opens"

MainActor  →  classifies as PIPELINE
           →  spawns PlannerAgent

PlannerAgent  →  queries home-assistant-agent for entity list
              →  LLM generates a plan (JSON array of agent specs)
              →  spawns agents from plan
              →  saves rule to MainActor's registry
              →  exits

Agents run indefinitely, reacting to events via MQTT
```

---

## Creating a pipeline

Just describe what you want. The intent classifier recognises pipeline requests from natural language:

```
"send me a Discord message if the lamp is on"
"turn on the hallway light when motion is detected"
"notify me on Telegram if someone is seen on the webcam"
"turn off all lights every day at midnight"
"if the temperature goes above 28 degrees, turn on the fan"
```

If you have a Discord webhook stored, the planner injects it automatically. Store one with:

```
/webhook discord https://discord.com/api/webhooks/...
```

> **💡 Entity resolution** — The planner fetches all 280+ entities from your HA instance before generating code, so you can refer to devices by their friendly name — "the living room lamp", "front door sensor" — and the planner maps them to real entity IDs.

---

## Canonical patterns

PlannerAgent uses five canonical wiring patterns. Every pipeline request maps to one of these.

---

### Pattern 1 — HA sensor → HA action

A Home Assistant sensor triggers a Home Assistant service call — e.g. motion sensor turns on a light, door sensor locks a switch, temperature sensor activates AC.

Because HA state is nested inside `new_state.state` and the ha_actuator agent can only filter top-level payload keys, this pattern requires a two-agent setup:

```
Agent 1  (dynamic)  name: <slug>-state-filter
  subscribe to homeassistant/state_changes/#
  filter by entity_id in payload
  check new_state["state"] against condition
  if met: publish custom/triggers/<slug> {"triggered": true}

Agent 2  (ha_actuator)  name: <slug>-actuator
  mqtt_topics: ["custom/triggers/<slug>"]
  detection_filter: {"triggered": true}
  actions: [HA service call with real entity_id]
```

#### Example

```
"turn on the hallway light when motion is detected in the hallway"
```

---

### Pattern 2 — HA sensor → notification

A Home Assistant state change triggers a notification — Discord, Telegram, or any HTTP webhook. This is the simplest real-world pattern: one dynamic agent handles both the state subscription and the HTTP call.

```
Agent 1  (dynamic)  name: <slug>-notify
  async def setup(agent):
      async def on_state(payload):
          if payload.get("entity_id") != "sensor.front_door": return
          if payload.get("new_state", {}).get("state") != "on": return
          async with httpx.AsyncClient() as c:
              await c.post(webhook_url, json={"content": "Front door opened!"})
      agent.subscribe("homeassistant/state_changes/#", on_state)
```

#### Examples

```
"send me a Discord message if the lamp is on"
"notify me on Telegram when the washing machine finishes"
```

> **ℹ Wildcard subscription** — Always subscribe to `homeassistant/state_changes/#` and filter by `entity_id` in the payload. This works regardless of whether `HA_STATE_BRIDGE_PER_ENTITY` is on or off.

---

### Pattern 3 — Webcam / camera detection → HA action

Object detection on a local webcam triggers a Home Assistant service call — e.g. person detected unlocks the door, cat detected turns on the pet feeder.

```
Agent 1  (dynamic)  name: <slug>-camera-detect
  setup(): load YOLO model, open camera
  process(): capture frame, run inference
             publish custom/detections/<slug>
             {"detected": bool, "target": "person", "objects": [...]}
  poll_interval: 1s
  install: ultralytics, opencv-python

Agent 2  (ha_actuator)  name: <slug>-actuator
  mqtt_topics: ["custom/detections/<slug>"]
  detection_filter: {"detected": true}
  actions: [HA service call]
```

#### Example

```
"unlock the front door when a person is detected on the webcam"
```

---

### Pattern 4 — Webcam / camera detection → notification

Same detection agent as Pattern 3, but the second agent sends a notification instead of calling an HA service.

```
Agent 1  (dynamic)  — same as Pattern 3 camera agent

Agent 2  (dynamic)  name: <slug>-notify
  setup(): subscribe to custom/detections/<slug>
           when detected=true: POST to webhook
```

#### Example

```
"notify me on Discord if a person is seen on the front camera"
```

---

### Pattern 5 — Timer → HA action

A scheduled trigger fires a Home Assistant service call at a fixed time or interval.

```
Agent 1  (dynamic)  name: <slug>-timer
  process(): check current time against schedule
             if match: publish custom/triggers/<slug> {"triggered": true}
  poll_interval: 60s

Agent 2  (ha_actuator)  name: <slug>-actuator
  mqtt_topics: ["custom/triggers/<slug>"]
  detection_filter: {"triggered": true}
  actions: [HA service call]
```

#### Examples

```
"turn off all lights every day at midnight"
"turn on the coffee maker at 07:30 on weekdays"
```

---

## Managing pipelines

Every pipeline is saved as a rule in MainActor's spawn registry. Rules persist across restarts — agents are automatically re-spawned when Wactorz starts.

#### List active rules

```
/rules
```

```
Active pipeline rules (2):

🟢 [a1b2c3d4] — send me a Discord message if the lamp is on
   agents  : lamp-on-discord-notify
   created : 2026-03-24 19:36

🟢 [e5f6a7b8] — turn on hallway light when motion detected
   agents  : motion-hallway-state-filter, motion-hallway-actuator
   created : 2026-03-25 11:14

To delete a rule: /rules delete <rule_id>
```

#### Delete a rule

```
/rules delete a1b2c3d4
```

This stops all agents associated with the rule and removes it from the spawn registry. The agents will not be re-spawned on next restart.

#### Check agent status

```
@lamp-on-discord-notify {"action": "status"}
```

---

## Memory and webhooks

The planner reads from MainActor's memory when generating pipeline code. Two things are particularly useful to store before requesting a pipeline:

#### Webhook URLs

Store notification destinations once — the planner injects them automatically into every generated notification agent.

```
/webhook discord  https://discord.com/api/webhooks/...
/webhook telegram https://api.telegram.org/bot.../sendMessage
```

#### User facts

MainActor extracts facts from conversation automatically (HA URLs, entity IDs, preferences). You can also manage them explicitly:

```
/memory                    — view stored facts
/memory forget ha_url      — remove a specific fact
/memory clear              — wipe everything
```

---

## How it works internally

When MainActor classifies a message as `PIPELINE` it spawns a short-lived **PlannerAgent** that:

1. Sends a `list_entities` task to `home-assistant-agent` and waits for the full entity list (up to 180 s)
2. Builds a prompt containing the entity list, available agent types, canonical patterns, stored webhook URLs, and the user's request
3. Calls the LLM once to produce a JSON plan — an array of agent specs, each with `name`, `type`, `code` (for dynamic agents) or `actions` (for ha_actuator agents)
4. Spawns each agent from the plan via `DynamicAgent`
5. Sends a `save_pipeline_rule` message to MainActor with the list of agent names
6. Exits — the spawned agents run indefinitely from this point

> **⚠ Planner hallucination** — If the planner's response looks like a tool call (e.g. `<tool_call>agent.send_to...</tool_call>`) with an instant perfect response, it is the LLM hallucinating — not a real agent interaction. Real pipeline creation takes 5–15 seconds and produces log lines showing agents being compiled and registered.

#### Agent types the planner can generate

| Type | Description | When used |
|------|-------------|-----------|
| `dynamic` | Full Python code string compiled at runtime by DynamicAgent | Any custom logic — MQTT subscriptions, HTTP calls, camera inference, timers |
| `ha_actuator` | Declarative: MQTT topic + detection filter + list of HA service calls | When the only job is to call an HA service when a payload matches |
