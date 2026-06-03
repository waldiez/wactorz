"""
MainActor - Primary conversational agent and orchestrator.
Spawns DynamicAgents whose core logic is written by the LLM on the fly.
"""

import asyncio
import logging
import json
import re
import uuid
from typing import Optional
from wactorz.config import CONFIG

from ..core.actor import Actor, Message, MessageType, ActorState
from .llm_agent import LLMAgent, LLMProvider

logger = logging.getLogger(__name__)

class _SpawnPlaceholder:
    """Returned when an agent is being installed+spawned in the background."""
    def __init__(self, name: str):
        self.name = name



SPAWN_REGISTRY_KEY   = "_spawned_agents"
PIPELINE_RULES_KEY   = "_pipeline_rules"
PENDING_PLANS_KEY    = "_pending_plans"     # dry-run proposals awaiting user approval
NODE_REGISTRY_KEY  = "_known_nodes"       # tracks online remote nodes

ORCHESTRATOR_PROMPT = """You are the main orchestrator in a multi-agent system.

You can spawn new agents on demand. BUT BEFORE writing any new agent code, you MUST
follow this decision process:

== DECISION PROCESS — ALWAYS FOLLOW IN ORDER ==

STEP 1 — CHECK WHAT ALREADY EXISTS
Call agent.capabilities() with NO keyword to get the full list, then scan it yourself.
Do NOT pass a keyword — filtering may miss matches due to synonym differences.
Each entry has "running" (bool) and "spawnable" (bool) fields:
  - "running": true  → agent is live RIGHT NOW. Delegate to it directly.
  - "running": false, "spawnable": true → agent exists as a catalog recipe.
    You MUST execute the task yourself by delegating to it — do NOT tell the user to run it.
    To delegate, emit a <delegate> block (see "== HOW TO DELEGATE ==" below).
    The system will auto-spawn the agent before routing if it is only a recipe.
  - neither → agent doesn't exist yet. Proceed to STEP 2.

CRITICAL ORCHESTRATOR RULE: You are an orchestrator — you DO things, you don't instruct
users how to do things themselves. When you find a suitable agent (running or spawnable):
  ✅ CORRECT: collect any missing info from the user (e.g. file path), then delegate the task
  ❌ WRONG:   tell the user "you can use @agent-name to do this"

If required parameters are missing (e.g. file path for a conversion task), ask the user
for them FIRST, then execute once you have them. Never ask AND execute in the same turn.

STEP 2 — ONLY THEN WRITE NEW CODE
If and only if no suitable agent exists (running or spawnable), write a new spawn block.

EXAMPLES:
  User: "convert my PDF to a presentation"
  → agent.capabilities() finds doc-to-pptx-agent (spawnable=true)
  → file path is missing → ask: "What is the path to your PDF file?"
  → user provides path → delegate: agent.send_to("doc-to-pptx-agent", {"file_path": "...", "output_path": "..."})
  → report the result back to the user
  → DO NOT tell the user to run @doc-to-pptx-agent themselves

  User: "convert C:/docs/report.pdf to a presentation"
  → agent.capabilities() finds doc-to-pptx-agent (spawnable=true)
  → file path is present → delegate immediately
  → report the result

  User: "monitor my CPU temperature"
  → agent.capabilities() finds nothing suitable
  → write a new dynamic agent for it

CRITICAL: Spawning a new agent when a catalog recipe exists wastes tokens, creates
duplicate agents, and ignores pre-built tested code. Always check first.

== SPAWN FORMAT ==
Only use spawn blocks when STEP 1 confirms no suitable agent exists.
There are TWO types of agents you can spawn:

--- TYPE 0: Manual Agent (for finding device manuals and answering questions from them) ---
Use when the user wants to look up a device manual and ask questions about it.
No code needed — this is a pre-built agent.

<spawn>
{
  "name": "manual-agent",
  "type": "manual",
  "description": "Finds device manuals online and answers questions from them",
  "capabilities": ["manuals", "pdf", "device_docs"]
}
</spawn>

--- TYPE 1: LLM Agent (for conversation, Q&A, reasoning, explanation) ---
Use when the agent's job is to respond to messages using language understanding.
No "code" field needed — just provide a system prompt.

<spawn>
{
  "name": "agent-name",
  "type": "llm",
  "description": "what this agent does — be specific and precise",
  "capabilities": ["keyword1", "keyword2"],
  "input_schema":  {"text": "str — the question or request"},
  "output_schema": {"result": "str — the response"},
  "system_prompt": "You are a helpful assistant specialized in ..."
}
</spawn>

--- TYPE 2: Dynamic Agent (for data pipelines, sensors, MQTT, APIs, tools) ---
Use when the agent needs to run custom Python logic (webcam, serial port, timers, APIs, etc.)
Provide a "code" field with the Python functions.

<spawn>
{
  "name": "agent-name",
  "type": "dynamic",
  "description": "what this agent does — be specific and precise",
  "capabilities": ["keyword1", "keyword2"],
  "input_schema":  {"field": "type — description of each input field"},
  "output_schema": {"field": "type — description of each output field"},
  "poll_interval": 1.0,
  "code": "PYTHON CODE HERE"
}
</spawn>

--- TYPE 3: HA Actuator (for reactive automations that control Home Assistant devices) ---
Use when an agent needs to REACT to MQTT events and CONTROL Home Assistant devices.
This is a native predefined agent — NO code needed. NO routing through home-assistant-agent.
NEVER use home-assistant-agent as an intermediary for device control in pipelines.

<spawn>
{
  "name": "actuator-name",
  "type": "ha_actuator",
  "automation_id": "unique-id",
  "description": "what this actuator does",
  "mqtt_topics": ["topic/to/watch"],
  "actions": [{"domain": "light", "service": "turn_on", "entity_id": "light.xyz"}],
  "detection_filter": {"person_detected": true},
  "cooldown_seconds": 10
}
</spawn>

CRITICAL HA PIPELINE RULE:
When building a pipeline that reacts to sensor data and controls HA devices:
  ✅ CORRECT: sensor-agent publishes to MQTT → ha_actuator subscribes and calls HA directly
  ❌ WRONG:   sensor-agent → send_to('home-assistant-agent') — this causes LLM classification + timeout
  ❌ WRONG:   coordinator-agent that sends tasks to home-assistant-agent — same timeout problem

The home-assistant-agent is ONLY for:
  - User asking to create/edit/delete HA automations via natural language
  - User asking what devices are available
  - User asking to list automations
It is NOT a device control proxy for other agents.

== CAPABILITY & SCHEMA RULES — ALWAYS FOLLOW ==

CAPABILITIES: Always include a "capabilities" list. These are short keywords the planner
uses to find the right agent for a task. Be specific:
  GOOD: ["weather", "temperature", "forecast", "wttr"]
  BAD:  ["data", "api", "agent"]

DESCRIPTION: Always write a precise, one-sentence description. Include what the agent
does, what data it uses, and what it returns:
  GOOD: "Fetches live weather for a city using wttr.in and returns temperature and conditions"
  BAD:  "Gets weather data"

INPUT_SCHEMA: Required for dynamic agents and recommended for LLM agents.
Describe every field the agent expects in handle_task(agent, payload):
  {"city": "str — city name to fetch weather for",
   "units": "str — 'celsius' or 'fahrenheit', default 'celsius'"}
  For agents that only receive free-text tasks, use: {"text": "str — natural language request"}
  For sensor/publisher agents with no handle_task, omit input_schema entirely.

OUTPUT_SCHEMA: Required for dynamic agents and recommended for LLM agents.
Describe every field returned by handle_task:
  {"temp_c": "float — temperature in celsius",
   "condition": "str — weather description",
   "error": "str|null — error message if request failed"}
  For agents that return plain text, use: {"result": "str — the response"}

RULE: If the user asks for a chat agent, math tutor, language teacher, Q&A bot,
explainer, or any agent that primarily responds to questions with text —
ALWAYS use type "llm" with a system_prompt. Never write code for this.

== CODE STRUCTURE (Dynamic agents only) ==
The code must define these async functions:

async def setup(agent):
    # Runs once on start. Import libs, load models, open connections.
    # Store state in agent.state dict.
    pass

async def process(agent):
    # Runs in a loop every poll_interval seconds. Core logic here.
    pass

async def handle_task(agent, payload):
    # Called when another agent sends a task to this agent.
    return {"result": "..."}

async def cleanup(agent):
    # Optional. Runs on stop/delete. Close connections, release resources.
    pass

== AGENT API ==
Inside your code, the `agent` object provides:
  agent.state                         — dict, persists across process() calls
  agent.name                          — this agent's name
  agent.publish(topic, data)          — publish to any MQTT topic
  agent.publish_result(data)          — publish to agents/{id}/result
  agent.publish_detection(data)       — publish to agents/{id}/detections
  agent.log(message)                  — show in dashboard event log
  agent.alert(message, severity)      — trigger a dashboard alert
  agent.persist(key, value)           — save to disk (survives restart)
  agent.recall(key)                   — load from disk
  agent.send_to(agent_name, payload)          — send task to LOCAL agent, wait for result (60s timeout)
  agent.send_to_many([(name, payload), ...])  — send to multiple LOCAL agents IN PARALLEL, returns list

  agent.subscribe(topic, callback)    — subscribe to MQTT topic, call callback(payload) for each message
                                        ALWAYS runs as background task — setup() returns immediately
                                        callback MUST be an async function WITH ONE ARGUMENT (payload)
                                        CORRECT usage:
                                          async def on_message(payload):        # ← exactly one argument
                                              agent.state['latest'] = payload.get('value')
                                          agent.subscribe('sensors/temperature', on_message)
                                        WRONG signatures:
                                          async def on_message():               # ← missing payload arg → ERROR
                                          async def on_message(topic, payload): # ← too many args → ERROR
                                          def on_message(payload):              # ← not async → will fail silently
                                        WRONG call patterns:
                                          data = await agent.subscribe('sensors/temperature')  # WRONG - not awaitable
                                          agent.subscribe('sensors/temperature')               # WRONG - missing callback

  agent.mqtt_get(topic, timeout=10)   — wait for ONE message on topic and return it (one-shot read)
                                        USE THIS when you need a single current value, not a stream
                                        USE agent.subscribe() when you need continuous updates
                                        Example: stats = await agent.mqtt_get('rpi-room/cpu')

  agent.topics(keyword="")            — list all MQTT topics published by known agents
                                        Example: agent.topics("temp") → topics with "temp" in name
                                        Returns: [{"topic": str, "agents": [{"name", "node"}]}, ...]
                                        USE THIS to discover what data is available before subscribing
  agent.capabilities(keyword="")      — list all known agents with their full capability profile
                                        Returns: [{"name", "description", "capabilities", "input_schema", "output_schema", "running", "spawnable"}, ...]
                                        Example: agent.capabilities("weather") → agents that handle weather
                                        USE THIS before delegating to another agent to know exact input/output format
                                        "running": true  → agent is live right now, delegate directly
                                        "running": false, "spawnable": true → catalog recipe, will be
                                          auto-spawned the first time you route a task to it with @agent-name

  agent.window(topic, seconds=300)    — sliding time window over a topic stream for temporal reasoning
                                        Returns a StreamWindow object synchronously. NOT a coroutine.
                                        NEVER use await with window() — it is NOT awaitable.
                                        CORRECT:   agent.state['w'] = agent.window('sensors/temp', seconds=60)
                                        WRONG:     agent.state['w'] = await agent.window(...)  # TypeError!
                                        Store in setup(), read in process():
                                          async def setup(agent):
                                              agent.state['w'] = agent.window('sensors/temp', seconds=60)
                                          async def process(agent):
                                              w = agent.state['w']
                                              avg  = w.mean('value')       # mean over window
                                              mn   = w.min('value')        # minimum
                                              mx   = w.max('value')        # maximum
                                              up   = w.rising(threshold=2) # rose by 2+ degrees
                                              gone = w.absent_for(60)      # no data for 60s
                                              n    = w.event_count('motion', True, seconds=300)
                                              last = w.latest()            # most recent entry dict
                                              cnt  = w.count()             # number of entries
                                        Methods: mean, min, max, rising, falling, stable, absent_for,
                                                 event_count, latest, count, values

  agent.publish_world_state(key, data) — publish retained shared state readable by any agent
                                         Topic: agents/{name}/data/{key}
                                         Example: await agent.publish_world_state('presence', {'zone': 'kitchen', 'present': True})
  agent.read_world_state(topic)        — read a retained world state topic (one-shot)
                                         Example: state = await agent.read_world_state('home/presence/kitchen')

  agent.declare_contract(publishes, subscribes, triggers_when, produces_schema)
                                       — declare this agent's topic contract for auto-wiring
                                         Call from setup() to make agent discoverable by planner
                                         Example:
                                           agent.declare_contract(
                                               publishes=['rpi/camera/detections'],
                                               subscribes=['homeassistant/state_changes/#'],
                                               triggers_when={'person_detected': True},
                                           )

  agent.llm                           — pre-configured LLM (same as main, already authenticated)
  agent.llm.chat(prompt, system="")   — single-turn LLM call, returns string
  agent.llm.complete(messages, system="") — multi-turn LLM call with full history

  The LLM provider is set at startup (Anthropic / OpenAI / Ollama / NVIDIA NIM).
  Agents always use the same provider as main — no configuration needed inside agent code.

== SUBSCRIBE vs MQTT_GET — CRITICAL DISTINCTION ==
  agent.subscribe(topic, callback)  — CONTINUOUS stream. Callback called for EVERY message.
                                      Use for: sensor streams, state changes, ongoing monitoring.
                                      NOT awaitable. Does NOT return data. Callback is required.
  agent.mqtt_get(topic, timeout=N)  — ONE-SHOT read. Returns ONE message then stops.
                                      Use for: reading current value once, polling on demand.
                                      IS awaitable. Returns the payload dict.

  Common mistake — DO NOT do this:
    data = await agent.subscribe('sensors/temp')           # WRONG: subscribe is not awaitable
    agent.subscribe('sensors/temp')                        # WRONG: callback missing
    data = agent.mqtt_get('sensors/temp')                  # WRONG: mqtt_get must be awaited

  Correct patterns:
    # Pattern A: continuous subscription (use in setup, read state in process)
    # callback MUST be async AND accept exactly one argument called 'payload'
    async def setup(agent):
        async def on_temp(payload):        # ← async, exactly ONE arg
            agent.state['temp'] = payload.get('value', 0)
        agent.subscribe('sensors/temperature', on_temp)  # ← no await

    async def process(agent):
        temp = agent.state.get('temp')
        if temp and temp > 30:
            await agent.alert('Too hot!')

    # Pattern B: one-shot read (use in process or handle_task)
    async def process(agent):
        data = await agent.mqtt_get('sensors/temperature', timeout=5)
        if data:
            await agent.log(f"Current temp: {data.get('value')}")

    # Pattern C: sliding window (best for temporal patterns — NO await on window())
    async def setup(agent):
        agent.state['window'] = agent.window('sensors/temperature', seconds=300)  # NO await

    async def process(agent):
        w = agent.state['window']
        if w.rising(threshold=3.0):
            await agent.alert('Temperature rising fast!')
        avg = w.mean('value')
        mn  = w.min('value')
        mx  = w.max('value')
        await agent.log(f'Temp stats: avg={avg:.1f} min={mn:.1f} max={mx:.1f}')

== LLM USAGE — READ THIS CAREFULLY ==
The agent already has a working LLM via agent.llm. DO NOT set up your own LLM.
NEVER import openai, anthropic, ollama, or any LLM library.
NEVER check for API keys. NEVER create a "configure" action for API keys.
NEVER write call_llm(), call_openai(), call_ollama() or similar helper functions.

For any agent that needs language understanding, reasoning, or text generation, just call:
    reply = await agent.llm.chat("your prompt here")
or for multi-turn with history:
    reply = await agent.llm.complete(messages=history, system="You are a helpful assistant.")



== REPLACING AN EXISTING AGENT ==
To fix or improve a running agent, use the same name and add "replace": true.
This stops the old agent and starts the new one immediately:
<spawn>
{
  "name": "yolo-agent",
  "replace": true,
  "description": "Improved version",
  "poll_interval": 0.5,
  "code": "..."
}
</spawn>

== DELETING AN AGENT ==
When the user explicitly asks to remove, stop, delete, or kill an agent, emit a
<delete> block. The framework will stop the agent, remove it from the spawn
registry (so it does NOT auto-restore on restart), clear its manifest, and
record the deletion in conversation history. This is the orchestrator-side
counterpart of <spawn>.

Use <delete> ONLY when the user's intent is clearly to permanently remove an
agent. Do NOT use it to "restart" an agent — use <spawn> with "replace": true
for that. Do NOT use it just because the user is frustrated with output —
ask for clarification first.

Format (JSON):
<delete>
{"name": "math-agent"}
</delete>

Or the shorthand bare-name form (when only a name is needed):
<delete>math-agent</delete>

You can include multiple <delete> blocks in one response, and you can mix
<delete> with <spawn> in the same turn (e.g. "delete the old math-agent and
spawn a new calculator-agent" → emit one <delete> block AND one <spawn>
block in the same response).

Protected names that you CANNOT delete: main, monitor, installer,
home-assistant-agent, anomaly-detector, code-agent, catalog. Requests to
delete these should be politely refused — explain they are system agents.

If the user asks to delete an agent that doesn't exist, do NOT emit a
<delete> block — just tell them it isn't running.

After emitting a <delete> block, write a short user-facing confirmation in
plain prose (the block itself is hidden from the user). Example:

  User: "delete the math-agent please"
  You:  "Removed the math-agent."
        <delete>{"name": "math-agent"}</delete>

== RULES ==
- Always import libraries INSIDE functions (not at module level)
- Use agent.state to pass data between setup() and process()
- Keep process() non-blocking — use asyncio.sleep() for waits
- For blocking operations (cv2, torch inference) wrap in:
    import asyncio
    result = await asyncio.get_event_loop().run_in_executor(None, blocking_fn)
- Python 3.10 compatibility: NEVER nest quotes inside f-strings
  BAD:  f'Hello {"world"}'  or  f'{"x" if c else "y"}'
  GOOD: val = "x" if c else "y"; f'{val}'  — always hoist expressions to a variable first
- Use double-quoted f-strings f"..." as default to avoid conflicts with string literals

== PIPELINES — for complex multi-agent tasks ==
When the user asks for something that requires multiple agents working together
(e.g. "find the manual AND answer a question", "research AND summarise AND email"),
use the run_pipeline capability. Tell the user:
  "I'll coordinate this as a pipeline across [agent1], [agent2]..."
Then in code you can call: await main.run_pipeline(goal, [agents])
The system will spawn an ephemeral TaskManager that plans, executes in parallel
where possible, and reports back — without flooding main's context.

== HOW TO DELEGATE ==
When a task belongs to another agent (running or spawnable), DO IT YOURSELF by
emitting a delegation block. This is the ONLY thing that actually dispatches a
task — describing it in prose does not. The system executes the block, splices
the agent's result back into your reply, and auto-spawns the agent first if it
is only a catalog recipe.

PREFERRED — structured block (unambiguous, never truncated):
  <delegate>{"agent": "manual-agent", "task": "search for the Philips 2200 manual"}</delegate>
  <delegate>{"agent": "weather-agent", "payload": {"city": "Athens"}}</delegate>
Use "task" for a free-text request, or "payload" for a structured dict (e.g. when
the agent's input_schema has named fields, or the task contains file paths or
other text with periods).

ALSO ACCEPTED — @mention, but ONLY in these exact shapes:
  - @agent-name {"key": "value"}        ← JSON payload, anywhere
  - @agent-name <task text>             ← MUST start a line or sentence; the task
                                          runs only up to the next . ! or ?
A name buried mid-sentence ("you can use @agent-name to ...") does NOT dispatch.
When in doubt, use the <delegate> block.

== CRITICAL: NEVER PROXY, NEVER PRETEND ==
NEVER say "I'll forward that to X" or "let me send that request" UNLESS the same
reply contains a real <delegate> block (or a valid @mention form above). Saying
you delegated without emitting one of those is the forbidden behavior — the task
is never sent and the user is misled.
You are the ORCHESTRATOR: you DO the task and report the result. Do NOT tell the
user to "use @agent-name" themselves, and do NOT ask a follow-up question when you
already have what you need to delegate — delegate in the same turn.

== EXISTING AGENTS ==
- main                    : you (orchestrator)
- monitor                 : health monitoring
- installer               : installs Python packages locally AND on remote nodes via SSH
                            Actions: install, node_deploy, node_install, node_run, check, history
- home-assistant-agent    : manages all Home Assistant operations (hardware recommendations, automation create/edit/delete/list)

== INSTALLING PACKAGES ==
Before spawning a dynamic agent that imports non-standard libraries (cv2, torch, pdfplumber,
duckduckgo_search, httpx, etc.), first ask the installer to install them:

<spawn>
{
  "name": "manual-agent",
  "type": "dynamic",
  "description": "searches and reads device manuals",
  "install": ["duckduckgo-search", "httpx", "pdfplumber"],
  "poll_interval": 60,
  "code": "..."
}
</spawn>

If the spawn config has an "install" list, the system will install those packages first automatically.
Standard library and pre-installed packages (asyncio, json, os, time, re, psutil) never need installing.

== REMOTE NODES & SPAWNING ==
wactorz can run agents on any machine (Raspberry Pi, VM, cloud server) that is
running remote_runner.py connected to the same MQTT broker.

To spawn an agent on a remote node, add "node" to the spawn block.
The node name must match the --name used when starting remote_runner.py.

Example — spawn a temperature sensor agent on a Pi:
<spawn>
{
  "name": "temp-sensor",
  "node": "rpi-kitchen",
  "type": "dynamic",
  "description": "Reads temperature and humidity from DHT22 sensor on the kitchen Pi, publishes to MQTT every 30s",
  "capabilities": ["temperature", "humidity", "dht22", "sensor", "climate"],
  "output_schema": {"temperature_c": "float", "humidity_pct": "float", "timestamp": "float"},
  "poll_interval": 30,
  "max_restarts": 5,
  "restart_delay": 3.0,
  "code": "
async def setup(agent):
    await agent.log('Sensor agent ready on ' + agent.node)

async def process(agent):
    import random   # replace with real adafruit_dht read
    temp = round(20 + random.uniform(-2, 2), 1)
    await agent.publish('sensors/temperature', {'value': temp, 'unit': 'C', 'node': agent.node})
    await agent.log(f'Temperature: {temp}C')
  "
}
</spawn>

Remote agents run under a local supervisor — if an agent crashes, it is automatically
restarted with exponential back-off (restart_delay doubles each attempt, capped at 60s).
After max_restarts consecutive failures it is marked failed and removed.
Compile errors and setup() fatals are never retried.

Inside remote agent code, agent.node gives the node name the agent is running on.

Remote agents have access to the LLM via a bridge back to the main node — the API
key stays on the main machine, the Pi just sends the request over MQTT:

  # Single-turn
  reply = await agent.ask_llm("Summarise this sensor reading: 42.3C")
  reply = await agent.ask_llm("Is this anomalous?", system="You are a sensor analyst.")

  # Multi-turn (agent maintains its own history list)
  history = agent.recall("history", [])
  history.append({"role": "user", "content": user_message})
  reply = await agent.chat(history, system="You are Gordon Ramsay.")
  history.append({"role": "assistant", "content": reply})
  agent.persist("history", history[-20:])   # keep last 20 turns

For conversational LLM agents that run remotely and need to respond to @mentions,
always define handle_task() using agent.chat() to process the incoming message:

  async def handle_task(agent, payload):
      text = payload.get("text") or payload.get("message") or str(payload)
      history = agent.recall("history", [])
      history.append({"role": "user", "content": text})
      reply = await agent.chat(history, system="You are Gordon Ramsay, a fiery chef...")
      history.append({"role": "assistant", "content": reply})
      agent.persist("history", history[-20:])
      return {"result": reply}

Without handle_task(), @agent-name mentions will return an error because there is
no entry point for task routing on the remote node.

== AGENT MIGRATION ==
To move a running agent from one machine to another, call migrate_agent():

  result = await main.migrate_agent("agent-name", "target-node-name")

The system will:
  1. Snapshot the agent's persisted state (counters, calibration, learned values)
  2. Stop the agent on its current machine
  3. Start it on the target machine with full state restored
  4. Update the spawn registry so it restores to the right machine on restart
  5. Notify you via the dashboard when migration completes

State that survives migration: any value the agent stored via agent.persist() /
agent.recall() that is JSON-serialisable (numbers, strings, dicts, lists).
Non-serialisable objects (numpy arrays, open file handles) are dropped with a warning
in the logs — they would not survive a process restart anyway.

Example:
  User: "Move temp-sensor to rpi-bedroom"
  You:  await main.migrate_agent("temp-sensor", "rpi-bedroom")

  User: "Bring counter-agent back to the main node"
  You:  await main.migrate_agent("counter-agent", "local")

Or use the slash command directly:
  /migrate temp-sensor rpi-bedroom
  /migrate counter-agent local

== MANAGING REMOTE NODES ==
To restart a remote runner process (e.g. after updating remote_runner.py,
or when a node is misbehaving but still reachable over MQTT):
  /nodes restart rpi-livingroom
  The runner stops all agents cleanly, then re-execs itself in-place.
  Agent state files are preserved on disk — agents come back with full state.

To shut down a remote runner (stops all agents, runner exits):
  /nodes shutdown rpi-livingroom
  Note: if systemd manages the runner on that machine, it will auto-restart.

To remove a node entirely from Wactorz (clears spawn registry + retained MQTT):
  /nodes remove rpi-livingroom

To restart a single agent on a remote node without stopping others:
  /agents restart temp-sensor-agent
  The agent stops and restarts using its saved config and persisted state.
  Use this instead of /agents stop + re-spawn to preserve state.

== LISTING NODES ==
To see which remote nodes are currently online (in your own response code, call it directly):
  nodes = main.list_nodes()
  # Returns: [{"node": "rpi-kitchen", "agents": ["temp-sensor"], "online": True, "last_seen": ...}]

IMPORTANT: In generated DynamicAgent CODE (setup/process/handle_task), NEVER use 'main'.
Use the agent API instead — it has the same data:
  nodes = agent.nodes()   # works inside generated agent code

Use before spawning to verify the target node is reachable.
A node is considered online if it sent a heartbeat in the last 30 seconds.

== DEPLOYING A NEW NODE ==
When the user wants to add a new Pi or machine, use the installer agent directly.
No need to spawn a devops-agent — installer handles SSH deploys natively.

Example:
  User: "set up my Raspberry Pi at 192.168.1.50 as a node called rpi-kitchen"
  You:  Send installer a node_deploy task:

  result = await main.delegate_to_installer({
      "action":     "node_deploy",
      "host":       "192.168.1.50",
      "user":       "pi",
      "node_name":  "rpi-kitchen",
      "broker":     "192.168.1.10",   # your main machine IP, reachable from the Pi
      "password":   "raspberry",       # or use key_path for SSH key auth
  })

  This will:
    1. Upload remote_runner.py to the Pi via SFTP
    2. Install aiomqtt (the only dependency)
    3. Start the runner in the background
    4. The node appears in /nodes within ~15 seconds

To install extra packages on a node BEFORE spawning an agent there:
  result = await main.delegate_to_installer({
      "action":   "node_install",
      "host":     "192.168.1.50",
      "user":     "pi",
      "packages": ["adafruit-circuitpython-dht", "RPi.GPIO"],
  })

To run a shell command on a node:
  result = await main.delegate_to_installer({
      "action":  "node_run",
      "host":    "192.168.1.50",
      "user":    "pi",
      "command": "python3 --version",
  })

The devops-agent is still available as a spawn option for more complex SSH workflows,
but for standard node setup the installer is simpler and faster.

== DEVOPS AGENT EXAMPLE ==
When asked to deploy or manage remote machines, spawn a devops agent like this:

<spawn>
{
  "name": "devops-agent",
  "description": "Manages remote nodes via SSH: deploy, run commands, check health",
  "capabilities": ["ssh", "deploy", "remote", "devops", "node_management"],
  "input_schema":  {"action": "str — deploy_node|run_command|check_node", "host": "str", "user": "str"},
  "output_schema": {"success": "bool", "stdout": "str|null", "error": "str|null"},
  "poll_interval": 3600,
  "code": "
import asyncio, os, json
from pathlib import Path

async def setup(agent):
    try:
        import asyncssh
        agent.state['ssh_available'] = True
        await agent.log('DevOps agent ready. asyncssh available.')
    except ImportError:
        agent.state['ssh_available'] = False
        await agent.alert('asyncssh not installed. Run: pip install asyncssh', 'warning')

async def process(agent):
    await asyncio.sleep(3600)

async def handle_task(agent, payload):
    action = payload.get('action', '')
    if action == 'deploy_node':
        return await deploy_node(agent, payload)
    elif action == 'run_command':
        return await run_remote_command(agent, payload)
    elif action == 'check_node':
        return await check_node(agent, payload)
    return {'error': f'Unknown action: {action}'}

async def deploy_node(agent, payload):
    import asyncssh
    host      = payload.get('host')
    user      = payload.get('user', 'pi')
    node_name = payload.get('node_name', 'remote-node')
    broker    = payload.get('broker', 'localhost')
    password  = payload.get('password')

    await agent.log(f'Deploying node {node_name} to {user}@{host}...')

    # Find remote_runner.py
    candidates = [
        Path(__file__).parent.parent / 'remote_runner.py',
        Path('remote_runner.py'),
    ]
    runner_path = next((p for p in candidates if p.exists()), None)
    if not runner_path:
        return {'error': 'remote_runner.py not found'}

    conn_kwargs = dict(host=host, username=user, known_hosts=None)
    if password:
        conn_kwargs['password'] = password

    try:
        async with asyncssh.connect(**conn_kwargs) as conn:
            # Create directory
            await conn.run('mkdir -p ~/wactorz')
            await agent.log(f'[{node_name}] Created ~/wactorz')

            # Upload remote_runner.py
            async with conn.start_sftp_client() as sftp:
                await sftp.put(str(runner_path), f'/home/{user}/wactorz/remote_runner.py')
            await agent.log(f'[{node_name}] Uploaded remote_runner.py')

            # Install deps
            await conn.run('pip install aiomqtt psutil --break-system-packages -q 2>&1')
            await agent.log(f'[{node_name}] Dependencies installed')

            # Kill existing instance
            await conn.run(f'pkill -f "remote_runner.py.*--name {node_name}" 2>/dev/null; true')

            # Start in background
            cmd = (
                f'nohup python3 ~/wactorz/remote_runner.py '
                f'--broker {broker} --name {node_name} '
                f'> ~/wactorz/{node_name}.log 2>&1 &'
            )
            await conn.run(cmd)
            await agent.log(f'[{node_name}] Runner started! Will appear in dashboard shortly.')

        return {'success': True, 'node': node_name, 'host': host}
    except Exception as e:
        await agent.alert(f'Deploy failed for {node_name}: {e}', 'critical')
        return {'error': str(e)}

async def run_remote_command(agent, payload):
    import asyncssh
    host     = payload.get('host')
    user     = payload.get('user', 'pi')
    command  = payload.get('command', 'echo hello')
    password = payload.get('password')

    conn_kwargs = dict(host=host, username=user, known_hosts=None)
    if password:
        conn_kwargs['password'] = password

    try:
        async with asyncssh.connect(**conn_kwargs) as conn:
            result = await conn.run(command)
            return {'stdout': result.stdout, 'stderr': result.stderr, 'exit_code': result.exit_status}
    except Exception as e:
        return {'error': str(e)}

async def check_node(agent, payload):
    import asyncssh
    host     = payload.get('host')
    user     = payload.get('user', 'pi')
    password = payload.get('password')

    conn_kwargs = dict(host=host, username=user, known_hosts=None)
    if password:
        conn_kwargs['password'] = password

    try:
        async with asyncssh.connect(**conn_kwargs) as conn:
            cpu    = await conn.run('top -bn1 | grep Cpu | awk '{print $2}'')
            mem    = await conn.run('free -m | awk 'NR==2{print $3"/"$2" MB"}'')
            uptime = await conn.run('uptime -p')
            return {
                'host':   host,
                'cpu':    cpu.stdout.strip(),
                'memory': mem.stdout.strip(),
                'uptime': uptime.stdout.strip(),
            }
    except Exception as e:
        return {'error': str(e)}
"
}
</spawn>

After spawning the devops agent, the user can talk to it directly:
@devops-agent deploy rpi-node to pi@192.168.1.50 with broker 192.168.1.10


== EXAMPLE — Math agent (Dynamic with full schemas) ==
<spawn>
{
  "name": "math-agent",
  "type": "dynamic",
  "description": "Performs arithmetic operations: add, subtract, multiply, divide, power, sqrt",
  "capabilities": ["math", "arithmetic", "calculator", "compute"],
  "input_schema":  {
    "operation": "str — one of: add, subtract, multiply, divide, power, sqrt",
    "a": "float — first number",
    "b": "float — second number (not required for sqrt)"
  },
  "output_schema": {
    "result": "float — the computed result",
    "expression": "str — human-readable e.g. 10 + 5 = 15",
    "error": "str|null — error message if operation failed"
  },
  "poll_interval": 3600,
  "code": "async def setup(agent):\n    await agent.log(\'math-agent ready\')\n\nasync def handle_task(agent, payload):\n    import math\n    op = str(payload.get(\'operation\', \'\')).lower().strip()\n    a  = float(payload.get(\'a\', 0))\n    b  = float(payload.get(\'b\', 0))\n    ops = {\n        \'add\':      (a + b,        f\'{a} + {b} = {a + b}\'),\n        \'subtract\': (a - b,        f\'{a} - {b} = {a - b}\'),\n        \'multiply\': (a * b,        f\'{a} * {b} = {a * b}\'),\n        \'divide\':   (a / b if b != 0 else None, f\'{a} / {b}\'),\n        \'power\':    (a ** b,       f\'{a} ^ {b} = {a ** b}\'),\n        \'sqrt\':     (math.sqrt(a), f\'sqrt({a}) = {math.sqrt(a)}\'),\n    }\n    if op not in ops:\n        return {\'result\': None, \'expression\': \'\', \'error\': f\'Unknown op: {op}. Use: {list(ops)}\'}\n    result, expr = ops[op]\n    if result is None:\n        return {\'result\': None, \'expression\': expr, \'error\': \'Division by zero\'}\n    expr_full = expr if \'=\' in expr else f\'{expr} = {result}\'\n    await agent.log(f\'Computed: {expr_full}\')\n    return {\'result\': result, \'expression\': expr_full, \'error\': None}\n\nasync def process(agent):\n    import asyncio\n    await asyncio.sleep(3600)"
}
</spawn>

== EXAMPLE — Webcam YOLO agent ==
CAMERA OPENING ON RASPBERRY PI — always use this pattern for RPI nodes:
  USB cameras: try CAP_V4L2 backend explicitly, fall back through device indices
  Never use cv2.VideoCapture(0) alone on RPI — it fails with OpenCV/FFMPEG warning
  Always run blocking cv2 calls in run_in_executor to avoid blocking the event loop

CAMERA OPENING ON WINDOWS — the framework auto-injects a resilient cv2 shim:
  cv2.VideoCapture(0) is automatically wrapped with retry+backoff and forced
  onto the CAP_DSHOW backend (more reliable than the default MSMF). Just call
  cv2.VideoCapture(0) — DO NOT pass cv2.CAP_MSMF explicitly.

CRITICAL — DO NOT RELEASE+REOPEN THE CAMERA INSIDE process():
  On a failed cap.read(), simply `return` from process(). The framework will
  call process() again after poll_interval, and the camera handle is still
  valid — a transient frame failure does NOT mean the device is dead. Calling
  cap.release() + cv2.VideoCapture(...) on every failed read produces a flap
  loop on Windows because MSMF/DSHOW need wall-clock time to release the
  device handle, and a tight reopen loop never gives them that time.

  WRONG (causes flap loop):
      ok, frame = cap.read()
      if not ok:
          cap.release()
          agent.state['cap'] = cv2.VideoCapture(0)
          return

  RIGHT:
      ok, frame = cap.read()
      if not ok:
          return   # next process() tick will retry on the same handle

<spawn>
{
  "name": "yolo-agent",
  "description": "Reads webcam frames, runs YOLOv8 object detection, publishes detections to MQTT",
  "capabilities": ["yolo", "object_detection", "webcam", "vision", "camera"],
  "output_schema": {"detections": "list — [{class, confidence}]", "count": "int", "timestamp": "float"},
  "poll_interval": 0.5,
  "code": "
async def setup(agent):
    import cv2
    from ultralytics import YOLO
    import asyncio
    agent.state['model'] = YOLO('yolov8n.pt')
    # RPI-compatible camera open: try V4L2 backend explicitly across device indices
    def _open_camera():
        for idx in [0, 1, 2]:
            cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                return cap
            cap.release()
        return None
    cap = await asyncio.get_event_loop().run_in_executor(None, _open_camera)
    if cap:
        agent.state['cap'] = cap
        await agent.log('Camera opened with V4L2 backend, model loaded')
    else:
        await agent.alert('Could not open camera — check /dev/video* exists', 'critical')
        agent.state['cap'] = None

async def process(agent):
    import time, asyncio
    cap = agent.state.get('cap')
    model = agent.state.get('model')
    if not cap or not model:
        return
    ret, frame = await asyncio.get_event_loop().run_in_executor(None, cap.read)
    if not ret:
        return
    results = await asyncio.get_event_loop().run_in_executor(
        None, lambda: model(frame, conf=0.45, verbose=False)
    )
    detections = []
    for r in results:
        for box in r.boxes:
            detections.append({'class': r.names[int(box.cls)], 'confidence': round(float(box.conf), 3)})
    await agent.publish_detection({'detections': detections, 'count': len(detections), 'timestamp': time.time()})
    if detections:
        classes = list({d['class'] for d in detections})
        await agent.log(f'Detected: {classes}')

async def cleanup(agent):
    cap = agent.state.get('cap')
    if cap:
        cap.release()
"
}
</spawn>
"""


class MainActor(LLMAgent):
    DESCRIPTION  = "Main orchestrator: spawns agents, routes tasks, manages the multi-agent system"
    CAPABILITIES = ["spawn_agent", "list_agents", "list_nodes", "list_topics", "orchestration"]

    INTENT_CLASSIFIER_PROMPT = (
        "You are a routing classifier for a smart home AI assistant.\n"
        "Respond with exactly one token: ACTUATE, HA, PIPELINE, or OTHER.\n\n"
        "ACTUATE = immediate one-shot device control in Home Assistant:\n"
        "  - Turn on/off a device right now\n"
        "  - Set temperature, dim lights, lock/unlock door\n"
        "  - Open/close covers or blinds right now\n"
        "  - Any direct command whose whole purpose is immediate device control\n\n"
        "HA = Home Assistant management, listing, or automation CRUD:\n"
        "  - List devices, areas, entities, automations\n"
        "  - Create/edit/delete a HA automation\n"
        "  - Query what devices or automations exist\n\n"
        "PIPELINE = a reactive rule that should run continuously:\n"
        "  - 'if X happens then do Y' — any conditional/reactive logic\n"
        "  - 'when X send me a message/notification'\n"
        "  - 'whenever X turns on/off do Y'\n"
        "  - Any rule involving a sensor state change triggering an action or notification\n"
        "  - Any webcam/camera detection triggering anything\n"
        "  - Anything involving Discord/Telegram notifications triggered by an event\n\n"
        "OTHER = general conversation, coding, questions, or mixed requests.anything not HA or pipeline related.\n\n"
        "Important:\n"
        "- Choose ACTUATE only when the entire request is immediate device control.\n"
        "- If the request mixes device control with non-HA tasks, return OTHER.\n"
        "- If the request is about automations, listing, discovery, or CRUD, return HA."
    )

    def __init__(self, llm_provider: Optional[LLMProvider] = None, **kwargs):
        kwargs.setdefault("name", "main")
        kwargs.setdefault("system_prompt", ORCHESTRATOR_PROMPT)
        super().__init__(llm_provider=llm_provider, **kwargs)
        self._result_futures: dict[str, asyncio.Future] = {}
        # Queued monitor notifications — prepended to next user response
        self._pending_notifications: list[dict] = []
        self.protected = True
        # Remote node tracking: node_name → {"last_seen": float, "agents": [...]}
        self._known_nodes: dict[str, dict] = {}
        # Topic registry: topic → [manifest, ...] — built from agents/+/manifest
        self._topic_registry: dict[str, list] = {}  # topic → list of agent manifests
        self._agent_manifests: dict[str, dict] = {}  # agent name → latest manifest (includes schemas)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        await super().on_start()
        await self._restore_spawned_agents()
        # Listen for remote node heartbeats so we know what's online
        self._tasks.append(asyncio.create_task(self._node_heartbeat_listener()))
        # Detect nodes that go silent and clean up their agents
        self._tasks.append(asyncio.create_task(self._node_offline_watcher()))
        # Listen for agent capability manifests to build topic registry
        self._tasks.append(asyncio.create_task(self._manifest_listener()))
        # LLM bridge — lets remote agents call agent.ask_llm() / agent.chat()
        self._tasks.append(asyncio.create_task(self._llm_bridge_listener()))
        # Observe real MQTT payloads from remote agents to populate observed_samples
        self._tasks.append(asyncio.create_task(self._remote_observed_samples_listener()))
        # Receive state + config from remote nodes during remote→local migration
        self._tasks.append(asyncio.create_task(self._state_return_listener()))
        # Inject persisted user facts into system prompt
        self._inject_user_facts_into_prompt()

    # ── Spawn registry ─────────────────────────────────────────────────────

    def _get_spawn_registry(self) -> dict:
        return self.recall(SPAWN_REGISTRY_KEY) or {}

    def _save_to_spawn_registry(self, config: dict):
        reg = self._get_spawn_registry()
        reg[config["name"]] = config
        self.persist(SPAWN_REGISTRY_KEY, reg)
        logger.info(f"[{self.name}] Spawn registry: {list(reg.keys())}")

    def _remove_from_spawn_registry(self, name: str):
        reg = self._get_spawn_registry()
        if name in reg:
            del reg[name]
            self.persist(SPAWN_REGISTRY_KEY, reg)
            logger.info(f"[{self.name}] Removed '{name}' from spawn registry.")

    async def _clear_agent_manifest(self, name: str, actor_id: Optional[str] = None):
        """
        Clear an agent's manifest from main's in-memory caches AND from the
        retained MQTT manifest topic. Without this, list_capabilities() will
        keep reporting the agent (with running=false but never disappearing),
        and on next restart it would be re-loaded from the retained message.

        Call this whenever an agent is stopped/deleted/replaced.
        """
        # Drop from in-memory caches immediately
        self._agent_manifests.pop(name, None)
        for topic, entries in list(self._topic_registry.items()):
            self._topic_registry[topic] = [m for m in entries if m.get("name") != name]
            if not self._topic_registry[topic]:
                self._topic_registry.pop(topic, None)
        # Publish empty retained payload to clear the broker-side retained manifest.
        # Need actor_id for the topic — fall back to looking it up from the registry
        # (only works if the actor is still alive — best-effort).
        if not actor_id and self._registry:
            target = self._registry.find_by_name(name)
            if target:
                actor_id = target.actor_id
        if actor_id:
            await self._mqtt_publish(
                f"agents/{actor_id}/manifest", b"", retain=True
            )
            logger.debug(f"[{self.name}] Cleared retained manifest for '{name}'")

    def _record_agent_deletion(self, name: str, reason: str = "user request"):
        """
        Inject a system-style note into conversation history that an agent was
        deleted. This is critical because the LLM otherwise sees its own earlier
        turn ("Spawned 'chat-agent'") and assumes the agent still exists when
        the user later asks to spawn one with the same name.

        Strengthens the running-agents system prompt block with explicit textual
        evidence inside the message stream — which models weight more heavily
        than system-prompt assertions.
        """
        try:
            note = (
                f"[SYSTEM] Agent '{name}' was deleted ({reason}). "
                f"It is no longer running. If the user asks to spawn an agent "
                f"with this name again, treat it as a fresh spawn — do NOT claim "
                f"it already exists."
            )
            self._conversation_history.append({"role": "user", "content": note})
            self._conversation_history.append({
                "role": "assistant",
                "content": f"Acknowledged — '{name}' has been removed from my view.",
            })
            self.persist("conversation_history", self._conversation_history)
            logger.info(f"[{self.name}] Recorded deletion note for '{name}' in history")
        except Exception as e:
            logger.warning(f"[{self.name}] Failed to record deletion note: {e}")

    # ── Pipeline rules registry ────────────────────────────────────────────
    # Stores grouped rules: one entry per user request, listing all agents spawned for it.
    # Schema: { rule_id: { "rule_id", "task", "agents": [str], "created_at": float } }

    def get_pipeline_rules(self) -> dict:
        return self.recall(PIPELINE_RULES_KEY) or {}

    def save_pipeline_rule(self, rule: dict):
        rules = self.get_pipeline_rules()
        rules[rule["rule_id"]] = rule
        self.persist(PIPELINE_RULES_KEY, rules)
        logger.info(f"[{self.name}] Pipeline rule saved: {rule['rule_id']} agents={rule.get('agents', [])}")

    # ── Pending-plan registry (dry-run / approval flow) ────────────────────
    # When PIPELINE intent fires, the planner runs in plan_only mode and returns
    # a proposal instead of spawning. We store the proposal here, show it to the
    # user, and wait for approval before executing. Persisted so a restart in
    # the middle of an approval flow doesn't lose the user's pending plans.
    #
    # Schema: { plan_id: {
    #     "plan_id":    str,
    #     "task":       str,            # original user request
    #     "created_at": float,
    #     "status":     "pending"|"approved"|"rejected"|"superseded"|"expired",
    #     "envelope":   dict,           # the full plan envelope from the planner
    # } }
    PLAN_TTL_S = 24 * 3600   # auto-expire pending plans after 24h

    def get_pending_plans(self) -> dict:
        plans = self.recall(PENDING_PLANS_KEY) or {}
        # Expire stale entries on every read so we don't have to gc separately
        import time as _t
        now = _t.time()
        expired_ids = [
            pid for pid, p in plans.items()
            if p.get("status") == "pending"
            and (now - p.get("created_at", now)) > self.PLAN_TTL_S
        ]
        if expired_ids:
            for pid in expired_ids:
                plans[pid]["status"] = "expired"
            self.persist(PENDING_PLANS_KEY, plans)
        return plans

    def save_pending_plan(self, plan: dict):
        plans = self.recall(PENDING_PLANS_KEY) or {}
        plans[plan["plan_id"]] = plan
        self.persist(PENDING_PLANS_KEY, plans)

    def update_plan_status(self, plan_id: str, status: str):
        plans = self.recall(PENDING_PLANS_KEY) or {}
        if plan_id in plans:
            plans[plan_id]["status"] = status
            self.persist(PENDING_PLANS_KEY, plans)

    def _most_recent_pending_plan(self) -> Optional[dict]:
        """Returns the most-recently-created plan still in 'pending' status, or None."""
        plans = self.get_pending_plans()
        pending = [p for p in plans.values() if p.get("status") == "pending"]
        if not pending:
            return None
        return max(pending, key=lambda p: p.get("created_at", 0))

    def _format_plan_proposal(self, plan: dict) -> str:
        """
        Render a pending plan as a human-readable summary for the user.

        Goals (in priority order):
          1. Show what the rule WILL DO in plain English (most important).
          2. Show which inputs / topics it listens to (so user can spot
             "did you really mean THIS sensor?").
          3. Show side effects: notifications sent, devices controlled, files
             written. Anything that affects the world.
          4. Hide raw code by default — link to expansion via /plans show <id>.
          5. Make the approval actions obvious.
        """
        envelope = plan.get("envelope", {})
        agents   = envelope.get("plan", []) or envelope.get("agents", [])
        task     = plan.get("task", envelope.get("task", "?"))
        plan_id  = plan.get("plan_id", "?")

        lines = []
        lines.append(f"**Proposed pipeline** (id `{plan_id}`)")
        lines.append(f"For: _{task}_")
        lines.append("")
        lines.append(f"This will create {len(agents)} agent(s):")

        # Per-agent summary
        for i, step in enumerate(agents, 1):
            name = step.get("name", "?")
            desc = step.get("description") or step.get("spawn_config", {}).get("description", "")
            spawn_cfg = step.get("spawn_config", {})
            agent_type = spawn_cfg.get("type", "dynamic")
            install   = spawn_cfg.get("install", []) or []

            lines.append(f"\n  {i}. **{name}** ({agent_type})")
            if desc:
                lines.append(f"     purpose: {desc}")

            # For scheduled agents: render the schedule prominently — this is
            # the field the user most needs to verify (did the LLM correctly
            # interpret "5pm every weekday"?).
            if agent_type == "scheduled":
                schedule_spec = spawn_cfg.get("schedule") or {}
                if isinstance(schedule_spec, dict) and schedule_spec:
                    try:
                        from .scheduled_agent import describe_schedule
                        tz_name = schedule_spec.get("tz") or self.get_user_facts().get("pref_timezone")
                        lines.append(f"     fires: {describe_schedule(schedule_spec, tz_name)}")
                    except Exception:
                        lines.append(f"     fires: {schedule_spec}")
                topic = spawn_cfg.get("publish_topic") or f"schedule/{name}/fired"
                lines.append(f"     publishes: {topic}")

            # Inputs — what it listens to
            subs = step.get("subscribes", []) or spawn_cfg.get("subscribe", []) or []
            if subs:
                lines.append(f"     listens on: {', '.join(subs)}")

            # Outputs — what it publishes
            pubs = step.get("publishes", []) or spawn_cfg.get("publish", []) or []
            if pubs and agent_type != "scheduled":   # already shown above for scheduled
                lines.append(f"     publishes: {', '.join(pubs)}")

            # External side effects — webhooks/notifications/HA actions
            # Best-effort heuristic from the code or spawn_cfg fields
            side_effects = []
            code = spawn_cfg.get("code", "") or ""
            if "webhook" in code.lower() or "notification" in code.lower():
                side_effects.append("sends notification")
            if "discord.com/api/webhooks" in code:
                side_effects.append("posts to Discord")
            if "api.telegram.org" in code:
                side_effects.append("posts to Telegram")
            if "homeassistant" in code.lower() and ("turn_on" in code or "turn_off" in code or "call_service" in code):
                side_effects.append("controls Home Assistant device")
            if agent_type == "ha_actuator":
                target = spawn_cfg.get("entity_id") or spawn_cfg.get("target", "?")
                action = spawn_cfg.get("service") or spawn_cfg.get("action", "?")
                side_effects.append(f"calls HA: {action} on {target}")
            if side_effects:
                lines.append(f"     side effects: {'; '.join(side_effects)}")

            # Install requirements — surfaced because user pays the cost
            if install:
                pkgs = ", ".join(install if isinstance(install, list) else [install])
                lines.append(f"     installs: {pkgs}")

        lines.append("")
        lines.append("**To proceed:**")
        lines.append("  Reply **yes** (or **approve**) to spawn the agents above.")
        lines.append("  Reply **no** (or **reject**) to discard this plan.")
        lines.append("  Reply with a correction (e.g. _'use the bedroom sensor instead'_) to revise.")
        lines.append(f"  Or run `/plans show {plan_id}` to see the full code.")

        return "\n".join(lines)

    def get_notification_urls(self) -> dict:
        """Return persisted notification webhook URLs (discord, telegram, slack, etc.)"""
        return self.recall("_notification_urls") or {}

    # ── User facts ─────────────────────────────────────────────────────────
    # Key facts extracted from conversation: HA URL, entity names, preferences,
    # user name, webhook URLs, etc. Stored separately from history so they
    # survive summarization and persist indefinitely.

    _FACTS_EXTRACT_PROMPT = (
        "You extract durable facts the assistant should remember about the user "
        "long-term. Read the EXCHANGE below and return any new facts as JSON.\n\n"
        "## What to extract — three buckets\n"
        "Use these key prefixes so the assistant can group facts later:\n\n"
        "**pref_*** — Personal identity, preferences, routines (slow-changing).\n"
        "  Examples: pref_user_name, pref_location, pref_timezone, pref_language,\n"
        "  pref_favorite_sport, pref_communication_style ('terse'/'detailed'),\n"
        "  pref_units ('metric'/'imperial'), pref_work_hours, pref_sleep_time,\n"
        "  pref_household_members.\n\n"
        "**device_*** — System and device topology (the user's setup).\n"
        "  Examples: device_ha_url, device_mqtt_broker, device_living_room_light\n"
        "  (entity ID), device_kitchen_camera (model + entity), device_pi_node_kitchen\n"
        "  (hardware spec), device_yolo_model_path, device_webhook_discord.\n\n"
        "**policy_*** — Standing instructions / rules of engagement.\n"
        "  Examples: policy_quiet_hours ('23:00-07:00'), policy_alert_channel\n"
        "  ('telegram'), policy_temperature_unit ('celsius'),\n"
        "  policy_low_battery_threshold ('20%'), policy_ask_before_spawn\n"
        "  ('always for cv2/webcam'), policy_planner_style ('no follow-up\n"
        "  questions, just pick something').\n\n"
        "## Rules\n"
        "  - Snake_case keys, ALWAYS prefixed with one of the three above.\n"
        "  - Values: a short phrase, not a sentence.\n"
        "  - SUPERSEDE: if the user updates a fact ('actually call me Yannis'),\n"
        "    return the SAME key with the new value — the system overwrites.\n"
        "  - Return ALL applicable facts in one object — don't pick just one.\n"
        "  - Return {} if nothing durable was stated.\n\n"
        "## What NOT to extract\n"
        "  - Things the ASSISTANT said. Only the user's explicit statements.\n"
        "  - One-off questions ('what time is it?', 'how do I do X?').\n"
        "  - Transient state ('user is debugging Y right now').\n"
        "  - Speculation or 'maybe' statements ('I might get a Yale lock soon').\n"
        "  - Plain-text passwords or full API tokens. URLs and entity IDs are fine.\n"
        "  - Facts about devices/agents that the user just deleted in this turn.\n\n"
        "## Examples\n"
        '  USER: "I am John, I like football"\n'
        '  → {"pref_user_name": "John", "pref_favorite_sport": "football"}\n\n'
        '  USER: "my home assistant is at http://192.168.1.10:8123"\n'
        '  → {"device_ha_url": "http://192.168.1.10:8123"}\n\n'
        '  USER: "use Telegram for alerts, not Discord"\n'
        '  → {"policy_alert_channel": "telegram"}\n\n'
        '  USER: "the living room light is light.wiz_rgbw_02cba0 and I prefer warm white"\n'
        '  → {"device_living_room_light": "light.wiz_rgbw_02cba0", "pref_light_color": "warm white"}\n\n'
        '  USER: "actually call me Yannis"\n'
        '  → {"pref_user_name": "Yannis"}\n\n'
        '  USER: "what time is it?"\n'
        "  → {}\n\n"
        '  USER: "I might switch to Zigbee2MQTT eventually"\n'
        "  → {}\n\n"
        "Output ONLY a valid JSON object. No prose, no markdown fences, no explanation."
    )

    def get_user_facts(self) -> dict:
        return self.recall("_user_facts") or {}

    def _get_running_agents_summary(self) -> str:
        """
        Build a short, authoritative description of currently running agents
        by reading the live registry (same source the planner uses).
        Returns empty string if registry is unavailable or only main is running.
        """
        if not self._registry:
            return ""
        skip = {"main", "monitor", "installer"}
        lines = []
        for actor in self._registry.all_actors():
            if actor.name in skip:
                continue
            # Skip transient planner instances — they're supervisors of their
            # spawned pipeline agents and not user-facing capabilities.
            if actor.name.startswith("planner-"):
                continue
            desc = (
                getattr(actor, "DESCRIPTION", None)
                or getattr(actor, "description", "")
                or (getattr(actor, "system_prompt", "") or "")[:80]
                or type(actor).__name__
            )
            # Single-line summary, trimmed
            desc = " ".join(str(desc).split())[:120]
            lines.append(f"  {actor.name} — {desc}" if desc else f"  {actor.name}")
        if not lines:
            return ""
        return "\n".join(lines)

    def _prefix_with_live_context(self, user_text: str) -> str:
        """
        Wrap the user's message with a `[CURRENT SYSTEM STATE]` block so the LLM
        sees the live agent list INSIDE the user message — not just the system
        prompt.

        Why both? Models weight in-message content more heavily than system prompts
        for "what is true right now" questions. Having the same list in both places
        is belt-and-braces: the system prompt sets the rule ("trust this list"),
        the per-message prefix supplies fresh evidence the rule applies to.

        The prefix is wrapped in clear delimiters so it's visually obvious to the
        model that it's context, not the user's actual question.
        """
        live_names = []
        if self._registry:
            skip = {"main", "monitor", "installer"}
            for actor in self._registry.all_actors():
                if actor.name in skip:
                    continue
                if actor.name.startswith("planner-"):
                    continue
                live_names.append(actor.name)
        live_names.sort()

        if live_names:
            ctx = (
                "[CURRENT SYSTEM STATE — auto-injected, NOT from the user]\n"
                f"Currently running agents (live, just queried): {', '.join(live_names)}\n"
                "If the user asks what agents exist or are running, answer using EXACTLY\n"
                "this list. Do not add agents from your memory of earlier turns.\n"
                "[END SYSTEM STATE]\n\n"
            )
        else:
            ctx = (
                "[CURRENT SYSTEM STATE — auto-injected, NOT from the user]\n"
                "Currently running agents (live, just queried): NONE\n"
                "No user-spawned agents exist right now. If the user asks what's running,\n"
                "say so plainly. Do not invent agents from earlier in the conversation.\n"
                "[END SYSTEM STATE]\n\n"
            )
        return ctx + user_text

    def _rebuild_system_prompt(self):
        """
        Reconstruct the system prompt from ORCHESTRATOR_PROMPT plus dynamic blocks:
          1. Currently running agents (live registry — authoritative, refreshed each call)
          2. Known user facts (persisted)

        This is the single source of truth for the system prompt. It MUST be called
        before every LLM turn so main never answers from a stale view of the world.
        Both blocks are appended in a fixed order so the prompt is deterministic.

        IMPORTANT: ORCHESTRATOR_PROMPT references agent.capabilities() — that's
        documentation aimed at spawned DynamicAgents which have an _AgentAPI.
        Main itself has NO such method. Without an explicit override, the LLM
        reads the documentation, "calls" the function (it can't), and confabulates
        the result based on conversation history. We prepend an OVERRIDE block that
        tells main directly: the running-agents list below IS the result of that
        lookup, do not pretend to call anything.
        """
        # ── Override block for main specifically ──
        override = (
            "== MAIN-SPECIFIC OVERRIDE (read this FIRST) ==\n"
            "You are 'main'. You are NOT a DynamicAgent. You do NOT have an `agent` object.\n"
            "You CANNOT call agent.capabilities(), agent.send_to(), agent.topics(), "
            "agent.subscribe(), agent.window(), agent.mqtt_get(), or any other agent.* method.\n"
            "Those methods exist for OTHER agents you SPAWN. They do not exist for you.\n\n"
            "If you see those methods mentioned later in this prompt, that is reference\n"
            "documentation for code you WRITE inside <spawn> blocks — NOT a tool you can\n"
            "invoke yourself. Never write 'Let me call agent.capabilities()' in a reply.\n"
            "Never fabricate the output of such a call.\n\n"
            "When the user asks what agents exist, what's running, or anything about\n"
            "current system state, READ THE 'CURRENTLY RUNNING AGENTS' BLOCK BELOW.\n"
            "That block IS your capability lookup — already done for you, refreshed live\n"
            "on every turn. Do not pretend to perform a separate lookup.\n"
        )

        prompt = override + "\n" + ORCHESTRATOR_PROMPT

        # ── Block 1: live running agents (so main knows the truth, not its memory) ──
        # Wording is deliberately strong: the LLM tends to trust earlier conversation
        # turns ("I just spawned X") over the system prompt. We need to override that.
        agents_summary = self._get_running_agents_summary()
        header = (
            "\n\n== CURRENTLY RUNNING AGENTS (LIVE GROUND TRUTH — overrides conversation history) ==\n"
            "This block is regenerated from the live registry on EVERY turn. It is the ONLY\n"
            "authoritative source for which agents exist. If something is not on this list,\n"
            "it does NOT exist right now — even if conversation history says you spawned it.\n"
            "Agents can be deleted by the user at any time, and the conversation will not\n"
            "necessarily mention it. ALWAYS trust this list over your memory of past turns.\n\n"
            "When the user asks what agents are running, list EXACTLY these names — no\n"
            "more, no less. Do not invent entries from memory. Do not include agents from\n"
            "earlier turns that aren't here now.\n\n"
            "When the user asks to spawn an agent and that name is NOT on this list:\n"
            "spawn it. Do NOT say 'it already exists' — that claim is based on stale memory.\n"
        )
        if agents_summary:
            prompt += header + agents_summary
        else:
            prompt += header + "  (no user-spawned agents are currently running)"

        # ── Block 2: persisted user facts, grouped by bucket ──
        facts = self.get_user_facts()
        if facts:
            buckets = {
                "pref_":   ("PREFERENCES & IDENTITY", []),
                "device_": ("DEVICES & SETUP",        []),
                "policy_": ("STANDING POLICIES",      []),
                "":        ("OTHER FACTS",            []),   # legacy / unprefixed
            }
            for k, v in facts.items():
                placed = False
                for prefix, (_, items) in buckets.items():
                    if prefix and k.startswith(prefix):
                        items.append(f"  {k[len(prefix):]}: {v}")
                        placed = True
                        break
                if not placed:
                    buckets[""][1].append(f"  {k}: {v}")

            sections = []
            for _, (heading, items) in buckets.items():
                if items:
                    sections.append(f"\n[{heading}]\n" + "\n".join(items))
            if sections:
                prompt += (
                    "\n\n== KNOWN USER FACTS (always keep in mind) =="
                    + "".join(sections)
                    + "\nWhen a POLICY conflicts with a default behavior, follow the policy."
                )

        self.system_prompt = prompt

    def _inject_user_facts_into_prompt(self):
        """Backward-compatible alias — delegates to the unified rebuild."""
        self._rebuild_system_prompt()

    async def _extract_and_save_facts(self, user_message: str, assistant_response: str):
        """
        After each exchange, ask the LLM to extract any new durable facts.

        Observability: this method logs every attempt at INFO level (start),
        success at INFO (with extracted keys), and failures at WARNING. If you
        suspect facts aren't being saved, search the log for
        '[main] Facts extraction'.

        Namespace normalization: the prompt asks for keys prefixed with one of
        pref_/device_/policy_, but LLMs sometimes return raw keys ('user_name'
        instead of 'pref_user_name'). We normalize on save so a stray unprefixed
        key still ends up in a sensible bucket rather than the OTHER catch-all.
        """
        if self.llm is None:
            logger.warning(f"[{self.name}] Facts extraction skipped: no LLM provider")
            return
        if not user_message or not user_message.strip():
            return
        logger.info(f"[{self.name}] Facts extraction running on: {user_message[:80]!r}")
        exchange = f"USER: {user_message[:600]}\nASSISTANT: {assistant_response[:600]}"
        try:
            raw, _usage = await self.llm.complete(
                messages=[{"role": "user", "content": exchange}],
                system=self._FACTS_EXTRACT_PROMPT,
                max_tokens=300,
            )
            self.total_input_tokens  += _usage.get("input_tokens", 0)
            self.total_output_tokens += _usage.get("output_tokens", 0)
            self.total_cost_usd      += _usage.get("cost_usd", 0.0)
            self._persist_cost()
            import json as _json
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            if not clean:
                logger.warning(f"[{self.name}] Facts extraction returned empty string")
                return
            new_facts = _json.loads(clean)
            if not isinstance(new_facts, dict):
                logger.warning(f"[{self.name}] Facts extraction returned non-dict: {type(new_facts).__name__}")
                return
            if not new_facts:
                logger.info(f"[{self.name}] Facts extraction: nothing durable in this turn")
                return

            # Normalize keys: if the LLM forgot the namespace prefix, infer one
            # from common patterns. This keeps the bucketed display clean.
            normalized = {}
            for k, v in new_facts.items():
                if k.startswith(("pref_", "device_", "policy_")):
                    normalized[k] = v
                    continue
                # Heuristic guesses for common unprefixed keys
                if k.endswith("_url") or k.endswith("_endpoint") or k.endswith("_path"):
                    normalized[f"device_{k}"] = v
                elif k.startswith(("user_", "favorite_", "pref_")) or k in ("name", "age", "location", "language"):
                    normalized[f"pref_{k}"] = v
                elif "policy" in k or "rule" in k or "threshold" in k:
                    normalized[f"policy_{k}"] = v
                else:
                    normalized[f"pref_{k}"] = v   # default bucket for unknowns

            # Merge with existing facts (supersede on key collision — by design)
            facts = self.get_user_facts()
            superseded = [k for k in normalized if k in facts and facts[k] != normalized[k]]
            facts.update(normalized)
            self.persist("_user_facts", facts)
            self._inject_user_facts_into_prompt()
            log_msg = f"[{self.name}] User facts updated: {list(normalized.keys())}"
            if superseded:
                log_msg += f" (superseded: {superseded})"
            logger.info(log_msg)
        except _json.JSONDecodeError as e:
            logger.warning(
                f"[{self.name}] Facts extraction JSON parse failed: {e}. "
                f"Raw response (first 200 chars): {raw[:200]!r}"
            )
        except Exception as e:
            logger.warning(f"[{self.name}] Facts extraction failed: {e!r}")

    async def delete_pipeline_rule(self, rule_id: str) -> str:
        """Stop all agents for a rule and remove it from registry."""
        rules = self.get_pipeline_rules()
        rule = rules.get(rule_id)
        if not rule:
            return f"No rule found with id '{rule_id}'."
        agents = rule.get("agents", [])
        stopped = []
        for agent_name in agents:
            self._remove_from_spawn_registry(agent_name)
            if self._registry:
                actor = self._registry.find_by_name(agent_name)
                if actor:
                    actor_id = actor.actor_id
                    await actor.stop()
                    await self._registry.unregister(actor_id)
                    await self._clear_agent_manifest(agent_name, actor_id)
                    self._record_agent_deletion(agent_name, reason=f"pipeline rule '{rule_id}' deleted")
                    stopped.append(agent_name)
        del rules[rule_id]
        self.persist(PIPELINE_RULES_KEY, rules)
        task_preview = rule.get("task", "")[:60]
        return f"Rule '{rule_id}' deleted. Stopped agents: {', '.join(stopped) or 'none running'}.\nRule was: {task_preview}"

    async def _restore_spawned_agents(self):
        reg = self._get_spawn_registry()
        if not reg:
            return

        # ── Skip names already brought up by the Supervisor's factories ─────
        # Both the Supervisor (registry.py) and this method spawn user agents
        # at startup. Without this guard they race: supervisor.start() spawns
        # instance #1 via its stored factory, then on_start() runs us here and
        # we spawn instance #2. Both register under the same deterministic
        # actor_id (uuid5 of name) — the dict entry gets overwritten but the
        # first instance's aiomqtt subscribe listeners keep running, causing
        # every MQTT message to be delivered twice.
        sup = getattr(self._registry, "_supervisor_ref", None) if self._registry else None
        already_supervised: set[str] = set()
        if sup is not None:
            for sup_name, spec in sup._specs.items():
                if spec.actor is not None and not spec.retired:
                    already_supervised.add(sup_name)

        if already_supervised:
            skip = sorted(n for n in reg.keys() if n in already_supervised)
            if skip:
                logger.info(
                    f"[{self.name}] Supervisor already restarted "
                    f"{len(skip)} agent(s); skipping restore for: {skip}"
                )

        pending = {n: c for n, c in reg.items() if n not in already_supervised}
        if not pending:
            return

        logger.info(f"[{self.name}] Restoring {len(pending)} agent(s): {list(pending.keys())}")
        for name, config in pending.items():
            node = config.get("node", "").strip()
            if node:
                # Remote agent — re-publish spawn to its node; no local object expected
                logger.info(f"[{self.name}] Re-spawning remote agent '{name}' on node '{node}'")
                try:
                    await self._spawn_remote(config, node, save=False)
                except Exception as e:
                    logger.error(f"[{self.name}] Failed to restore remote '{name}' on '{node}': {e}")
                continue
            if self._registry and self._registry.find_by_name(name):
                logger.info(f"[{self.name}] '{name}' already running, skipping.")
                continue
            try:
                await self._spawn_from_config(config, save=False)
                logger.info(f"[{self.name}] Restored: {name}")
            except Exception as e:
                logger.error(f"[{self.name}] Failed to restore '{name}': {e}")

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            # Intercept monitor notifications BEFORE passing to LLM _handle_task
            if isinstance(msg.payload, dict) and msg.payload.get("_monitor_notification"):
                self._pending_notifications.append(msg.payload)
                logger.info(f"[{self.name}] Monitor alert queued: {msg.payload.get('message','')[:80]}")
                return
            await self._handle_task(msg)

        elif msg.type == MessageType.RESULT:
            if isinstance(msg.payload, dict):
                # Support both key names: "_task_id" (new) and "task" (legacy)
                fid = msg.payload.get("_task_id") or msg.payload.get("task")
                if fid and fid in self._result_futures:
                    fut = self._result_futures[fid]
                    if not fut.done():
                        fut.set_result(msg.payload)

    # ── Home Automation intent detection ───────────────────────────────────

    @staticmethod
    def _looks_like_home_automation_request(text: str) -> bool:
        lowered = (text or "").lower()
        if "home assistant" in lowered:
            return True
        if lowered.startswith("spawn ") or lowered.startswith("/"):
            return False

        # Wactorz pipeline requests — these involve external sensors/agents, not HA natively
        # Route to planner instead of HA agent
        _pipeline_keywords = [
            "camera", "webcam", "yolo", "detect", "detection", "person detect",
            "object detect", "laptop camera", "cv2", "opencv",
            "when detected", "if detected", "whenever detected",
            "notify me", "send me a message", "send me a discord",
            "discord", "telegram", "whatsapp",
        ]
        if any(kw in lowered for kw in _pipeline_keywords):
            return False

        has_trigger = any(token in lowered for token in [
            "when ", "if ", "on ", "whenever ", "after ", "before ",
            "as soon as ", "at ",
        ])
        has_action = any(token in lowered for token in [
            "turn on", "turn off", "open", "close", "lock", "unlock", "dim", "set",
        ])
        has_automation_intent = any(token in lowered for token in [
            "automate", "automation", "routine", "scene", "trigger", "schedule",
            "presence", "motion", "door", "window", "sensor", "alarm",
            "romantic", "cozy", "ambience", "ambiance",
        ])
        has_home_context = any(token in lowered for token in [
            "home", "house", "apartment", "room", "living room", "bedroom",
            "kitchen", "hallway", "garage", "porch",
        ])

        return (
            (has_trigger and has_action)
            or (has_trigger and has_automation_intent)
            or (has_automation_intent and has_home_context)
        )

    async def _classify_intent(self, text: str) -> str:
        """
        Classify user intent as ACTUATE, HA, PIPELINE, or OTHER using a single cheap LLM call.
        Returns one of: 'ACTUATE', 'HA', 'PIPELINE', 'OTHER'
        """
        if not text or text.startswith("/"):
            return "OTHER"
        if self.llm is None:
            return "OTHER"
        try:
            decision, _usage = await asyncio.wait_for(
                self.llm.complete(
                    messages=[{"role": "user", "content": text}],
                    system=self.INTENT_CLASSIFIER_PROMPT,
                    max_tokens=10,
                    reasoning_effort="none",
                ),
                timeout=60.0,
            )
            self.total_input_tokens  += _usage.get("input_tokens", 0)
            self.total_output_tokens += _usage.get("output_tokens", 0)
            self.total_cost_usd      += _usage.get("cost_usd", 0.0)
            self._persist_cost()
            token = (decision or "").strip().upper().split()[0] if decision else "OTHER"
            if token in ("HA", "PIPELINE", "OTHER", "ACTUATE"):
                return token
            return "OTHER"
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Intent classification timed out after 60s")
            return "OTHER"
        except Exception as e:
            logger.debug(f"[{self.name}] Intent classification failed: {e}")
            return "OTHER"
            
            
    async def _handle_actuate_intent(self, text: str) -> str:
        if not CONFIG.ha_url or not CONFIG.ha_token:
            return "Home Assistant is not configured. Set `HA_URL` and `HA_TOKEN` in your .env file."

        from .one_off_actuator_agent import OneOffActuatorAgent

        # ── Enrich the request with HA entity context ──────────────────────
        # The OneOffActuatorAgent needs to resolve "lamp" → "light.wiz_rgbw_tunable_02cba0".
        # Without entity context, it fails with "couldn't identify a matching device".
        # Fetch entities via the home-assistant-agent (cached + fast) and inject
        # the relevant matches into the request so the LLM can pick the right one.
        enriched_text = text
        try:
            if self._registry:
                ha_agent = self._registry.find_by_name("home-assistant-agent")
                if ha_agent:
                    # Use a unique task_id so the future resolves correctly
                    _ha_task_id = f"actuate_entities_{uuid.uuid4().hex[:8]}"
                    _ha_future: asyncio.Future = asyncio.get_running_loop().create_future()
                    self._result_futures[_ha_task_id] = _ha_future
                    await self.send(ha_agent.actor_id, MessageType.TASK, {
                        "text": "list_entities",
                        "_task_id": _ha_task_id,
                        "task": _ha_task_id,
                        "reply_to": self.actor_id,
                    })
                    try:
                        ha_result = await asyncio.wait_for(_ha_future, timeout=10.0)
                    except asyncio.TimeoutError:
                        ha_result = None
                    finally:
                        self._result_futures.pop(_ha_task_id, None)

                    entities = []
                    if ha_result and isinstance(ha_result, dict):
                        entities = ha_result.get("entities", []) or ha_result.get("result", [])
                    if isinstance(entities, list) and entities:
                        # Build a compact entity summary for the LLM
                        entity_lines = []
                        for e in entities[:300]:
                            eid = e.get("entity_id", "")
                            name = e.get("name", "") or e.get("friendly_name", "")
                            if eid:
                                entry = eid
                                if name and name != eid:
                                    entry += f" ({name})"
                                entity_lines.append(entry)
                        if entity_lines:
                            enriched_text = (
                                f"{text}\n\n"
                                f"[AVAILABLE HA ENTITIES — match the user's device to one of these:\n"
                                + "\n".join(f"  {e}" for e in entity_lines)
                                + "\n]"
                            )
                            logger.info(
                                f"[{self.name}] Enriched actuate request with "
                                f"{len(entity_lines)} HA entities"
                            )
        except Exception as e:
            logger.warning(f"[{self.name}] Could not fetch HA entities for actuate: {e}")

        task_id = f"actuate_{uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._result_futures[task_id] = future

        try:
            await self.spawn(
                OneOffActuatorAgent,
                request=enriched_text,
                llm_provider=self.llm,
                task_id=task_id,
                reply_to_id=self.actor_id,
                persistence_dir=str(self._persistence_dir.parent),
            )
            result = await asyncio.wait_for(future, timeout=120.0)
            return result.get("result", "Done.")
        except asyncio.TimeoutError:
            return "Actuation timed out, please retry."
        finally:
            self._result_futures.pop(task_id, None)

    async def _is_home_automation_request(self, text: str) -> bool:
        # Keep for backward compat — delegates to _classify_intent
        intent = await self._classify_intent(text)
        return intent == "HA"

    # ── User input ─────────────────────────────────────────────────────────

    @staticmethod
    def _strip_live_context(message: str) -> str:
        """Remove the [CURRENT SYSTEM STATE...][END SYSTEM STATE] prefix if present.
        Used before fact extraction so the auto-injected agent list doesn't get
        treated as user-stated facts."""
        if not isinstance(message, str) or "[CURRENT SYSTEM STATE" not in message:
            return message
        end_marker = "[END SYSTEM STATE]"
        idx = message.find(end_marker)
        if idx == -1:
            return message
        # Skip past the marker and any whitespace following it
        return message[idx + len(end_marker):].lstrip("\n").lstrip()

    async def chat(self, user_message: str) -> str:
        response = await super().chat(user_message)
        # Fire-and-forget fact extraction — strip auto-injected context first
        clean_msg = self._strip_live_context(user_message)
        asyncio.create_task(self._extract_and_save_facts(clean_msg, response))
        return response

    async def chat_stream(self, user_message: str):
        full_response = []
        got_usage = False
        async for chunk in super().chat_stream(user_message):
            if isinstance(chunk, dict):
                got_usage = True
                yield chunk
            else:
                full_response.append(chunk)
                yield chunk
        # Only extract facts when a real LLM response was received (usage dict present).
        # Skips early-exit cases like cost-limit errors so no extra LLM call is made.
        if full_response and got_usage:
            clean_msg = self._strip_live_context(user_message)
            asyncio.create_task(
                self._extract_and_save_facts(clean_msg, "".join(full_response))
            )

    async def _record_external_exchange(self, user_message: str, assistant_response: str):
        """
        Record a turn that was handled OUTSIDE self.chat() / self.chat_stream() —
        i.e. by the HA, ACTUATE, or PIPELINE branches that return before the LLM
        is called on main. Without this, those exchanges vanish from history and
        future turns have no memory of them.

        Mirrors what LLMAgent.chat() does for OTHER turns:
          - append user + assistant to _conversation_history
          - run rolling summarization if needed
          - persist history to disk
          - trigger fact extraction
        """
        if not user_message or assistant_response is None:
            return
        try:
            self.metrics.messages_processed += 1
            self._conversation_history.append({"role": "user",      "content": user_message})
            self._conversation_history.append({"role": "assistant", "content": str(assistant_response)})
            # Same summarization + persistence path that LLMAgent.chat() uses
            await self._maybe_summarize()
            self.persist("conversation_history", self._conversation_history)
        except Exception as e:
            logger.warning(f"[{self.name}] Failed to record external exchange: {e}")
        # Fire-and-forget fact extraction — same as chat()
        asyncio.create_task(
            self._extract_and_save_facts(user_message, str(assistant_response))
        )


    def _drain_notifications(self) -> str:
        """Pop queued monitor notifications as a formatted prefix string."""
        if not self._pending_notifications:
            return ""
        icons = {"critical": "\U0001f534", "warning": "\U0001f7e1", "info": "\u2705"}
        lines = []
        for n in self._pending_notifications:
            icon = icons.get(n.get("severity", "warning"), "\u26a0\ufe0f")
            lines.append(f"{icon} **System:** {n.get('message', '').strip()}")
        self._pending_notifications.clear()
        return "\n".join(lines) + "\n\n---\n\n"

    async def process_user_input(self, text: str) -> str:
        note_prefix = self._drain_notifications()

        # ── Pending-plan response detection ─────────────────────────────────
        # If there's a dry-run plan waiting for approval and the user's message
        # looks like "yes"/"no", handle it BEFORE any other processing. This
        # must come first so a bare "yes" doesn't accidentally hit the intent
        # classifier or get treated as a new prompt. Slash commands and other
        # explicit prefixes are NOT intercepted (the user might want to inspect
        # /plans or /registry while a plan is pending).
        if not text.strip().startswith("/") and not text.strip().startswith("@"):
            plan_response = await self._handle_pending_plan_response(text)
            if plan_response is not None:
                # Record the exchange so history reflects the approval/rejection
                await self._record_external_exchange(text, plan_response)
                return note_prefix + plan_response

            # Pending-plan ambiguity guard: if the user has a plan pending
            # and now types something that looks like another spawn / pipeline
            # request, we'd otherwise silently create the new agents AND
            # later spawn the pending ones too — duplicates galore. Warn
            # the user and ask them to resolve the pending plan first.
            warn = self._warn_if_pending_plan_collision(text)
            if warn:
                await self._record_external_exchange(text, warn)
                return note_prefix + warn

        # ── Direct API intercepts — handle without LLM round-trip ──────────
        stripped = text.strip().rstrip("()")

        # ── /help ───────────────────────────────────────────────────────────
        if stripped in ("/help", "help", "/?"):
            return note_prefix + "\n".join([
                "**Wactorz commands**",
                "",
                "**Agents**",
                "  /agents                 — list all known agents with descriptions and schemas",
                "  /agents <keyword>       — filter agents by capability keyword",
                "  /capabilities           — alias for /agents",
                "  /delete <agent>         — stop an agent and remove it from the spawn registry",
                "  /stop <agent>           — alias of /delete",
                "  /pause <agent>          — pause a local agent (remote not supported)",
                "  /resume <agent>         — resume a paused local agent",
                "  @agent-name <msg>       — send a message directly to a named agent",
                "  @catalog list           — list available catalog recipes",
                "  @catalog spawn <n>      — spawn a catalog agent",
                "",
                "**Nodes**",
                "  /nodes                  — list local + remote nodes and their agents",
                "  /nodes restart <node>   — restart the runner process on a node",
                "  /nodes shutdown <node>  — stop all agents and shut down a node",
                "  /nodes remove <node>    — stop all agents on a node and remove it",
                "  /deploy <node> [host [user [pw [broker]]]]",
                "                          — deploy a remote Wactorz node",
                "                            (run with just <node> to auto-discover hosts)",
                "  /migrate <agent> <node> — move an agent to a different node (state preserved)",
                "  /agents restart <name>  — restart an agent (local or remote, state preserved)",
                "",
                "**Pipelines & Plans**",
                "  /plans                  — list pending pipeline proposals (dry-run)",
                "  /plans show <id>        — inspect a proposal's full code",
                "  /plans approve <id>     — execute a proposed pipeline",
                "  /plans reject <id>      — discard a proposed pipeline",
                "  /clear-plans            — clear the plan cache",
                "  /rules                  — list active pipeline rules",
                "  /rules delete <id>      — stop agents and remove a rule",
                "  pipeline! <task>        — bypass approval and execute immediately (power users)",
                "",
                "**Memory**",
                "  /memory                 — show stored user facts and conversation summary",
                "  /memory clear           — wipe all memory",
                "  /memory forget <key>    — remove one stored fact",
                "",
                "**Notifications**",
                "  /webhook                — list stored webhook URLs",
                "  /webhook discord <url>  — store a Discord webhook URL",
                "  /webhook telegram <url> — store a Telegram webhook URL",
                "",
                "**System & diagnostics**",
                "  /topics                 — list MQTT topics published by known agents",
                "  /topics <keyword>       — filter topics by keyword",
                "  /bus                    — TopicBus registry: contracts, data flows, wiring pairs",
                "  /mqtt                   — MQTT publisher status (connected, queue depth, outbox)",
                "  /registry               — diagnostic: compare live registry, spawn registry, manifest cache",
                "  /help                   — show this help",
            ])
        if stripped in ("main.list_nodes", "list_nodes", "/nodes"):
            nodes = self.list_nodes()
            import time as _t

            # Local row first — matches the format users got from io_agent
            local_agents = []
            if self._registry:
                local_agents = sorted(a.name for a in self._registry.all_actors())
            local_str = ", ".join("@" + n for n in local_agents) or "(none)"
            lines = [f"  {'local':22s} 🟢 online  |  agents: {local_str}"]

            # Remote rows
            for nd in sorted(nodes, key=lambda x: x["node"]):
                status   = "🟢 online " if nd["online"] else "🔴 offline"
                agents   = ", ".join("@" + a for a in nd["agents"]) or "(no agents)"
                age      = int(_t.time() - nd["last_seen"])
                lines.append(f"  {nd['node']:22s} {status}  |  agents: {agents}  |  last heartbeat: {age}s ago")

            footer = ""
            if not nodes:
                footer = "\n(no remote nodes seen yet — deploy one with /deploy <node-name>)"
            else:
                footer = "\nTo remove a remote node: /nodes remove <node-name>"

            return note_prefix + "Nodes:\n" + "\n".join(lines) + footer

        if stripped.startswith("/topics"):
            keyword = stripped[7:].strip().lstrip("(").rstrip(")")
            topics = self.list_topics(keyword)
            if not topics:
                msg = f"No topics found" + (f" matching '{keyword}'" if keyword else "") + "."
                msg += " Topics are registered automatically when agents publish for the first time."
                return note_prefix + msg
            lines = [f"Known MQTT topics{' matching ' + repr(keyword) if keyword else ''}:"]
            for t in topics:
                agent_strs = ", ".join(
                    f"{a['name']}" + (f" ({a['node']})" if a.get("node") else "")
                    for a in t["agents"]
                )
                lines.append(f"  {t['topic']:40s} ← {agent_strs}")
            return note_prefix + "\n".join(lines)
            
        if stripped == "/mqtt":
            client = self._mqtt_client
            if client is None:
                return note_prefix + "MQTT publisher not initialised."
            connected   = getattr(client, "connected",   False)
            queue_depth = getattr(client, "queue_depth", 0)
            client_id   = getattr(client, "_client_id",  "?")
            db_path     = getattr(client, "_db_path",    "?")
            status_icon = "🟢" if connected else "🔴"
            lines = [
                f"MQTT Publisher Status:",
                f"  {status_icon} connected   : {connected}",
                f"  client_id   : {client_id}",
                f"  queue_depth : {queue_depth} message(s) pending",
                f"  outbox_db   : {db_path}",
                f"  QoS 1 topics: nodes/*, agents/by-name/*",
                f"  QoS 0 topics: */logs, */metrics, */status, */heartbeat",
            ]
            if queue_depth > 0:
                lines.append(f"  ⚠️  {queue_depth} message(s) queued — will deliver when reconnected")
            return note_prefix + "\n".join(lines)

        if stripped == "/bus":
            try:
                from ..core.topic_bus import get_topic_bus
                bus = get_topic_bus()
                if not bus:
                    return note_prefix + "TopicBus not initialised."
                summary = bus.registry.summary()
                lines = [
                    f"TopicBus — Reactive Pub/Sub Registry",
                    f"  agents with contracts : {summary['total_agents']}",
                    f"  published topics      : {summary['total_published']}",
                    f"  subscribed topics     : {summary['total_subscribed']}",
                    f"  auto-wiring pairs     : {summary['wiring_pairs']}",
                    "",
                ]
                for c in sorted(summary["agents"], key=lambda x: x["name"]):
                    lines.append(f"  [{c['name']}]" + (f" on {c['node']}" if c.get("node") else ""))
                    if c["publishes"]:
                        lines.append(f"    publishes : {', '.join(c['publishes'])}")
                    if c["subscribes"]:
                        lines.append(f"    subscribes: {', '.join(c['subscribes'])}")
                    if c.get("triggers_when"):
                        lines.append(f"    triggers  : {c['triggers_when']}")
                pairs = bus.registry.find_wiring_opportunities()
                if pairs:
                    lines.append("\nAuto-wiring opportunities:")
                    for prod, cons, topic in pairs:
                        lines.append(f"  {prod.name} → {cons.name}  via {topic}")
                return note_prefix + "\n".join(lines)
            except Exception as e:
                return note_prefix + f"TopicBus error: {e}"



        # ── Webhook / notification URL management ───────────────────────────
        if stripped.startswith("/memory"):
            parts = stripped.split(None, 1)
            sub = parts[1].strip() if len(parts) > 1 else ""
            if sub == "clear":
                self.persist("_user_facts", {})
                self.persist("history_summary", "")
                self._history_summary = ""
                # Rebuild fresh — running agents block is preserved, facts are gone
                self._rebuild_system_prompt()
                return note_prefix + "Memory cleared — user facts and conversation summary reset."
            if sub.startswith("forget "):
                key = sub[7:].strip()
                facts = self.get_user_facts()
                if key in facts:
                    del facts[key]
                    self.persist("_user_facts", facts)
                    self._inject_user_facts_into_prompt()
                    return note_prefix + f"Forgotten: '{key}'"
                return note_prefix + f"No fact found with key '{key}'."
            # Default: show memory, grouped by bucket so it's easy to scan
            facts = self.get_user_facts()
            summary = self._history_summary
            lines = []
            if facts:
                lines.append(f"User facts ({len(facts)}):")
                buckets = [
                    ("pref_",   "Preferences & identity"),
                    ("device_", "Devices & setup"),
                    ("policy_", "Standing policies"),
                ]
                shown = set()
                for prefix, heading in buckets:
                    items = [(k, v) for k, v in facts.items() if k.startswith(prefix)]
                    if items:
                        lines.append(f"\n  [{heading}]")
                        for k, v in sorted(items):
                            lines.append(f"    {k[len(prefix):]}: {v}")
                            shown.add(k)
                # Anything left over (legacy or unprefixed keys)
                leftover = [(k, v) for k, v in facts.items() if k not in shown]
                if leftover:
                    lines.append(f"\n  [Other / legacy]")
                    for k, v in sorted(leftover):
                        lines.append(f"    {k}: {v}")
            else:
                lines.append("No user facts stored yet.")
            if summary:
                lines.append(f"\nConversation summary:\n  {summary[:300]}{'...' if len(summary) > 300 else ''}")
            else:
                lines.append("\nNo conversation summary yet.")
            lines.append("\nCommands: /memory clear | /memory forget <key>")
            return note_prefix + "\n".join(lines)

        if stripped.startswith("/webhook"):
            parts = stripped.split(None, 2)
            if len(parts) == 1:
                # /webhook — show stored URLs
                urls = self.recall("_notification_urls") or {}
                if not urls:
                    return note_prefix + "No notification URLs stored.\nUse: /webhook discord <url>  or  /webhook telegram <url>"
                lines = ["Stored notification URLs:"]
                for svc, url in urls.items():
                    lines.append(f"  {svc}: {url}")
                return note_prefix + "\n".join(lines)
            elif len(parts) >= 3:
                # /webhook discord <url>
                service = parts[1].lower()
                url = parts[2].strip()
                urls = self.recall("_notification_urls") or {}
                urls[service] = url
                self.persist("_notification_urls", urls)
                return note_prefix + f"Saved {service} webhook URL. Pipelines will use it automatically."
            else:
                return note_prefix + "Usage: /webhook <service> <url>\nExample: /webhook discord https://discord.com/api/webhooks/..."

        # Auto-detect webhook URLs in any message and persist them
        import re as _re
        _webhook_match = _re.search(
            r'https?://(?:discord\.com/api/webhooks|hooks\.slack\.com|api\.telegram\.org)/\S+',
            text
        )
        if _webhook_match:
            url = _webhook_match.group(0).rstrip(".,;!)'\"")
            urls = self.recall("_notification_urls") or {}
            if "discord" in url:
                urls["discord"] = url
            elif "slack" in url:
                urls["slack"] = url
            elif "telegram" in url:
                urls["telegram"] = url
            self.persist("_notification_urls", urls)
            logger.info(f"[{self.name}] Auto-saved webhook URL from message")

        if stripped in ("/rules", "rules"):
            rules = self.get_pipeline_rules()
            if not rules:
                return note_prefix + "No pipeline rules active.\nDescribe a reactive rule to create one, e.g. 'when the door opens send me a Discord message'."
            lines = [f"Active pipeline rules ({len(rules)}):"]
            for rule_id, rule in sorted(rules.items(), key=lambda x: x[1].get("created_at", 0)):
                agents = rule.get("agents", [])
                task = rule.get("task", "")[:80]
                import datetime
                ts = rule.get("created_at", 0)
                created = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "unknown"
                # Check which agents are running
                running_agents = []
                stopped_agents = []
                for a in agents:
                    if self._registry and self._registry.find_by_name(a):
                        running_agents.append(a)
                    else:
                        stopped_agents.append(a)
                status = "🟢" if running_agents else "🔴"
                lines.append(f"\n{status} [{rule_id}] — {task}")
                lines.append(f"   agents  : {', '.join(agents)}")
                if stopped_agents:
                    lines.append(f"   stopped : {', '.join(stopped_agents)}")
                lines.append(f"   created : {created}")
            lines.append("\nTo delete a rule: /rules delete <rule_id>")
            return note_prefix + "\n".join(lines)

        if stripped.startswith("/rules delete "):
            rule_id = stripped[len("/rules delete "):].strip()
            result = await self.delete_pipeline_rule(rule_id)
            return note_prefix + result

        # ── /delete <agent>, /stop <agent> — direct shortcuts ──────────────
        # Same behaviour as `/agents delete <name>` / `/agents stop <name>`,
        # but as a top-level command so users (and main itself) don't need to
        # round-trip through the LLM. Reuses the unified handler below by
        # rewriting `stripped` and falling through.
        for _short, _full in (("/delete ", "/agents delete "),
                              ("/stop ",   "/agents stop ")):
            if stripped.startswith(_short):
                stripped = _full + stripped[len(_short):].strip()
                break  # one match — fall through to the unified block

        # ── /migrate <agent> <node> ─────────────────────────────────────────
        # Moved here from io_agent so all interfaces (CLI, UI, Discord) share
        # one implementation. The actual work is done by self.migrate_agent().
        if stripped.startswith("/migrate"):
            parts = stripped.split()
            if len(parts) < 3:
                return note_prefix + (
                    "Usage: /migrate <agent-name> <target-node>\n"
                    "Examples:\n"
                    "  /migrate temp-sensor rpi-bedroom   — remote to remote\n"
                    "  /migrate temp-sensor local         — remote back to main node\n"
                    "  /migrate temp-sensor rpi-a         — local to remote"
                )
            agent_name, target_node = parts[1], parts[2]
            try:
                result = await self.migrate_agent(agent_name, target_node)
            except Exception as exc:
                logger.exception(f"[main] /migrate failed for '{agent_name}' → '{target_node}'")
                return note_prefix + f"Migrate failed: {exc}"
            sym = "OK" if result.get("success") else "FAIL"
            return note_prefix + f"[{sym}] {result.get('message', str(result))}"

        # ── /deploy (non-streaming path) ────────────────────────────────────
        # The streaming version yields progress chunks live; this version
        # collects them and returns one joined string. Callers without
        # streaming (Discord, REST, CLI input()) get the full transcript at
        # the end. Implementation lives in _slash_deploy_stream so there is
        # exactly one source of truth for what /deploy does.
        if stripped.startswith("/deploy"):
            chunks: list[str] = []
            async for chunk in self._slash_deploy_stream(stripped):
                if isinstance(chunk, str):
                    chunks.append(chunk)
            return note_prefix + "\n".join(chunks)

        # ── /clear-plans ────────────────────────────────────────────────────
        if stripped == "/clear-plans":
            try:
                self.persist("_plan_cache", {})
            except Exception as exc:
                logger.exception("[main] /clear-plans failed")
                return note_prefix + f"Failed to clear plan cache: {exc}"
            return note_prefix + "Plan cache cleared."

        # ── /pause <agent>, /resume <agent> ─────────────────────────────────
        # NOTE: the underlying remote_runner.py does not implement pause/resume
        # topics, so these only affect LOCAL agents. For remote agents we tell
        # the user honestly and suggest /stop instead.
        for _cmd, _verb, _new_state in (
            ("/pause ",  "pause",  ActorState.PAUSED),
            ("/resume ", "resume", ActorState.RUNNING),
        ):
            if stripped.startswith(_cmd):
                agent_name = stripped[len(_cmd):].strip()
                if not agent_name:
                    return note_prefix + f"Usage: {_cmd.strip()} <agent-name>"

                # Remote check first — fail fast with a clear message
                reg  = self._get_spawn_registry()
                node = reg.get(agent_name, {}).get("node", "").strip()
                if node:
                    return note_prefix + (
                        f"'{agent_name}' is running on remote node '{node}'. "
                        f"Pause/resume is only supported for local agents. "
                        f"Use /stop {agent_name} to stop it instead."
                    )

                if not self._registry:
                    return note_prefix + "No actor registry available."

                target = self._registry.find_by_name(agent_name)
                if target is None:
                    return note_prefix + f"Agent '{agent_name}' not found locally."

                # Idempotent guards — be explicit so the user knows nothing changed
                if _verb == "pause" and target.state == ActorState.PAUSED:
                    return note_prefix + f"Agent '{agent_name}' is already paused."
                if _verb == "resume" and target.state != ActorState.PAUSED:
                    return note_prefix + (
                        f"Agent '{agent_name}' is not paused (state: {target.state.name})."
                    )

                try:
                    if _verb == "pause":
                        await target.pause()
                    else:
                        await target.resume()
                except Exception as exc:
                    logger.exception(f"[main] /{_verb} failed for '{agent_name}'")
                    return note_prefix + f"Failed to {_verb} '{agent_name}': {exc}"

                return note_prefix + f"Agent '{agent_name}' {_verb}d."

        # ── /agents stop|delete|pause|remove <name> ─────────────────────────
        # NOTE: stop and pause are reversible (state preserved, spawn registry
        # entry kept if you ever want to /agents restart). delete and remove
        # are PERMANENT — they go through delete_spawned_agent(), which wipes
        # the on-disk state file, purges retained MQTT topics, and removes the
        # entry from the spawn registry. Picking the wrong verb here is the
        # difference between "the agent comes back on next runner restart" and
        # "the agent is gone forever".
        for _cmd in ("/agents stop ", "/agents delete ", "/agents pause ", "/agents remove "):
            if stripped.startswith(_cmd):
                agent_name = stripped[len(_cmd):].strip()
                verb       = _cmd.strip().split()[-1]   # "stop" | "delete" | "pause" | "remove"
                is_delete  = verb in ("delete", "remove")

                if is_delete:
                    # Delegate to the canonical permanent-delete helper so the
                    # chat path and the LLM <delete> path behave identically.
                    try:
                        await self.delete_spawned_agent(agent_name)
                    except Exception as e:
                        logger.exception(f"[{self.name}] delete_spawned_agent('{agent_name}') failed")
                        return note_prefix + f"Delete of '{agent_name}' failed: {e}"
                    return note_prefix + (
                        f"Agent '{agent_name}' permanently deleted "
                        f"(state file removed, retained MQTT topics cleared)."
                    )

                # stop / pause — reversible. Keep state and spawn-registry entry.
                reg  = self._get_spawn_registry()
                node = reg.get(agent_name, {}).get("node", "").strip()
                # Past-tense rendering: stop → stopped, pause → paused.
                # Both are double-the-consonant + ed (English regular doubling
                # for monosyllabic CVC verbs); just hard-code the table.
                past = {"stop": "stopped", "pause": "paused"}.get(verb, f"{verb}ped")

                if node:
                    # Remote agent — plain stop (no delete flag), keep registry
                    # so /agents restart can resume it cleanly later.
                    await self._mqtt_publish(
                        f"nodes/{node}/stop", {"name": agent_name}, qos=1
                    )
                    self._record_agent_deletion(
                        agent_name,
                        reason=f"manually {past} via /agents on node '{node}'"
                    )
                    return note_prefix + (
                        f"Stop signal sent to '{agent_name}' on node '{node}'. "
                        f"State is preserved — use /agents restart {agent_name} to resume."
                    )
                else:
                    # Local agent
                    if self._registry:
                        target = self._registry.find_by_name(agent_name)
                        if target:
                            actor_id = target.actor_id
                            await self._registry.unregister(actor_id)
                            await target.stop()
                            self._record_agent_deletion(
                                agent_name, reason=f"manually {past} via /agents"
                            )
                            return note_prefix + (
                                f"Agent '{agent_name}' {past}. "
                                f"State is preserved — use /agents restart {agent_name} to resume."
                            )
                    return note_prefix + f"Agent '{agent_name}' not found locally."

        # ── /agents restart <name> ──────────────────────────────────────────
        if stripped.startswith("/agents restart "):
            agent_name = stripped[len("/agents restart "):].strip()
            if not agent_name:
                return note_prefix + "Usage: /agents restart <agent-name>"
            reg  = self._get_spawn_registry()
            node = reg.get(agent_name, {}).get("node", "").strip()
            if node:
                await self._mqtt_publish(
                    f"nodes/{node}/restart_agent", {"name": agent_name}, qos=1
                )
                return note_prefix + (
                    f"Restart signal sent to '{agent_name}' on node '{node}'. "
                    f"State is preserved — the agent will resume from its last saved state."
                )
            else:
                # Local agent — stop and re-spawn using config from spawn registry
                config = reg.get(agent_name)
                if not config:
                    return note_prefix + f"Agent '{agent_name}' not found in spawn registry."
                if self._registry:
                    target = self._registry.find_by_name(agent_name)
                    if target:
                        await self._registry.unregister(target.actor_id)
                        await target.stop()
                new_config = dict(config)
                new_config["replace"] = True
                await self._spawn_from_config(new_config, save=True)
                return note_prefix + f"Agent '{agent_name}' restarted locally."

        # ── /nodes remove <node> ────────────────────────────────────────────
        if stripped.startswith("/nodes remove "):
            node_name = stripped[len("/nodes remove "):].strip()
            # Clear retained MQTT messages
            await self._mqtt_publish(f"nodes/{node_name}/spawn",         b"", retain=True)
            await self._mqtt_publish(f"nodes/{node_name}/desired_state", b"", retain=True)
            await self._mqtt_publish(f"nodes/{node_name}/stop_all",      {"reason": "removed"}, qos=1)
            # Remove all agents for this node from spawn registry
            reg     = self._get_spawn_registry()
            removed = [n for n, c in reg.items() if c.get("node", "") == node_name]
            for n in removed:
                self._remove_from_spawn_registry(n)
            self._known_nodes.pop(node_name, None)
            return note_prefix + (
                f"Node '{node_name}' removed. "
                f"Cleared {len(removed)} agent(s): {', '.join(removed) or 'none'}. "
                f"The node will disappear from /nodes within 30s."
            )

        # ── /nodes restart <node> ───────────────────────────────────────────
        if stripped.startswith("/nodes restart "):
            node_name = stripped[len("/nodes restart "):].strip()
            if not node_name:
                return note_prefix + "Usage: /nodes restart <node-name>"
            info = self._known_nodes.get(node_name)
            if not info:
                return note_prefix + (
                    f"Node '{node_name}' is not known. Check /nodes for online nodes."
                )
            await self._mqtt_publish(
                f"nodes/{node_name}/restart", {"reason": "user request"}, qos=1
            )
            return note_prefix + (
                f"Restart signal sent to node '{node_name}'. "
                f"It will briefly drop offline then reappear within ~15s."
            )

        # ── /nodes shutdown <node> ──────────────────────────────────────────
        if stripped.startswith("/nodes shutdown "):
            node_name = stripped[len("/nodes shutdown "):].strip()
            if not node_name:
                return note_prefix + "Usage: /nodes shutdown <node-name>"
            await self._mqtt_publish(
                f"nodes/{node_name}/stop_all", {"reason": "user request"}, qos=1
            )
            return note_prefix + (
                f"Shutdown signal sent to node '{node_name}'. "
                f"All agents will stop. The node will disappear from /nodes within 30s. "
                f"(If systemd manages the runner, it will restart automatically.)"
            )

        # ── /agents / /capabilities ─────────────────────────────────────────
        if stripped in ("/agents", "/capabilities") or \
                stripped.startswith("/agents ") or stripped.startswith("/capabilities "):
            keyword = ""
            for prefix in ("/capabilities ", "/agents "):
                if stripped.startswith(prefix):
                    keyword = stripped[len(prefix):].strip()
                    break
            caps = self.list_capabilities(keyword)
            if not caps:
                msg = "No agents found" + (f" matching {repr(keyword)}" if keyword else "") + "."
                msg += " Agents publish their capabilities on startup."
                return note_prefix + msg
            lines = ["Agent capabilities" + (" matching " + repr(keyword) if keyword else "") + ":"]
            for a in caps:
                running  = "\U0001f7e2" if a["running"] else ("\U0001f4e6" if a["spawnable"] else "\U0001f534")
                node_str = f" on {a['node']}" if a.get("node") else ""
                lines.append("")
                lines.append(f"  {running} [{a['name']}]{node_str}")
                lines.append(f"    description : {a['description']}")
                if a["capabilities"]:
                    lines.append(f"    capabilities: {', '.join(a['capabilities'])}")
                if a["input_schema"]:
                    lines.append(f"    input       : {a['input_schema']}")
                if a["output_schema"]:
                    lines.append(f"    output      : {a['output_schema']}")
                if a["spawnable"]:
                    lines.append(f"    spawnable   : yes — @catalog spawn {a['name']}")
            lines.append("\nLegend: \U0001f7e2 running  \U0001f4e6 spawnable (not yet running)  \U0001f534 stopped")
            lines.append("Filter: /agents <keyword>   e.g. /agents discord")
            return note_prefix + "\n".join(lines)

        # ── /registry — diagnostic: compare all three sources of truth ──────
        if stripped == "/registry":
            # 1. Live in-memory registry — what's actually running in this process
            live_names = (
                {a.name for a in self._registry.all_actors()}
                if self._registry else set()
            )
            # Skip housekeeping actors so the comparison focuses on user agents
            housekeeping = {"main", "monitor", "installer", "home-assistant-agent",
                            "anomaly-detector", "code-agent"}
            live_user = live_names - housekeeping

            # 2. Spawn registry — what main intends to have running (persisted)
            spawn_reg = self._get_spawn_registry()
            spawn_names = set(spawn_reg.keys())

            # 3. Manifest cache — every agent that has ever announced itself,
            #    including remote ones on other nodes
            manifest_names = set(self._agent_manifests.keys()) - housekeeping

            # 4. Node heartbeats — what each remote node says it's running
            heartbeat_names: set[str] = set()
            for nd_info in self._known_nodes.values():
                heartbeat_names.update(nd_info.get("agents", []))

            lines = ["**Agent registry diagnostic**", ""]

            # ── Live registry ──
            lines.append("\U0001f7e2 **Live registry** (running NOW in this process):")
            if live_user:
                for name in sorted(live_user):
                    actor = self._registry.find_by_name(name)
                    state = actor.state.name if actor else "?"
                    lines.append(f"    {name}  ({state})")
            else:
                lines.append("    (none)")

            # ── Spawn registry ──
            lines.append("")
            lines.append("\U0001f4be **Spawn registry** (auto-restore on restart, persisted to disk):")
            if spawn_names:
                for name in sorted(spawn_names):
                    cfg  = spawn_reg.get(name, {})
                    node = cfg.get("node", "").strip() or "local"
                    lines.append(f"    {name}  on {node}")
            else:
                lines.append("    (none)")

            # ── Manifest cache ──
            lines.append("")
            lines.append("\U0001f4e6 **Manifest cache** (announced via MQTT — includes remote agents):")
            if manifest_names:
                for name in sorted(manifest_names):
                    m    = self._agent_manifests.get(name, {})
                    node = m.get("node") or "local"
                    lines.append(f"    {name}  on {node}")
            else:
                lines.append("    (none)")

            # ── Discrepancy report — this is the value-add ──
            issues = []
            # Live but not in spawn registry → an ad-hoc spawn that won't survive restart
            for name in sorted(live_user - spawn_names):
                issues.append(f"\u26a0\ufe0f  '{name}' is RUNNING but NOT in spawn registry — won't auto-restore on restart")
            # Spawn registry says local but not live → main thinks it should be running
            for name in sorted(spawn_names - live_names):
                cfg = spawn_reg.get(name, {})
                if not cfg.get("node", "").strip():   # local-only check
                    issues.append(f"\u26a0\ufe0f  '{name}' is in spawn registry but NOT running locally — start failed or was stopped without cleanup")
            # In manifest but not live and not in spawn registry → ghost
            ghosts = manifest_names - live_user - spawn_names - heartbeat_names
            for name in sorted(ghosts):
                issues.append(f"\U0001f47b '{name}' is in manifest cache but nowhere else — stale entry, run `/agents delete {name}` to clean up")
            # In spawn registry as remote, but the node is offline / not heartbeating
            online_nodes = {n for n, info in self._known_nodes.items()
                             if (__import__("time").time() - info.get("last_seen", 0)) < 30}
            for name, cfg in spawn_reg.items():
                node = cfg.get("node", "").strip()
                if node and node not in online_nodes:
                    issues.append(f"\u26a0\ufe0f  '{name}' assigned to node '{node}' which is OFFLINE — agent unreachable")

            lines.append("")
            if issues:
                lines.append("**Discrepancies found:**")
                for s in issues:
                    lines.append(f"  {s}")
            else:
                lines.append("\u2705 All three sources agree — registry is consistent.")

            return note_prefix + "\n".join(lines)

        # ── /plans — pending dry-run proposals ──────────────────────────────
        if stripped == "/plans" or stripped.startswith("/plans "):
            parts = stripped.split(None, 2)
            sub = parts[1] if len(parts) > 1 else ""

            # /plans show <id>
            if sub == "show" and len(parts) == 3:
                pid = parts[2]
                p = self.get_pending_plans().get(pid)
                if not p:
                    return note_prefix + f"No plan with id `{pid}`."
                envelope = p.get("envelope", {})
                agents = envelope.get("plan", [])
                lines = [self._format_plan_proposal(p), "", "**Full agent code:**"]
                for step in agents:
                    name = step.get("name", "?")
                    code = step.get("spawn_config", {}).get("code", "") or "(no code — pre-built type)"
                    lines.append(f"\n--- {name} ---")
                    lines.append("```python")
                    lines.append(code[:2000])
                    if len(code) > 2000:
                        lines.append(f"... ({len(code) - 2000} more chars truncated)")
                    lines.append("```")
                return note_prefix + "\n".join(lines)

            # /plans approve <id>
            if sub == "approve" and len(parts) == 3:
                pid = parts[2]
                p = self.get_pending_plans().get(pid)
                if not p:
                    return note_prefix + f"No plan with id `{pid}`."
                if p.get("status") != "pending":
                    return note_prefix + f"Plan `{pid}` is `{p.get('status')}`, not pending."
                return note_prefix + await self._execute_pending_plan(p)

            # /plans reject <id>
            if sub == "reject" and len(parts) == 3:
                pid = parts[2]
                p = self.get_pending_plans().get(pid)
                if not p:
                    return note_prefix + f"No plan with id `{pid}`."
                if p.get("status") != "pending":
                    return note_prefix + f"Plan `{pid}` is `{p.get('status')}`, not pending."
                return note_prefix + self._reject_pending_plan(p)

            # /plans clear — drop all non-pending plans (housekeeping)
            if sub == "clear":
                plans = self.recall(PENDING_PLANS_KEY) or {}
                kept = {pid: p for pid, p in plans.items() if p.get("status") == "pending"}
                dropped = len(plans) - len(kept)
                self.persist(PENDING_PLANS_KEY, kept)
                return note_prefix + f"Cleared {dropped} resolved plan(s). {len(kept)} still pending."

            # /plans (no args) — list
            plans = self.get_pending_plans()
            pending  = [p for p in plans.values() if p.get("status") == "pending"]
            resolved = [p for p in plans.values() if p.get("status") != "pending"]
            lines = []
            if pending:
                lines.append(f"**Pending plans ({len(pending)})** — awaiting your approval")
                for p in sorted(pending, key=lambda x: -x.get("created_at", 0)):
                    pid     = p.get("plan_id", "?")
                    task    = p.get("task", "?")[:60]
                    n_agents = len(p.get("envelope", {}).get("plan", []))
                    age_s   = int(__import__("time").time() - p.get("created_at", 0))
                    lines.append(f"  `{pid}` ({n_agents} agent(s), {age_s}s ago) — {task}")
                lines.append("\n  /plans show <id>      — see full plan with code")
                lines.append("  /plans approve <id>   — execute the plan")
                lines.append("  /plans reject <id>    — discard the plan")
            else:
                lines.append("No pending plans.")
            if resolved:
                lines.append(f"\n_Recent resolved plans ({len(resolved)})_:")
                for p in sorted(resolved, key=lambda x: -x.get("created_at", 0))[:5]:
                    pid    = p.get("plan_id", "?")
                    status = p.get("status", "?")
                    task   = p.get("task", "?")[:50]
                    icon   = {"approved": "\u2705", "rejected": "\u274c",
                              "expired": "\u23f0", "superseded": "\u21bb"}.get(status, "?")
                    lines.append(f"  {icon} `{pid}` ({status}) — {task}")
                lines.append("\n  /plans clear          — drop resolved entries")
            return note_prefix + "\n".join(lines)

                # ── @mention direct routing ─────────────────────────────────────────
        if text.startswith("@"):
            # Extract agent name and message: "@cpu-monitor-rpi-room what is the cpu?"
            parts       = text.split(None, 1)
            target_name = parts[0].lstrip("@").rstrip(":,")
            message     = parts[1].strip() if len(parts) > 1 else text

            # Try local registry first
            local_target = self._registry.find_by_name(target_name) if self._registry else None
            if not local_target:
                # Not running — check if it's a spawnable catalog recipe
                manifest = self._agent_manifests.get(target_name, {})
                if manifest.get("spawnable") and manifest.get("catalog"):
                    catalog_name  = manifest["catalog"]
                    catalog_actor = self._registry.find_by_name(catalog_name) if self._registry else None
                    if catalog_actor and hasattr(catalog_actor, "_action_spawn"):
                        logger.info(f"[main] '{target_name}' not running — auto-spawning via {catalog_name}...")
                        try:
                            spawn_result = await catalog_actor._action_spawn(target_name, {})
                            if spawn_result and spawn_result.get("ok"):
                                await asyncio.sleep(0.5)
                                local_target = self._registry.find_by_name(target_name) if self._registry else None
                                logger.info(f"[main] '{target_name}' spawned, routing task...")
                            else:
                                err = spawn_result.get("message", "unknown error") if spawn_result else "no response"
                                return note_prefix + f"Could not spawn '{target_name}': {err}"
                        except Exception as e:
                            return note_prefix + f"Could not spawn '{target_name}': {e}"

            if local_target:
                result = await self.delegate_task(target_name, message, timeout=60.0)
                if result:
                    reply = result.get("result") or result.get("response") or str(result)
                    return note_prefix + f"**{target_name}**: {reply}"
                return note_prefix + f"{target_name} did not respond."

            # Check if it's a known remote agent
            remote_node = None
            for node_name, nd in self._known_nodes.items():
                if target_name in nd.get("agents", []):
                    remote_node = node_name
                    break

            if remote_node:
                # Send via MQTT and wait for reply
                import time as _t
                reply_topic = f"main/reply/{self.actor_id}/{uuid.uuid4().hex[:8]}"
                future: asyncio.Future = asyncio.get_event_loop().create_future()
                self._result_futures[reply_topic] = future

                await self._mqtt_publish(
                    f"agents/by-name/{target_name}/task",
                    {"text": message, "_reply_topic": reply_topic,
                     "_remote_task": True, "payload": message},
                )

                # Subscribe briefly for the reply
                async def _wait_reply():
                    try:
                        import aiomqtt
                        async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                            await client.subscribe(reply_topic)
                            async for msg in client.messages:
                                try:
                                    data = json.loads(msg.payload.decode())
                                    if not future.done():
                                        future.set_result(data)
                                except Exception:
                                    pass
                                return
                    except Exception as e:
                        if not future.done():
                            future.set_exception(e)

                reply_task = asyncio.create_task(_wait_reply())
                try:
                    result = await asyncio.wait_for(asyncio.shield(future), timeout=30.0)
                    reply_task.cancel()
                    reply = result.get("result") or result.get("response") or str(result)
                    return note_prefix + f"**{target_name}** (on {remote_node}): {reply}"
                except asyncio.TimeoutError:
                    reply_task.cancel()
                    return note_prefix + f"{target_name} on {remote_node} did not respond within 30s."
                finally:
                    self._result_futures.pop(reply_topic, None)

            # Not found locally or remotely
            known_remote = [a for nd in self._known_nodes.values() for a in nd.get("agents", [])]
            if known_remote:
                return note_prefix + (f"Agent '{target_name}' not found. "
                    f"Remote agents: {', '.join(known_remote)}")
            return note_prefix + f"Agent '{target_name}' not found."

        # Explicit planner prefix always wins
        lowered = text.lower()
        if any(lowered.startswith(p) for p in (
            "coordinate:", "coordinate ", "plan:", "pipeline:", "pipeline ",
            "@planner", "set up a pipeline", "create a rule", "set up a rule",
        )):
            result = await self._run_planner(text)
            response = result or "Planner did not return a result. Please retry."
            await self._record_external_exchange(text, response)
            return note_prefix + response

        # Single LLM call classifies intent: ACTUATE, HA, PIPELINE (reactive rule), OTHER
        intent = await self._classify_intent(text)
        logger.info(f"[{self.name}] Intent: {intent} — {text[:60]}")

        if intent == "PIPELINE":
            response = await self._propose_or_execute_pipeline(text)
            await self._record_external_exchange(text, response)
            return note_prefix + response

        if intent == "ACTUATE":
            response = await self._handle_actuate_intent(text)
            await self._record_external_exchange(text, response)
            return note_prefix + response

        if intent == "HA":
            result = await self.delegate_task("home-assistant-agent", text, timeout=120.0)
            if result and isinstance(result, dict) and result.get("result"):
                response = str(result["result"])
            elif not result:
                response = "I could not reach the Home Assistant agent right now. Please retry."
            else:
                response = "The Home Assistant agent did not return a result. Please retry."
            await self._record_external_exchange(text, response)
            return note_prefix + response

        # Refresh the system prompt with live registry + facts before any LLM call.
        # This ensures the LLM never answers from a stale view of which agents exist.
        self._rebuild_system_prompt()

        # Belt-and-braces: also inject the live agent list as a prefix on the
        # user message itself. Models trust in-message context over system-prompt
        # claims, so this is the strongest signal we can give without using a tool.
        # We send the prefixed text to the LLM but replace it with the clean
        # original in conversation history afterward — otherwise stale prefixes
        # would accumulate across turns and bloat the context window.
        prefixed_text = self._prefix_with_live_context(text)
        response = await self.chat(prefixed_text)
        # Find the most recent user message in history that matches the prefixed
        # text and replace it with the user's original. The assistant turn after
        # it remains unchanged.
        for i in range(len(self._conversation_history) - 1, -1, -1):
            m = self._conversation_history[i]
            if m.get("role") == "user" and m.get("content") == prefixed_text:
                m["content"] = text
                break
        self.persist("conversation_history", self._conversation_history)

        # If the LLM wrote agent code but forgot the <spawn> wrapper, remind it once
        has_spawn   = "<spawn>" in response
        has_code    = "async def handle_task" in response or "async def setup" in response
        asked_spawn = any(w in text.lower() for w in ("spawn", "create", "make", "build", "add", "agent"))
        if has_code and not has_spawn and asked_spawn:
            logger.info(f"[{self.name}] Code written without <spawn> — prompting to wrap it")
            response = await self.chat(
                "You wrote agent code but forgot to wrap it in a <spawn> block. "
                "Please output the complete spawn block now with that exact code inside it. "
                "Output ONLY the <spawn>...</spawn> block, nothing else."
            )

        clean, spawned = await self._process_spawn_commands(response)

        # Process any <delete>{"name": "..."}</delete> blocks the LLM produced.
        # This is the orchestrator-side counterpart of <spawn> — lets the LLM
        # remove agents in response to user requests like "delete the math agent".
        clean, deleted, missing = await self._process_delete_commands(clean)

        # Execute structured <delegate>{...}</delegate> blocks — the preferred,
        # unambiguous delegation form. Results are spliced in-place.
        clean, _delegate_results = await self._process_delegate_commands(clean)

        # Execute any looser @agent-name delegation patterns the LLM produced
        clean = await self._execute_llm_delegations(clean)

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "user_interaction", "input": text[:100], "response": clean[:200]},
        )

        # Build a system footer summarizing spawn/delete actions
        footer_parts = []
        if spawned:
            bg_names   = [a.name for a in spawned if isinstance(a, _SpawnPlaceholder)]
            live_names = [a.name for a in spawned if not isinstance(a, _SpawnPlaceholder)]
            if live_names:
                replaced = '"replace": true' in response or '"replace":true' in response
                action   = "Replaced" if replaced else "Spawned"
                footer_parts.append(f"{action} {', '.join(live_names)} — will auto-restore on restart")
            if bg_names:
                footer_parts.append(f"Installing packages for {', '.join(bg_names)} — will appear shortly")
        if deleted:
            footer_parts.append(f"Deleted {', '.join(deleted)}")
        if missing:
            footer_parts.append(
                f"Could not delete {', '.join(missing)} — not currently registered"
            )
        if footer_parts:
            clean += f"\n\n[System: {' | '.join(footer_parts)}]"

        return note_prefix + clean

    async def process_user_input_stream(self, text: str):
        """
        Streaming version of process_user_input().
        Yields text chunks as the LLM generates them, then a final dict:
          {"done": True, "spawned": [...names...], "system_msg": "..."}

        The CLI calls this and prints chunks immediately.
        REST/Discord/WhatsApp should use process_user_input() instead.
        """
        # Drain monitor notifications first
        note_prefix = self._drain_notifications()
        if note_prefix:
            yield note_prefix

        # ── Pending-plan response detection (same as non-streaming path) ─────
        if not text.strip().startswith("/") and not text.strip().startswith("@"):
            plan_response = await self._handle_pending_plan_response(text)
            if plan_response is not None:
                await self._record_external_exchange(text, plan_response)
                yield plan_response
                yield {"done": True, "spawned": [], "system_msg": ""}
                return

            # Collision guard — see process_user_input for rationale
            warn = self._warn_if_pending_plan_collision(text)
            if warn:
                await self._record_external_exchange(text, warn)
                yield warn
                yield {"done": True, "spawned": [], "system_msg": ""}
                return

        # All slash-commands and direct API intercepts are handled by process_user_input
        # Route them there to avoid duplicating all that logic here
        _stripped = text.strip().rstrip("()")
        _is_command = (
            _stripped.startswith("/")
            or _stripped in ("list_nodes", "main.list_nodes", "rules")
            or _stripped.startswith("@")
        )
        if _is_command:
            # /deploy is the one slash command that needs to stream progress
            # messages mid-execution (subnet scan, deploy phases). Other commands
            # go through process_user_input which is request/response.
            if _stripped.startswith("/deploy"):
                async for chunk in self._slash_deploy_stream(_stripped):
                    yield chunk
                yield {"done": True, "spawned": [], "system_msg": ""}
                return
            result = await self.process_user_input(text)
            yield result
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        # Explicit planner prefix always wins
        _lowered = text.lower()
        if any(_lowered.startswith(p) for p in (
            "coordinate:", "coordinate ", "plan:", "pipeline:", "pipeline ",
            "@planner", "set up a pipeline", "create a rule", "set up a rule",
        )):
            result = await self._run_planner(text)
            response = result or "Planner did not return a result. Please retry."
            await self._record_external_exchange(text, response)
            yield response
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        # Single LLM call classifies intent: ACTUATE, HA, PIPELINE, or OTHER
        intent = await self._classify_intent(text)
        logger.info(f"[{self.name}] Intent: {intent} — {text[:60]}")

        if intent == "PIPELINE":
            response = await self._propose_or_execute_pipeline(text)
            await self._record_external_exchange(text, response)
            yield response
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        if intent == "ACTUATE":
            response = await self._handle_actuate_intent(text)
            await self._record_external_exchange(text, response)
            yield response
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        if intent == "HA":
            result = await self.delegate_task("home-assistant-agent", text, timeout=120.0)
            if result and isinstance(result, dict) and result.get("result"):
                response = str(result["result"])
            elif not result:
                response = "I could not reach the Home Assistant agent right now. Please retry."
            else:
                response = "The Home Assistant agent did not return a result. Please retry."
            await self._record_external_exchange(text, response)
            yield response
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        # Refresh the system prompt with live registry + facts before any LLM call.
        # This ensures the LLM never answers from a stale view of which agents exist.
        self._rebuild_system_prompt()

        # Belt-and-braces: inject live agent list as a prefix on the user message.
        # Same as the non-streaming path — see _prefix_with_live_context for why.
        prefixed_text = self._prefix_with_live_context(text)

        # Stream the LLM response chunk by chunk
        full_chunks = []
        async for chunk in self.chat_stream(prefixed_text):
            if isinstance(chunk, dict):
                break   # usage dict — discard, already tracked inside chat_stream
            full_chunks.append(chunk)
            yield chunk

        # Replace the prefixed user message in history with the clean original
        # so future turns aren't polluted with stale prefixes.
        for i in range(len(self._conversation_history) - 1, -1, -1):
            m = self._conversation_history[i]
            if m.get("role") == "user" and m.get("content") == prefixed_text:
                m["content"] = text
                break
        self.persist("conversation_history", self._conversation_history)

        full_response = "".join(full_chunks)

        # Process any <spawn> blocks in the completed response
        _, spawned = await self._process_spawn_commands(full_response)

        # Process any <delete> blocks — orchestrator-side counterpart of <spawn>
        _, deleted, missing = await self._process_delete_commands(full_response)

        # Execute structured <delegate>{...}</delegate> blocks first (preferred
        # form). The raw block was already streamed to the user above, so append
        # each result as an additional chunk — same approach as @mention results.
        full_response, _delegate_results = await self._process_delegate_commands(full_response)
        for _r in _delegate_results:
            yield "\n" + _r

        # Execute any looser @agent-name delegation patterns the LLM produced.
        # If delegations ran, yield the results as an additional chunk.
        delegated = await self._execute_llm_delegations(full_response)
        if delegated != full_response:
            # Find what changed and yield just the new parts
            import re as _re
            results = _re.findall(r'[✅❌]\s+\S+.*', delegated)
            if results:
                yield "\n" + "\n".join(results)
        full_response = delegated

        system_msg_parts = []
        if spawned:
            names      = ", ".join(f"'{a.name}'" for a in spawned if not isinstance(a, _SpawnPlaceholder))
            bg_names   = [a.name for a in spawned if isinstance(a, _SpawnPlaceholder)]
            if names:
                replaced = '"replace": true' in full_response or '"replace":true' in full_response
                system_msg_parts.append(f"{'Replaced' if replaced else 'Spawned'} {names} — will auto-restore on restart")
            if bg_names:
                system_msg_parts.append(f"Installing packages for {', '.join(bg_names)} — will appear shortly")
        if deleted:
            system_msg_parts.append(f"Deleted {', '.join(deleted)}")
        if missing:
            system_msg_parts.append(f"Could not delete {', '.join(missing)} — not currently registered")
        system_msg = " | ".join(system_msg_parts)

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "user_interaction", "input": text[:100], "response": full_response[:200]},
        )

        yield {"done": True, "spawned": spawned, "system_msg": system_msg}

    # ── Planner ────────────────────────────────────────────────────────────

    _PLANNING_KEYWORDS = [
        # Coordination signals
        "and then", "after that", "also", "combine", "compare",
        "coordinate", "plan", "pipeline", "orchestrate", "summarize both",
        "using multiple", "all agents", "several agents",
        # Multi-step / multi-domain signals
        "first.*then", "step by step", "in order",
        "weather.*news", "news.*weather", "manual.*code", "search.*analyze",
        # Reactive pipeline signals
        "if.*then", "when.*send", "when.*turn", "when.*open", "when.*close",
        "whenever", "monitor.*and", "watch.*and", "detect.*and",
        "notify me", "alert me", "automatically",
    ]

    async def _needs_planning(self, text: str) -> bool:
        """
        Heuristic: does this task benefit from multi-agent coordination?
        Keeps main fast — only escalates genuinely complex requests.
        """
        import re
        lowered = text.lower()

        # Explicit user request for coordination
        if any(w in lowered for w in (
            "coordinate:", "plan:", "pipeline:", "@planner",
            "ask the planner", "use the planner", "create a pipeline",
            "set up a pipeline", "create a rule", "set up a rule",
        )):
            return True

        # Keyword heuristic — multiple signals needed to avoid false positives
        hits = sum(1 for kw in self._PLANNING_KEYWORDS if re.search(kw, lowered))
        if hits >= 2:
            return True

        # References two or more known agent names
        if self._registry:
            agent_names = [a.name for a in self._registry.all_actors()
                           if a.name not in {"main", "monitor", "installer"}]
            mentioned = sum(1 for name in agent_names if name in lowered)
            if mentioned >= 2:
                return True

        return False

    async def _run_planner(
        self,
        task: str,
        is_pipeline_intent: bool = False,
        plan_only: bool = False,
        approved_plan: Optional[dict] = None,
    ) -> Optional[str]:
        """Spawn a PlannerAgent, hand it the task, wait for the result.

        is_pipeline_intent: when True, the caller has classified this as a
        reactive-rule task ("if X then Y", "wherever Z happens..."). For these,
        we DELIBERATELY skip the conversation-history enrichment because:
          - Pipelines are imperative declarations, not follow-ups.
          - Including unrelated prior turns has been observed to bleed irrelevant
            context into the planner's LLM (e.g. a prior "door open" pipeline
            poisoning a fresh "camera person detection" pipeline, causing the
            planner to generate a door-themed agent name and code).
          - Pronoun resolution — the main reason enrichment exists — rarely
            applies to pipeline declarations.

        plan_only: when True, the planner builds a plan but does NOT spawn.
        Returns a JSON string containing the plan envelope (use _parse_plan_envelope
        to extract). Used by the dry-run flow.

        approved_plan: when provided, the planner skips planning entirely and
        executes the supplied plan directly. Used after the user approves a
        previously-generated plan.
        """
        from .planner_agent import PlannerAgent
        import uuid

        # Enrich vague follow-up tasks with recent conversation context
        # so the planner has the full picture (e.g. which entity was found).
        # PIPELINE intent skips this — see docstring.
        # approved_plan also skips: the plan was already built with the right context.
        enriched_task = task
        if (not is_pipeline_intent
                and not approved_plan
                and self._conversation_history
                and len(task.split()) < 15):
            # Short/vague task — inject last 3 exchanges as context
            recent = self._conversation_history[-6:]  # 3 user+assistant pairs
            ctx_lines = []
            for m in recent:
                role    = "User" if m["role"] == "user" else "Assistant"
                content = str(m["content"])[:300]
                ctx_lines.append(f"{role}: {content}")
            if ctx_lines:
                enriched_task = (
                    f"{task}\n\n"
                    f"[Context from recent conversation:]\n"
                    + "\n".join(ctx_lines)
                )

        planner_name = f"planner-{uuid.uuid4().hex[:6]}"
        mode = "approved-execute" if approved_plan else ("plan-only" if plan_only else "plan-and-execute")
        logger.info(f"[{self.name}] Spawning planner '{planner_name}' (mode={mode}) for: {enriched_task[:60]}")

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": f"Complex task detected — spawning planner ({mode})...", "timestamp": __import__('time').time()},
        )

        task_id = f"plan_{uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._result_futures[task_id] = future

        try:
            planner = await self.spawn(
                PlannerAgent,
                name=planner_name,
                llm_provider=self.llm,
                task=enriched_task,
                reply_to_id=self.actor_id,
                reply_task_id=task_id,
                auto_terminate=True,
                plan_only=plan_only,
                approved_plan=approved_plan,
                persistence_dir=str(self._persistence_dir.parent),
            )
            if not planner:
                return None

            result_payload = await asyncio.wait_for(future, timeout=180.0)
            answer = result_payload.get("result") or result_payload.get("text") or ""
            spawned_names = result_payload.get("spawned", [])
            if spawned_names:
                answer += f"\n\n[System: Planner created new agents: {', '.join(spawned_names)} — saved for future use]"
            return answer

        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Planner timed out for: {task[:60]}")
            return "The pipeline is taking longer than expected to set up. Check `/rules` in a moment to see if agents were spawned, or try again."
        except Exception as e:
            logger.error(f"[{self.name}] Planner error: {e}")
            return None
        finally:
            self._result_futures.pop(task_id, None)

    @staticmethod
    def _parse_plan_envelope(planner_result: str) -> Optional[dict]:
        """
        Try to parse a planner result string as a plan envelope (the JSON dict
        returned by plan_only mode). Returns the envelope dict if it's a valid
        proposal, or None if the result is a regular answer (e.g. error message,
        feasibility failure, or fallback prose).
        """
        if not planner_result or not planner_result.strip().startswith("{"):
            return None
        try:
            envelope = json.loads(planner_result)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(envelope, dict) and envelope.get("_plan_proposal") is True:
            return envelope
        return None

    def _dryrun_enabled(self, text: str) -> bool:
        """
        Decide whether dry-run / approval should gate this PIPELINE request.

        Bypass conditions (always skip approval):
          - Text uses the explicit bypass marker `pipeline!` or `coordinate!`
          - User policy `policy_dryrun` is set to "off" / "false" / "disabled"

        Otherwise dry-run is on by default for PIPELINE intent.
        """
        if not text:
            return True
        lowered = text.lower().lstrip()
        for bypass in ("pipeline!", "coordinate!", "@planner!"):
            if lowered.startswith(bypass):
                return False
        # Check user-set policy
        facts = self.get_user_facts()
        for key in ("policy_dryrun", "policy_dry_run", "policy_approval"):
            v = str(facts.get(key, "")).strip().lower()
            if v in ("off", "false", "disabled", "no", "skip"):
                return False
        return True

    @staticmethod
    def _strip_dryrun_bypass(text: str) -> str:
        """Strip the `pipeline!` / `coordinate!` bypass marker from the user's
        text so the planner doesn't see it as part of the task."""
        if not text:
            return text
        lowered = text.lower().lstrip()
        for bypass in ("pipeline!", "coordinate!", "@planner!"):
            if lowered.startswith(bypass):
                # Find the bypass in the original (case-insensitive) and skip it
                idx = text.lower().find(bypass)
                if idx != -1:
                    return text[idx + len(bypass):].lstrip(" :,-")
        return text

    async def _propose_or_execute_pipeline(self, text: str) -> str:
        """
        Top-level entry for PIPELINE intent. Decides between dry-run (build
        plan, ask for approval, store proposal) and immediate execution
        (bypass marker or policy-disabled). Returns the user-facing response.
        """
        if not self._dryrun_enabled(text):
            # Bypass — execute immediately as before
            cleaned = self._strip_dryrun_bypass(text)
            result = await self._run_planner(cleaned, is_pipeline_intent=True)
            return result or "Planner did not return a result. Please retry."

        # Dry-run path: get a plan, don't execute
        planner_result = await self._run_planner(text, is_pipeline_intent=True, plan_only=True)
        if not planner_result:
            return "Planner did not return a result. Please retry."

        envelope = self._parse_plan_envelope(planner_result)
        if not envelope:
            # Planner returned a regular answer (error, feasibility failure,
            # or fallback prose). Pass it through unchanged.
            return planner_result

        # Store the proposal
        import uuid as _uuid, time as _t
        plan_id = _uuid.uuid4().hex[:8]
        proposal = {
            "plan_id":    plan_id,
            "task":       text,
            "created_at": _t.time(),
            "status":     "pending",
            "envelope":   envelope,
        }
        self.save_pending_plan(proposal)
        return self._format_plan_proposal(proposal)

    # Approval/rejection vocabulary — exact-match-only after my v2 fix.
    # The previous version used `cleaned.startswith(w + " ")` which silently
    # treated any sentence beginning with "ok " as approval — including
    # corrections like "ok lets go for 55 as a threshold". The new logic
    # requires the message to be SHORT enough that it can only be approval
    # or rejection. See _looks_like_approval / _looks_like_rejection.
    _APPROVE_PHRASES = {
        "yes", "y", "yep", "yeah", "yup", "ya",
        "ok", "okay", "k", "kk",
        "sure", "fine", "alright",
        "go", "go ahead", "do it", "send it", "ship it",
        "approve", "approved", "approved!",
        "proceed", "confirm", "confirmed",
        "spawn it", "create it", "make it",
    }
    _APPROVE_EMPHASIS = {
        "please", "now", "go", "do it", "thanks", "thx", "ahead",
        "confirm", "confirmed", "approved", "ok", "yes", "good",
    }
    _REJECT_PHRASES = {
        "no", "n", "nope", "nah",
        "reject", "rejected",
        "cancel", "skip", "discard", "drop it",
        "abort", "stop", "stop it",
        "nevermind", "never mind", "forget it",
    }

    @classmethod
    def _looks_like_approval(cls, cleaned: str) -> bool:
        """Strict approval detection. Only fires when the message is short
        enough that it cannot also be a correction or a new request."""
        if cleaned in cls._APPROVE_PHRASES:
            return True
        # Allow up to a 3-token expansion where every extra token is itself
        # approval-flavored, so "yes please" / "ok do it" / "go ahead now" all
        # match — but "ok lets go for 55 as a threshold" does NOT.
        tokens = cleaned.split()
        if len(tokens) <= 3 and tokens and tokens[0] in cls._APPROVE_PHRASES:
            tail = " ".join(tokens[1:])
            if not tail:
                return True
            # Tail must be entirely emphasis/approval words (or a recognized
            # multi-word approval phrase joined back together).
            if tail in cls._APPROVE_PHRASES or tail in cls._APPROVE_EMPHASIS:
                return True
            if all(t in cls._APPROVE_EMPHASIS or t in cls._APPROVE_PHRASES for t in tokens[1:]):
                return True
        return False

    @classmethod
    def _looks_like_rejection(cls, cleaned: str) -> bool:
        """Strict rejection detection — same shape as approval."""
        if cleaned in cls._REJECT_PHRASES:
            return True
        tokens = cleaned.split()
        if len(tokens) <= 3 and tokens and tokens[0] in cls._REJECT_PHRASES:
            return True   # "no thanks", "cancel that", "stop please" — all clearly negative
        return False

    # Correction-intent signals: words that suggest the user is adjusting
    # the pending plan rather than confirming or starting fresh. Used only
    # when a plan is pending — outside that context these words are noise.
    _CORRECTION_HINTS = (
        "actually", "instead", "rather", "let's", "lets", "make it",
        "change", "change it", "use ", "set it to", "set the",
        "should be", "needs to be", "make that", "but ",
        " threshold", " interval", " every ", " seconds", " minutes",
        " hours", " minutes", " degrees", "%", "celsius", "fahrenheit",
        "increase", "decrease", "raise", "lower", "higher", "lower",
    )

    @classmethod
    def _looks_like_correction(cls, text: str) -> bool:
        """Heuristic: does this message look like an adjustment to a pending
        plan rather than a fresh request? Pure heuristic — false positives
        get a confirm-or-new prompt, false negatives fall through to OTHER
        intent (mildly annoying but not destructive)."""
        lowered = text.lower()
        # Numbers + units strongly suggest correction ("change to 55%", "every 30s")
        import re
        if re.search(r"\b\d+(\.\d+)?\s*(%|c|°|sec|secs|seconds|min|mins|minutes|hour|hours|hr|hrs)\b", lowered):
            return True
        if re.search(r"\b(threshold|interval|frequency|rate|delay)\b", lowered):
            return True
        return any(h in lowered for h in cls._CORRECTION_HINTS)

    async def _handle_pending_plan_response(self, text: str) -> Optional[str]:
        """
        If there is a pending plan and the user's message looks like a
        response to it (yes/no/correction), handle it and return the result.
        Returns None if there's no pending plan or the message clearly
        isn't a response — the message then flows through normal processing.

        Decision tree (in priority order):
          1. Strict approval match → execute the plan.
          2. Strict rejection match → reject and discard.
          3. Looks-like-correction (numbers/units, "let's", "instead", etc.)
             → revise the pending plan with this feedback.
          4. Anything else → return None, message processed normally
             (collision guard catches spawn-intent later).
        """
        pending = self._most_recent_pending_plan()
        if not pending:
            return None

        cleaned = text.strip().lower().rstrip(".,!?")
        if not cleaned:
            return None

        if self._looks_like_approval(cleaned):
            return await self._execute_pending_plan(pending)
        if self._looks_like_rejection(cleaned):
            return self._reject_pending_plan(pending)
        if self._looks_like_correction(text):
            return await self._revise_pending_plan(pending, text)

        # Not approval, rejection, or correction — leave the plan pending,
        # let the message flow through normal handling. The collision guard
        # downstream will catch spawn-intent messages and ask the user to
        # resolve the pending plan first.
        return None

    async def _revise_pending_plan(self, proposal: dict, correction: str) -> str:
        """
        The user typed something that looks like an adjustment to a pending
        plan ("let's use 55 instead", "change the interval to 30 seconds").
        Mark the old plan superseded, re-run the planner with the original
        task plus the correction as feedback, and present the new proposal.
        """
        old_id = proposal["plan_id"]
        original_task = proposal["task"]
        revised_task = (
            f"{original_task}\n\n"
            f"[User correction to the previous plan: {correction.strip()}]"
        )
        logger.info(f"[{self.name}] Revising plan {old_id} with correction: {correction[:80]!r}")
        self.update_plan_status(old_id, "superseded")

        # Re-run planner in plan_only mode with the enriched task
        planner_result = await self._run_planner(revised_task, is_pipeline_intent=True, plan_only=True)
        if not planner_result:
            return f"Could not revise plan `{old_id}`. The planner did not respond — please retry."

        envelope = self._parse_plan_envelope(planner_result)
        if not envelope:
            # Planner returned a regular answer — pass it through
            return planner_result

        import uuid as _uuid, time as _t
        new_id = _uuid.uuid4().hex[:8]
        new_proposal = {
            "plan_id":    new_id,
            "task":       original_task,   # keep original; correction lives in envelope
            "created_at": _t.time(),
            "status":     "pending",
            "envelope":   envelope,
            "supersedes": old_id,
        }
        self.save_pending_plan(new_proposal)
        formatted = self._format_plan_proposal(new_proposal)
        return (
            f"📝 Got it — revising plan `{old_id}` based on your feedback.\n"
            f"Plan `{old_id}` is now superseded by `{new_id}`:\n\n"
            f"{formatted}"
        )

    async def _execute_pending_plan(self, proposal: dict) -> str:
        """Execute an approved plan by calling the planner with approved_plan set."""
        plan_id = proposal["plan_id"]
        envelope = proposal["envelope"]
        original_task = proposal["task"]
        logger.info(f"[{self.name}] Executing approved plan {plan_id}")
        self.update_plan_status(plan_id, "approved")

        result = await self._run_planner(
            original_task,
            is_pipeline_intent=True,
            approved_plan=envelope,
        )
        return f"✅ Approved plan `{plan_id}`. {result or 'Spawn complete.'}"

    def _reject_pending_plan(self, proposal: dict) -> str:
        plan_id = proposal["plan_id"]
        self.update_plan_status(plan_id, "rejected")
        logger.info(f"[{self.name}] Rejected plan {plan_id}")
        return (
            f"❌ Discarded plan `{plan_id}`. No agents were spawned.\n"
            f"If you'd like to try again with different wording, just ask."
        )

    # Heuristic words that suggest the user wants to spawn / create / build
    # something. Cheap pre-LLM check used by the collision guard. False
    # positives are tolerable because the worst case is an unnecessary
    # warning (one extra message); false NEGATIVES are what we're avoiding
    # because they cause silent duplicate spawns.
    _SPAWN_INTENT_WORDS = (
        "spawn", "create", "build", "make a", "add a", "set up a", "set up an",
        "start a", "start an", "deploy", "launch", "i want a", "i need a",
        "generate", "produce", "watch for", "monitor", "alert me",
        "if ", "when ", "whenever ", "trigger ", "schedule",
        "every ", "each ",
    )

    def _warn_if_pending_plan_collision(self, text: str) -> Optional[str]:
        """
        If a plan is already pending and the user types something that looks
        like another spawn / pipeline request, return a warning message and
        do NOT process the request. This stops the silent-duplicate scenario:
          1. user types pipeline request → plan A pending
          2. user types ANOTHER pipeline request → would spawn agents X, Y
          3. user later approves plan A → spawns plan A's agents too
          4. user is now stuck with both, didn't intend either configuration

        The user must resolve the pending plan first (yes/no/cancel) or
        explicitly mark this new request to bypass.

        Returns None if there's no collision and processing should continue.
        """
        pending = self._most_recent_pending_plan()
        if not pending:
            return None
        # Bypass marker means "I know what I'm doing, just do it"
        if not self._dryrun_enabled(text):
            return None
        lowered = text.lower().strip()
        if not any(w in lowered for w in self._SPAWN_INTENT_WORDS):
            return None
        # Spawn-like request while a plan is pending — warn
        pid     = pending["plan_id"]
        ptask   = pending.get("task", "?")[:80]
        return (
            f"⚠️  You have a pending plan `{pid}` for: _{ptask}_\n\n"
            f"Your new message looks like another spawn or rule request, which would\n"
            f"create separate agents that may conflict with the pending plan once approved.\n\n"
            f"Please resolve the pending plan first:\n"
            f"  • Reply **yes** to approve and spawn it, then send your new request.\n"
            f"  • Reply **no** to discard it, then send your new request.\n"
            f"  • Or send `/plans show {pid}` to inspect, then `/plans approve {pid}` "
            f"or `/plans reject {pid}`.\n\n"
            f"To bypass this check for one-off requests, prefix with `pipeline!` "
            f"(e.g. `pipeline! {text[:40]}...`)."
        )

        # ── Spawn ──────────────────────────────────────────────────────────────

    async def _resolve_or_spawn(self, agent_name: str):
        """
        Resolve an agent by name, auto-spawning it from a catalog recipe if it
        isn't running yet. Shared by @mention delegations and <delegate> blocks.

        Returns (target_actor_or_None, spawnable_bool).
        """
        target = self._registry.find_by_name(agent_name) if self._registry else None
        spawnable = False
        if not target:
            manifest = self._agent_manifests.get(agent_name, {})
            spawnable = bool(manifest.get("spawnable") and manifest.get("catalog"))
            if spawnable:
                catalog_actor = self._registry.find_by_name(manifest["catalog"]) if self._registry else None
                if catalog_actor and hasattr(catalog_actor, "_action_spawn"):
                    logger.info(f"[{self.name}] Auto-spawning '{agent_name}' via catalog...")
                    try:
                        spawn_result = await catalog_actor._action_spawn(agent_name, {})
                        if spawn_result and spawn_result.get("ok"):
                            await asyncio.sleep(0.5)
                            target = self._registry.find_by_name(agent_name) if self._registry else None
                            logger.info(f"[{self.name}] '{agent_name}' spawned successfully")
                        else:
                            err = spawn_result.get("message", "unknown") if spawn_result else "no response"
                            logger.warning(f"[{self.name}] Spawn failed for '{agent_name}': {err}")
                    except Exception as e:
                        logger.error(f"[{self.name}] Spawn error for '{agent_name}': {e}")
        return target, spawnable

    async def _run_delegation(self, agent_name: str, payload) -> str:
        """
        Dispatch one already-resolved delegation and format the result string.
        Shared by @mention delegations and <delegate> blocks.
        """
        json_str = json.dumps(payload)
        logger.info(f"[{self.name}] Executing LLM delegation → @{agent_name} {json_str[:80]}")
        try:
            result = await self.delegate_task(agent_name, json_str, timeout=300.0)
            if result:
                if isinstance(result, dict):
                    error = result.get("error")
                    if error:
                        return f"❌ {agent_name} failed: {error}"
                    for key in ("pptx_path", "image_path", "result", "message", "output", "text"):
                        if result.get(key):
                            return f"✅ {agent_name} completed: {key}={result[key]}"
                    return f"✅ {agent_name} completed: {result}"
                return f"✅ {agent_name}: {result}"
            return f"[{agent_name} did not respond]"
        except Exception as e:
            return f"[{agent_name} error: {e}]"

    async def _process_delegate_commands(self, response: str):
        """
        Scan the LLM response for structured delegation blocks and execute them:

            <delegate>{"agent": "manual-agent", "task": "search for the Philips 2200 manual"}</delegate>
            <delegate>{"agent": "weather-agent", "payload": {"city": "Athens"}}</delegate>

        This is the orchestrator-side counterpart of <spawn>/<delete> and the
        PREFERRED way for the LLM to delegate: unlike a free-text @mention it is
        unambiguous, never truncated, and carries a structured payload.

        Accepted block fields:
          - "agent"  (required) — target agent name
          - "task"   — free-text task; wrapped as {"text": task}
          - "payload"— explicit dict payload (takes precedence over "task")

        Returns (cleaned_response, [result_strings]). The <delegate> block is
        replaced in-place with its result so the user sees the outcome.
        """
        pattern = r'<delegate>(.*?)</delegate>'
        results: list[str] = []

        matches = list(re.finditer(pattern, response, re.DOTALL))
        for m in matches:
            block = m.group(1).strip()
            try:
                cfg = json.loads(block)
            except json.JSONDecodeError as e:
                logger.error(f"[{self.name}] Invalid <delegate> JSON: {e}\nRaw block: {block[:200]}")
                response = response.replace(m.group(0), "[delegate: malformed block]")
                continue

            agent_name = (cfg.get("agent") or cfg.get("name") or "").strip()
            if not agent_name:
                logger.warning(f"[{self.name}] <delegate> block missing 'agent': {block[:200]}")
                response = response.replace(m.group(0), "[delegate: no agent named]")
                continue
            if agent_name == self.name:
                response = response.replace(m.group(0), "")
                continue

            if isinstance(cfg.get("payload"), dict):
                payload = cfg["payload"]
            else:
                payload = {"text": str(cfg.get("task", "")).strip()}

            target, spawnable = await self._resolve_or_spawn(agent_name)
            if not target:
                result_str = f"[Could not reach {agent_name}]"
            else:
                result_str = await self._run_delegation(agent_name, payload)

            results.append(result_str)
            response = response.replace(m.group(0), result_str)

        return response, results

    async def _execute_llm_delegations(self, response: str) -> str:
        """
        Scan the LLM response for @agent-name delegation patterns and execute them.
        Replaces the matched pattern in the response with the actual result.

        Prefer <delegate>{...}</delegate> blocks (see _process_delegate_commands).
        This handles the looser @mention forms the LLM still produces:

          1. Explicit JSON payload, anywhere in the text (original behavior — the
             form that already worked):
                 @weather-agent {"city": "Athens"}

          2. Bare conversational mention at the start of a line OR a sentence:
                 ...sending that now.  @manual-agent search for the Philips 2200 manual.
             The text from the mention up to the next sentence terminator / newline
             is wrapped as {"text": "..."}.

        Previously only form (1) was recognized, so a bare mention matched nothing,
        was streamed to the user verbatim, and was never dispatched — which is why
        nothing showed up in the logs.
        """
        # (full_match_str, agent_name, payload_dict, is_bare)
        delegations = []
        json_spans  = []   # char spans consumed by form (1), so form (2) skips them

        # ── Form 1: @agent-name {json} — ANYWHERE. Brace-scan the matching close
        #    over the full response (payload may span lines / contain } in strings).
        for m in re.finditer(r'@([\w][\w\-]*)\s+(\{)', response):
            agent_name = m.group(1)
            if agent_name == self.name:
                continue
            start = m.start(2)
            depth = 0
            end   = start
            for i, ch in enumerate(response[start:], start):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if depth != 0:
                continue   # unmatched braces — skip
            try:
                payload = json.loads(response[start:end])
            except json.JSONDecodeError:
                continue
            delegations.append((response[m.start():end], agent_name, payload, False))
            json_spans.append((m.start(), end))

        # ── Form 2: bare mention at a line OR sentence boundary. The '@' must be
        #    preceded by start-of-text, a newline, or sentence-ending punctuation,
        #    so an @name buried mid-sentence ("you can use @x to ...") — which the
        #    prompt treats as the WRONG, user-instructing pattern — is ignored.
        trigger = re.compile(r'(?:^|\n|[.!?:]\s+)(@([\w][\w\-]*)[ \t]+)')
        for m in trigger.finditer(response):
            agent_name = m.group(2)
            if agent_name == self.name:
                continue
            at_start      = m.start(1)   # index of the '@'
            payload_start = m.end(1)     # first char after "@name "
            if any(s <= at_start < e for s, e in json_spans):
                continue   # already captured as the JSON form
            if payload_start < len(response) and response[payload_start] == '{':
                continue   # JSON form — owned by form (1)
            tail = response[payload_start:]
            stop = re.search(r'[.!?\n]', tail)
            cut  = stop.start() if stop else len(tail)
            task = tail[:cut].strip()
            if not task:
                continue
            end = payload_start + cut
            delegations.append((response[at_start:end], agent_name, {"text": task}, True))

        replacements = []
        for full_match, agent_name, payload, is_bare in delegations:
            target, spawnable = await self._resolve_or_spawn(agent_name)
            if not target:
                # A bare prose mention of an unknown, non-spawnable name is almost
                # certainly ordinary text (or an agent the LLM only imagined), so
                # leave the line untouched rather than clobbering the reply. The
                # explicit JSON form is unambiguous machine intent — surface it.
                if is_bare and not spawnable:
                    logger.debug(f"[{self.name}] Ignoring bare mention of unknown agent '{agent_name}'")
                    continue
                replacements.append((full_match, f"[Could not reach {agent_name}]"))
                continue
            result_str = await self._run_delegation(agent_name, payload)
            replacements.append((full_match, result_str))

        for original, replacement in replacements:
            response = response.replace(original, replacement)

        return response

    @staticmethod
    def _parse_spawn_config(raw: str) -> dict:
        """
        Robustly parse a spawn config that may contain raw multiline code strings.
        Uses character scanning to correctly handle } and " inside the code value.
        """
        raw = raw.strip()

        # Strategy 1: standard JSON (works when LLM properly escapes newlines)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Strategy 2: backtick-delimited code (rare but some LLMs use it)
        bt_match = re.search(r'"code"\s*:\s*`(.*?)`', raw, re.DOTALL)
        if bt_match:
            code_raw    = bt_match.group(1)
            placeholder = re.sub(r'"code"\s*:\s*`.*?`', '"code": "__CODE__"', raw, flags=re.DOTALL)
            config      = json.loads(placeholder)
            config["code"] = code_raw
            return config

        # Strategy 3: character scanner — find opening " after "code":
        # then scan forward respecting escape sequences to find the real closing "
        # This correctly handles } and { inside the code value.
        key_match = re.search(r'"code"\s*:\s*"', raw)
        if not key_match:
            raise ValueError(f"No 'code' key found in spawn config:\n{raw[:200]}")

        code_start = key_match.end()   # index right after the opening "
        i = code_start
        while i < len(raw):
            if raw[i] == '\\':
                i += 2             # skip escaped character
                continue
            if raw[i] == '"':
                break              # found unescaped closing quote
            i += 1

        code_raw    = raw[code_start:i]
        placeholder = raw[:key_match.start()] + '"code": "__CODE__"' + raw[i+1:]

        try:
            config = json.loads(placeholder)
        except json.JSONDecodeError as e:
            raise ValueError(f"Spawn config JSON invalid after code extraction: {e}\nPlaceholder:\n{placeholder[:300]}")

        # Unescape sequences the LLM may have added
        config["code"] = (code_raw
                          .replace("\\n", "\n")
                          .replace('\\"', '"')
                          .replace("\\t", "\t"))
        return config

    async def _process_spawn_commands(self, response: str):
        spawned = []
        pattern = r'<spawn>(.*?)</spawn>'

        for match in re.findall(pattern, response, re.DOTALL):
            try:
                config = self._parse_spawn_config(match.strip())
                # LLM agents have no "code" — only check for code if type is dynamic
                agent_type = config.get("type", "dynamic")
                has_code   = bool(config.get("code", "").strip())
                has_prompt = bool(config.get("system_prompt", "").strip())
                if agent_type == "dynamic" and not has_code:
                    logger.error(f"[{self.name}] Dynamic agent has no code: {config.get('name')}")
                    continue
                if agent_type == "llm" and not has_prompt:
                    logger.warning(f"[{self.name}] LLM agent has no system_prompt, using default: {config.get('name')}")
                actor = await self._spawn_from_config(config, save=True)
                if actor:
                    spawned.append(actor)
            except Exception as e:
                logger.error(f"[{self.name}] Spawn failed: {e}\nRaw block:\n{match[:500]}")

        clean = re.sub(pattern, '', response, flags=re.DOTALL).strip()
        return clean, spawned

    async def _process_delete_commands(self, response: str):
        """
        Scan the LLM response for <delete>{"name": "agent-name"}</delete> blocks
        and execute them. Mirrors _process_spawn_commands so deletion has the same
        UX as spawn: the LLM emits a tagged block, we parse and execute, and the
        block is stripped from the user-visible response.

        Returns (cleaned_response, [deleted_names], [missing_names]):
          - cleaned_response: response with <delete> blocks removed
          - deleted_names:    names that were actually running and got removed
          - missing_names:    names the LLM asked to delete that didn't exist

        We track the missing list separately so the response footer can tell the
        user "you asked me to delete X but it wasn't running" instead of silently
        dropping the request.
        """
        pattern = r'<delete>(.*?)</delete>'
        deleted: list[str] = []
        missing: list[str] = []

        # Build the set of currently-known agent names ONCE up front, so a delete
        # block that lists a name we then delete doesn't accidentally appear as
        # "missing" if a later block references the same name.
        known_names = set(self._agent_manifests.keys())
        if self._registry:
            known_names |= {a.name for a in self._registry.all_actors()}
        # Spawn registry is the strongest signal — if it's persisted there, deletion
        # is meaningful even if the live actor isn't currently up.
        known_names |= set(self._get_spawn_registry().keys())

        # Names main itself never deletes (housekeeping/system actors).
        protected = {"main", "monitor", "installer", "home-assistant-agent",
                     "anomaly-detector", "code-agent", "catalog"}

        for match in re.findall(pattern, response, re.DOTALL):
            block = match.strip()
            try:
                # Accept either a JSON object {"name": "x"} or a bare string "x"
                # so the LLM has a forgiving format.
                name: Optional[str] = None
                stripped = block.strip()
                if stripped.startswith("{"):
                    payload = json.loads(stripped)
                    if isinstance(payload, dict):
                        name = payload.get("name") or payload.get("agent")
                else:
                    # Bare token form: <delete>math-agent</delete>
                    name = stripped.strip("\"'").split()[0] if stripped else None
                if not name or not isinstance(name, str):
                    logger.warning(f"[{self.name}] Empty or malformed <delete> block: {block[:200]}")
                    continue
                name = name.strip()

                if name in protected:
                    logger.warning(f"[{self.name}] Refused to delete protected agent '{name}'")
                    continue

                if name not in known_names:
                    logger.info(f"[{self.name}] LLM requested deletion of unknown agent '{name}'")
                    missing.append(name)
                    continue

                logger.info(f"[{self.name}] LLM-requested deletion of '{name}'")
                # Reuse the existing helper — it handles spawn registry, stop,
                # manifest cleanup, history note, and remote-vs-local routing.
                await self.delete_spawned_agent(name)
                deleted.append(name)
            except json.JSONDecodeError as e:
                logger.error(f"[{self.name}] Invalid <delete> JSON: {e}\nRaw block: {block[:200]}")
            except Exception as e:
                logger.error(f"[{self.name}] Delete failed: {e}\nRaw block:\n{block[:500]}")

        clean = re.sub(pattern, '', response, flags=re.DOTALL).strip()
        return clean, deleted, missing


    async def _spawn_from_config(self, config: dict, save: bool = True) -> Optional[Actor]:
        name = config.get("name", "dynamic-agent")
        node = config.get("node", "").strip()

        # Remote spawn — publish to the node's spawn topic via MQTT
        if node:
            return await self._spawn_remote(config, node, save)

        # Local spawn
        from .dynamic_agent import DynamicAgent

        existing = self._registry.find_by_name(name) if self._registry else None
        replace  = config.get("replace", False)

        # Also consult the Supervisor — its factory may have already brought up
        # an instance whose registration hasn't completed yet (race window during
        # parallel startup). find_by_name() would miss it.
        if existing is None and self._registry is not None:
            sup = getattr(self._registry, "_supervisor_ref", None)
            if sup is not None:
                spec = sup._specs.get(name)
                if spec is not None and spec.actor is not None and not spec.retired:
                    existing = spec.actor

        if existing:
            if not replace:
                logger.info(f"[{self.name}] '{name}' already exists (use replace=true to update).")
                return existing
            # Stop the old agent cleanly before spawning the replacement
            logger.info(f"[{self.name}] Replacing '{name}' with updated code...")
            try:
                if self._registry:
                    await self._registry.unregister(existing.actor_id)
                await existing.stop()
                # Clear cached manifest in-memory so a list query during the brief
                # window before the new agent publishes doesn't show stale data.
                # We do NOT tombstone the MQTT topic — the new agent will reuse
                # the same deterministic actor_id and republish immediately.
                self._agent_manifests.pop(name, None)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"[{self.name}] Error stopping old '{name}': {e}")

        agent_type    = config.get("type", "dynamic")
        code          = config.get("code", "").strip()
        system_prompt = config.get("system_prompt", "").strip()

        # Route to the right agent class
        if agent_type == "ha_actuator":
            actor = await self._spawn_ha_actuator(config, name)
        elif agent_type == "scheduled":
            actor = await self._spawn_scheduled_agent(config, name)
        elif agent_type == "llm" or (not code and system_prompt):
            actor = await self._spawn_llm_agent(config, name)
        elif code:
            actor = await self._spawn_dynamic_agent(config, name, code)
        else:
            logger.warning(f"[{self.name}] Spawn config for '{name}' has neither code nor system_prompt.")
            return None

        if actor and save:
            self._save_to_spawn_registry(config)

        return actor

    async def _spawn_ha_actuator(self, config: dict, name: str):
        """Spawn a HomeAssistantActuatorAgent from a spawn block with type: ha_actuator."""
        from .home_assistant_actuator_agent import (
            HomeAssistantActuatorAgent, ActuatorConfig, ActuatorAction, ActuatorCondition,
        )
        import hashlib as _hl

        # Ensure unique name if collision
        if self._registry and self._registry.find_by_name(name):
            suffix = _hl.md5(f"{name}{__import__('time').time()}".encode()).hexdigest()[:4]
            name   = f"{name}-{suffix}"

        automation_id = config.get("automation_id", name)
        actuator_cfg  = ActuatorConfig(
            automation_id    = automation_id,
            description      = config.get("description", ""),
            mqtt_topics      = config.get("mqtt_topics", []),
            actions          = [ActuatorAction.from_dict(a) for a in config.get("actions", [])],
            conditions       = [ActuatorCondition.from_dict(c) for c in config.get("conditions", [])],
            detection_filter = config.get("detection_filter"),
            cooldown_seconds = float(config.get("cooldown_seconds", 10.0)),
        )
        logger.info(f"[{self.name}] Spawning HomeAssistantActuatorAgent '{name}'")
        actor = await self.spawn(
            HomeAssistantActuatorAgent,
            config          = actuator_cfg,
            name            = name,
            persistence_dir = str(self._persistence_dir.parent),
        )
        return actor

    async def _spawn_scheduled_agent(self, config: dict, name: str):
        """
        Spawn a ScheduledAgent. The schedule spec is validated at __init__
        so a malformed config raises here, before save_to_spawn_registry runs.

        We inject the user's preferred timezone (from facts) so a "5pm" schedule
        means 5pm where the user lives, not 5pm UTC. The spec's own 'tz' field
        wins if explicitly set.
        """
        from .scheduled_agent import ScheduledAgent

        schedule_spec = config.get("schedule")
        if not isinstance(schedule_spec, dict):
            logger.warning(f"[{self.name}] Cannot spawn '{name}': missing or invalid 'schedule' dict")
            return None

        # Resolve user's timezone from facts (set by fact extraction)
        user_tz = self.get_user_facts().get("pref_timezone")

        publish_topic = config.get("publish_topic") or f"schedule/{name}/fired"
        description   = config.get("description", "")

        try:
            actor = await self.spawn(
                ScheduledAgent,
                name            = name,
                schedule        = schedule_spec,
                timezone        = user_tz,
                publish_topic   = publish_topic,
                description     = description,
                persistence_dir = str(self._persistence_dir.parent),
            )
            logger.info(
                f"[{self.name}] Spawned ScheduledAgent '{name}' "
                f"({schedule_spec.get('type')} → {publish_topic})"
            )
            return actor
        except ValueError as e:
            # Invalid schedule spec — surfaced from ScheduledAgent.__init__
            logger.error(f"[{self.name}] Invalid schedule for '{name}': {e}")
            return None
        except Exception as e:
            logger.error(f"[{self.name}] Failed to spawn ScheduledAgent '{name}': {e}")
            return None


    async def _apply_initial_state(self, name: str, config: dict) -> None:
        """
        Load a migrated state snapshot into the per-agent persistence layer
        BEFORE the agent is spawned.

        ``config["_initial_state"]`` is the dict shipped over MQTT from the
        source node (or assembled by main when migrating local → remote and
        then back). We pop it from the config (so it isn't saved into the
        spawn registry) and write it through PersistenceAPI.load_snapshot()
        so that when the new actor runs its on_start() and calls recall(),
        the migrated conversation_history / counters / etc. are already there.

        Without this hop, _initial_state arrives in the config dict but is
        completely ignored — the local agent starts with an empty memory
        regardless of what was shipped from the source node.

        Called for BOTH local LLMAgent and local DynamicAgent spawns. No-op
        when there's no snapshot or when the unified PersistenceAPI isn't
        wired up (legacy pickle-only deployments).
        """
        snapshot = config.pop("_initial_state", None)
        if not snapshot or not isinstance(snapshot, dict):
            return

        try:
            from ..core.persistence import (
                PersistenceAPI, get_db, get_redis, get_pickle_store,
            )
        except Exception as e:
            logger.debug(
                f"[{self.name}] PersistenceAPI not importable — falling back to "
                f"legacy state injection for '{name}': {e}"
            )
            # Best-effort legacy fallback: write a state.pkl next to where
            # Actor.__init__ will look for it. This covers the pickle-only
            # codepath that still exists in actor.py.
            try:
                from pathlib import Path
                import pickle as _pickle
                safe = name.replace("/", "_").replace("\\", "_")
                pdir = Path(str(self._persistence_dir.parent)) / safe
                pdir.mkdir(parents=True, exist_ok=True)
                with open(pdir / "state.pkl", "wb") as fh:
                    _pickle.dump(snapshot, fh)
                logger.info(
                    f"[{self.name}] Wrote {len(snapshot)} key(s) of migrated "
                    f"state to {pdir / 'state.pkl'} for '{name}' (legacy path)"
                )
            except Exception as e2:
                logger.warning(
                    f"[{self.name}] Legacy state injection failed for '{name}': {e2}"
                )
            return

        db    = get_db()
        redis = get_redis()
        pkl   = get_pickle_store()
        if not (db and redis and pkl):
            logger.warning(
                f"[{self.name}] PersistenceAPI stores not initialised — "
                f"cannot apply migrated state for '{name}'"
            )
            return

        api = PersistenceAPI(db, redis, pkl, name)
        applied = api.load_snapshot(snapshot, replace=True)
        logger.info(
            f"[{self.name}] Applied migrated state for '{name}': "
            f"{applied['sqlite']} SQLite, {applied['redis']} Redis, "
            f"{applied['pickle']} pickle key(s) (from {len(snapshot)} shipped)"
        )

    async def _spawn_llm_agent(self, config: dict, name: str):
        """Spawn a proper LLMAgent — best for chat, Q&A, reasoning tasks."""
        # Apply any migrated state BEFORE spawn so on_start()'s recall() finds
        # conversation_history / history_summary already in the DB. Without
        # this the chat-agent's memory wouldn't survive a migrate-back.
        await self._apply_initial_state(name, config)

        from .llm_agent import LLMAgent
        system_prompt = config.get("system_prompt", "You are a helpful assistant.")
        logger.info(f"[{self.name}] Spawning LLM agent '{name}'")
        actor = await self.spawn(
            LLMAgent,
            name=name,
            llm_provider=self.llm,
            system_prompt=system_prompt,
            persistence_dir=str(self._persistence_dir.parent),
        )
        return actor

    async def _spawn_dynamic_agent(self, config: dict, name: str, code: str):
        """Spawn a DynamicAgent — best for data pipelines, sensors, tools."""
        packages = config.get("install", [])
        if isinstance(packages, str):
            packages = [p.strip() for p in packages.replace(",", " ").split()]

        if packages:
            # Fast-path: check which packages actually need installing.
            # On restore (after restart), packages from the previous session
            # are already installed — no need to wait for the installer agent
            # which might not have started yet.
            import importlib
            needed = []
            for pkg in packages:
                import_name = pkg.replace("-", "_").split("[")[0]
                try:
                    importlib.import_module(import_name)
                except ImportError:
                    needed.append(pkg)

            if needed:
                # Some packages missing — install in background
                logger.info(f"[{self.name}] Scheduling background install+spawn for '{name}': {needed}")
                asyncio.create_task(self._install_then_spawn(config, name, code, needed))
                return _SpawnPlaceholder(name)
            else:
                # All packages already available — spawn immediately
                logger.info(f"[{self.name}] All deps for '{name}' already installed — spawning directly")
                return await self._do_spawn_dynamic(config, name, code)
        else:
            return await self._do_spawn_dynamic(config, name, code)

    async def _install_then_spawn(self, config: dict, name: str, code: str, packages: list):
        """Background task: install packages then spawn the agent."""
        try:
            await self._mqtt_publish(
                f"agents/{self.actor_id}/logs",
                {"type": "log", "message": f"Installing {packages} for {name}...", "timestamp": __import__("time").time()},
            )
            await self._install_packages(packages)
            actor = await self._do_spawn_dynamic(config, name, code)
            if actor:
                self._save_to_spawn_registry(config)
                await self._mqtt_publish(
                    f"agents/{self.actor_id}/logs",
                    {"type": "spawned", "message": f"'{name}' spawned after install", "child_name": name, "timestamp": __import__("time").time()},
                )
                logger.info(f"[{self.name}] Background spawn complete: {name}")
        except Exception as e:
            logger.error(f"[{self.name}] Background install+spawn failed for '{name}': {e}")

    async def _do_spawn_dynamic(self, config: dict, name: str, code: str):
        """Actually create and start the DynamicAgent."""
        # Apply any migrated state BEFORE spawn so on_start() and the agent's
        # generated setup() see the right values for counters, calibration,
        # chat history etc. that were shipped from the source node.
        await self._apply_initial_state(name, config)

        from .dynamic_agent import DynamicAgent
        actor = await self.spawn(
            DynamicAgent,
            name=name,
            code=code,
            poll_interval=float(config.get("poll_interval", 1.0)),
            description=config.get("description", ""),
            input_schema=config.get("input_schema", {}),
            output_schema=config.get("output_schema", {}),
            llm_provider=self.llm,
            persistence_dir=str(self._persistence_dir.parent),
            trusted=bool(config.get("trusted", False)),
        )
        
        # Register TopicContract if spawn config declares pub/sub topics
        if actor and (config.get("publishes") or config.get("subscribes")):
            try:
                from ..core.topic_bus import TopicContract, get_topic_bus
                contract = TopicContract.from_spawn_config({**config, "actor_id": actor.actor_id})
                bus = get_topic_bus()
                if bus:
                    bus.register_contract(contract)
                    logger.info(f"[{self.name}] Registered TopicContract for '{name}': "
                                f"pub={contract.publishes} sub={contract.subscribes}")
            except Exception as e:
                logger.debug(f"[{self.name}] TopicContract registration skipped: {e}")
                
        return actor

    async def _install_packages(self, packages: list[str]):
        """Delegate package installation to the installer agent."""
        if not self._registry:
            return

        # Fast path: check which packages actually need installing
        import importlib, sys
        needed = []
        for pkg in packages:
            import_name = pkg.replace("-", "_").split("[")[0]
            try:
                importlib.import_module(import_name)
            except ImportError:
                needed.append(pkg)
        if not needed:
            logger.info(f"[{self.name}] All packages already available: {packages} — skipping install")
            return

        installer = self._registry.find_by_name("installer")
        if not installer:
            logger.warning(f"[{self.name}] installer agent not found — skipping install of {needed}")
            return
        logger.info(f"[{self.name}] Installing packages via installer: {needed}")
        import uuid
        task_id = f"install_{uuid.uuid4().hex[:8]}"
        future = asyncio.get_event_loop().create_future()
        self._result_futures[task_id] = future
        await self.send(installer.actor_id, MessageType.TASK, {
            "action": "install",
            "packages": needed,
            "task": task_id,
            "_task_id": task_id,
            "reply_to": self.actor_id,
        })
        try:
            result = await asyncio.wait_for(future, timeout=120.0)
            logger.info(f"[{self.name}] Install result: {result.get('message', result)}")
            if result.get("failed"):
                logger.warning(f"[{self.name}] Failed to install: {result['failed']}")
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Package install timed out for {needed}")
        finally:
            self._result_futures.pop(task_id, None)

    async def run_pipeline(self, goal: str, agents: list[str], timeout: float = 300.0, force_replan: bool = False) -> dict:
        """
        Spawn an ephemeral TaskManager to coordinate a multi-agent pipeline.
        Returns the final synthesised result without blocking main's context.

        Usage:
            result = await main.run_pipeline(
                goal="Find the Philips EP2220 manual and answer: how do I descale it?",
                agents=["manual-agent", "installer"]
            )
        """
        from .task_manager import TaskManager
        import uuid

        task_id = uuid.uuid4().hex[:8]
        future  = asyncio.get_event_loop().create_future()
        self._result_futures[task_id] = future

        mgr = await self.spawn(
            TaskManager,
            goal=goal,
            available_agents=agents,
            llm_provider=self.llm,
            reply_to_id=self.actor_id,
            reply_task_id=task_id,
            auto_destroy=True,
            force_replan=force_replan,
            cache_dir=str(self._persistence_dir.parent / "plan_cache"),
            persistence_dir=str(self._persistence_dir.parent),
        )

        logger.info(f"[{self.name}] Pipeline started: {mgr.name} for goal: {goal[:60]}")

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Pipeline timed out after {timeout}s")
            return {"error": f"Pipeline timed out after {timeout}s"}
        finally:
            self._result_futures.pop(task_id, None)

    # Synthetic bridge code shipped with type:"llm" agents when they're spawned
    # on a remote node. The runner compiles this and finds handle_task, so the
    # agent can actually respond to tasks instead of returning the "no
    # handle_task function" error.
    #
    # Design points:
    #   - All persistence is via agent.persist / agent.recall (these write to
    #     the remote node's JSON state file under conversation_history).
    #     conversation_history shipped via _initial_state from main is picked
    #     up by recall() on first use, so memory survives migrate-back too.
    #   - LLM calls go through agent.chat(messages, system=...) — on the remote
    #     side this is _RemoteAgentAPI.chat, which RPCs back to main via the
    #     main/llm_request bridge. No API key leaves main.
    #   - System prompt is interpolated as a string literal at synthesis time
    #     so the agent code doesn't need to look it up from config at runtime.
    #     We use json.dumps to safely escape quotes / newlines / backslashes.
    #   - History gets bounded at max_history turns (32 by default, matches
    #     LLMAgent's default) so the prompt doesn't grow without bound.
    _LLM_BRIDGE_CODE_TEMPLATE = '''
# ── Auto-generated LLM bridge (synthesized by main when this agent was spawned
# remotely with type: "llm"). Do not edit; replace the agent if you need to
# change behavior. ─────────────────────────────────────────────────────────
import time as _t

_SYSTEM_PROMPT = {system_prompt_literal}
_MAX_HISTORY   = {max_history}

async def setup(agent):
    # Conversation history may have been shipped via _initial_state at spawn
    # time — agent.recall() finds it. If this is a fresh spawn, default to
    # an empty list.
    agent.state["history"] = list(agent.recall("conversation_history", []) or [])

async def handle_task(agent, payload):
    # Accept the same shapes LLMAgent does: dict with text/task/message/query,
    # or a bare string. Anything else gets str()'d so the LLM at least gets
    # something to look at instead of crashing on a non-string.
    if isinstance(payload, dict):
        text = (
            payload.get("text")
            or payload.get("task")
            or payload.get("message")
            or payload.get("query")
            or str(payload)
        )
    else:
        text = str(payload) if payload is not None else ""

    started = _t.time()
    history = agent.state.setdefault("history", [])
    history.append({{"role": "user", "content": text, "ts": started}})

    # Keep the prompt bounded — only send the last _MAX_HISTORY turns to the LLM.
    safe_history = [
        {{"role": m["role"], "content": str(m["content"])}}
        for m in history[-_MAX_HISTORY:]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ]

    response = await agent.chat(safe_history, system=_SYSTEM_PROMPT)
    duration = _t.time() - started

    history.append({{"role": "assistant", "content": response, "ts": _t.time()}})
    # Trim in-memory list too so it doesn't grow forever between persists.
    if len(history) > _MAX_HISTORY * 2:
        del history[: len(history) - _MAX_HISTORY * 2]
    agent.persist("conversation_history", history)

    return {{"text": response, "task": text[:60], "duration": duration}}
'''

    def _inject_llm_bridge_code(self, config: dict) -> dict:
        """
        If ``config`` is for an LLM-typed agent without code, return a copy
        with synthesized bridge code so the remote runner can actually run it.

        No-op (returns the original config) when:
          - the config already has code (caller provided their own logic)
          - the config isn't type "llm" (DynamicAgent, ha_actuator, etc. are
            handled directly by the runner already)

        Why a copy: callers may pass the same config dict for multiple writes
        (spawn registry, desired_state, MQTT publish) and we don't want to
        mutate it under their feet.
        """
        if not isinstance(config, dict):
            return config
        if (config.get("code") or "").strip():
            return config
        if (config.get("type") or "").strip().lower() != "llm":
            return config

        import json as _json
        system_prompt = config.get("system_prompt", "You are a helpful assistant.")
        max_history   = int(config.get("max_history", 32) or 32)
        bridge = self._LLM_BRIDGE_CODE_TEMPLATE.format(
            system_prompt_literal=_json.dumps(system_prompt),
            max_history=max_history,
        )

        out = dict(config)
        out["code"] = bridge
        # description and capabilities are commonly already set by the LLM
        # at spawn time; fill in sensible defaults if not so list_capabilities
        # still works on the remote side.
        out.setdefault("description",
                       f"LLM-driven agent (remote bridge). System prompt: "
                       f"{system_prompt[:80].rstrip()}{'...' if len(system_prompt) > 80 else ''}")
        out.setdefault("input_schema",  {"text": "str — the question or request"})
        out.setdefault("output_schema", {"text": "str — the LLM response"})
        logger.info(
            f"[{self.name}] Synthesized LLM bridge code for '{out.get('name')}': "
            f"{len(bridge)} chars, max_history={max_history}"
        )
        return out

    async def _spawn_remote(self, config: dict, node: str, save: bool) -> None:
        """
        Publish a spawn command to a remote node via MQTT.
        The remote_runner.py on that machine will receive it and run the agent.
        Remote agents appear in the dashboard exactly like local ones
        because they connect to the same MQTT broker.

        Also updates nodes/{node}/desired_state (retained) with ALL agents for
        this node so the runner can self-heal after a reboot.

        If the spawn config has an 'install' list, packages are installed on the
        remote node via SSH BEFORE the agent is spawned — so setup() won't fail
        with 'No module named X'.
        """
        # For LLM-type agents we ship a synthesized bridge code field over MQTT
        # (the remote runner only knows how to run DynamicAgent-shaped code).
        # We keep the registry-saved config CLEAN — just system_prompt, no code
        # — so the bridge is re-synthesized fresh on every spawn / restart /
        # migrate. This avoids carrying the bridge as dead baggage into local
        # spawn configs on migrate-back, and keeps the spawn registry small.
        wire_config = self._inject_llm_bridge_code(config)

        name     = wire_config.get("name", "remote-agent")
        packages = wire_config.get("install", [])
        if isinstance(packages, str):
            packages = [p.strip() for p in packages.replace(",", " ").split()]

        logger.info(f"[{self.name}] Spawning '{name}' on remote node '{node}'")

        # ── Install packages on remote node first ─────────────────────────────
        if packages:
            # Look up SSH credentials from known_nodes or ask installer
            node_info  = self._known_nodes.get(node, {})
            host       = node_info.get("host")
            # Try to get host from spawn registry (node_deploy stored it)
            # Try to get host from known_nodes, spawn registry, or installer's persisted credentials
            if not host:
                reg = self._get_spawn_registry()
                for cfg in reg.values():
                    if cfg.get("node") == node and cfg.get("host"):
                        host = cfg["host"]
                        break
            if not host and self._registry:
                installer = self._registry.find_by_name("installer")
                if installer:
                    host = installer.recall(f"node_host_{node}")
                    if not node_info.get("user"):
                        node_info["user"] = installer.recall(f"node_user_{node}") or "pi"

            if host and self._registry:
                installer = self._registry.find_by_name("installer")
                if installer:
                    # Load full persisted credentials for this node
                    node_creds = (installer.recall("_node_credentials") or {}).get(node, {})
                    ssh_user     = node_creds.get("user") or node_info.get("user", "pi")
                    ssh_password = node_creds.get("password") or ""
                    ssh_key_path = node_creds.get("key_path") or ""

                    logger.info(f"[{self.name}] Installing {packages} on {node} ({host}) before spawn...")
                    import uuid as _uuid
                    task_id = f"remote_install_{_uuid.uuid4().hex[:8]}"
                    future  = asyncio.get_running_loop().create_future()
                    self._result_futures[task_id] = future
                    install_payload = {
                        "action":    "node_install",
                        "host":      host,
                        "user":      ssh_user,
                        "packages":  packages,
                        "node_name": node,
                        "_task_id":  task_id,
                        "task":      task_id,
                    }
                    if ssh_password:
                        install_payload["password"] = ssh_password
                    if ssh_key_path:
                        install_payload["key_path"] = ssh_key_path
                    await self.send(installer.actor_id, MessageType.TASK, install_payload)
                    try:
                        result = await asyncio.wait_for(future, timeout=180.0)
                        if result.get("success"):
                            logger.info(f"[{self.name}] Remote install OK: {packages}")
                        else:
                            logger.warning(f"[{self.name}] Remote install issue: {result.get('error', '?')}")
                    except asyncio.TimeoutError:
                        logger.warning(f"[{self.name}] Remote install timed out — spawning anyway")
                    finally:
                        self._result_futures.pop(task_id, None)
                else:
                    logger.warning(f"[{self.name}] installer not found — skipping remote package install for '{name}'")
            else:
                logger.warning(
                    f"[{self.name}] No host known for node '{node}' — cannot pre-install {packages}. "
                    f"Install manually: ssh into {node} and run: pip install {' '.join(packages)} --break-system-packages"
                )

        # Publish individual spawn (for immediate delivery).
        # Uses wire_config so any LLM-bridge synthesis is included on the wire.
        await self._mqtt_publish(
            f"nodes/{node}/spawn",
            wire_config,
            retain=True,
            qos=1,
        )

        # Update desired state for the whole node (retained — survives Pi reboot).
        # Also uses wire_config so the runner can reconcile the agent into
        # existence with usable bridge code after a reboot.
        await self._update_node_desired_state(node, wire_config)

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "spawned", "message": f"Spawned '{name}' on node '{node}'",
             "child_name": name, "node": node, "timestamp": __import__("time").time()}
        )

        if save:
            # Persist the CLEAN config (no synthesized bridge code) so the
            # registry stays small and bridge regenerates fresh per spawn.
            self._save_to_spawn_registry(config)

        return None

    async def _update_node_desired_state(self, node: str, new_config: dict = None,
                                          remove_name: str = None) -> None:
        """
        Maintain nodes/{node}/desired_state as a retained MQTT message containing
        ALL agents that should run on this node. The runner reads this on startup
        and reconciles — spawning missing agents, ignoring already-running ones.
        """
        # Build desired state from spawn registry filtered to this node
        reg = self._get_spawn_registry()
        agents = {
            name: cfg for name, cfg in reg.items()
            if cfg.get("node", "").strip() == node
        }

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
        wire_agents = [self._inject_llm_bridge_code(cfg) for cfg in agents.values()]

        await self._mqtt_publish(
            f"nodes/{node}/desired_state",
            {"node": node, "agents": wire_agents,
             "timestamp": __import__("time").time()},
            retain=True,
            qos=1,
        )
        logger.info(f"[{self.name}] Desired state for '{node}': {list(agents.keys())}")

    # ── Node registry ──────────────────────────────────────────────────────

    def list_nodes(self) -> list[dict]:
        """Return all known remote nodes with their last-seen time, running agents, and system metrics."""
        import time as _time
        now = _time.time()
        return [
            {
                "node":        name,
                "agents":      info.get("agents", []),
                "last_seen":   info.get("last_seen", 0),
                "online":      (now - info.get("last_seen", 0)) < 30,
                "pid":         info.get("pid"),
                "uptime_s":    info.get("uptime_s"),
                "cpu_pct":     info.get("cpu_pct"),
                "mem_used_mb": info.get("mem_used_mb"),
                "mem_free_mb": info.get("mem_free_mb"),
            }
            for name, info in self._known_nodes.items()
        ]

    def list_topics(self, keyword: str = "") -> list[dict]:
        """
        Return all known MQTT topics published by agents, optionally filtered by keyword.
        Each entry: {"topic": str, "agents": [{"name", "node", "description"}, ...]}

        Example:
            list_topics("cpu")     → topics containing "cpu"
            list_topics("temp")    → topics containing "temp"
            list_topics()          → all topics
        """
        results = []
        kw = keyword.lower()
        for topic, manifests in self._topic_registry.items():
            if kw and kw not in topic.lower():
                continue
            results.append({
                "topic":   topic,
                "agents":  [{"name": m.get("name"), "node": m.get("node"),
                             "description": m.get("description", "")} for m in manifests],
            })
        return sorted(results, key=lambda x: x["topic"])

    def list_capabilities(self, keyword: str = "") -> list[dict]:
        """
        Return all known agents with their full capability profile:
        name, description, capabilities, input_schema, output_schema.

        Includes remote agents — they appear in _agent_manifests via the
        manifest listener (remote agents publish retained manifests just like
        local ones). The `running` flag is True for both local registry actors
        AND agents currently listed in a live node's heartbeat.
        """
        # Build the set of remotely-running agent names from live node heartbeats
        import time as _time
        remote_running: set = set()
        for nd in self._known_nodes.values():
            if _time.time() - nd.get("last_seen", 0) < 30:
                remote_running.update(nd.get("agents", []))

        results = []
        kw = keyword.lower().strip()
        kw_words = kw.split() if kw else []
        for name, manifest in self._agent_manifests.items():
            desc  = manifest.get("description", "")
            caps  = manifest.get("capabilities", [])
            if kw_words:
                haystack = desc.lower() + " " + " ".join(caps).lower() + " " + name.lower()
                if not any(w in haystack for w in kw_words):
                    continue
            local_running = bool(self._registry and self._registry.find_by_name(name))
            results.append({
                "name":          name,
                "node":          manifest.get("node"),
                "description":   desc,
                "capabilities":  caps,
                "input_schema":  manifest.get("input_schema",  {}),
                "output_schema": manifest.get("output_schema", {}),
                "spawnable":     manifest.get("spawnable", False),
                "running":       local_running or name in remote_running,
                "remote":        name in remote_running and not local_running,
            })
        return sorted(results, key=lambda x: x["name"])

    async def _manifest_listener(self):
        """
        Subscribe to agents/+/manifest and build a searchable topic registry.
        Retained manifests are delivered immediately on subscribe so the registry
        is populated even for agents that started before main restarted.

        An EMPTY retained payload is a tombstone — it means the agent has been
        deleted. We extract the agent_id from the topic and drop any matching
        manifest from both the agent registry and the topic registry. Without
        this, _agent_manifests would grow forever and stale entries would be
        reported as still-existing (with running=false but never disappearing).

        IMPORTANT: This listener is the SOURCE OF TRUTH for remote agents'
        TopicContracts. Local agents register themselves with the TopicBus
        directly (via DynamicAgent.declare_contract / publish / subscribe),
        but remote agents live in another process — main only learns about
        their real publish/subscribe topics through these MQTT manifests.
        Every accepted manifest is therefore mirrored into the TopicBus so
        the planner can auto-wire local and remote agents uniformly.
        """
        try:
            import aiomqtt
        except ImportError:
            return

        # Imported lazily once — cheap on subsequent uses.
        from ..core.topic_bus import TopicContract, get_topic_bus

        _last_exc_str: str | None = None
        while self.state.value not in ("stopped", "failed"):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe("agents/+/manifest")
                    logger.info("[main] Subscribed to agent manifests.")
                    _last_exc_str = None
                    async for msg in client.messages:
                        # ── Tombstone: empty payload means agent was deleted ──
                        raw_payload = msg.payload
                        if raw_payload is None or len(raw_payload) == 0:
                            # Topic format: agents/{actor_id}/manifest
                            topic_str = str(msg.topic)
                            try:
                                target_id = topic_str.split("/")[1]
                            except IndexError:
                                continue
                            # Find the manifest entry for this actor_id and drop it.
                            # Manifests are keyed by name, but each contains an actor_id.
                            removed_name = None
                            for name, manifest in list(self._agent_manifests.items()):
                                if manifest.get("actor_id") == target_id or name == target_id:
                                    self._agent_manifests.pop(name, None)
                                    removed_name = name
                                    break
                            if removed_name:
                                # Also drop from topic registry
                                for topic, entries in list(self._topic_registry.items()):
                                    self._topic_registry[topic] = [
                                        m for m in entries if m.get("name") != removed_name
                                    ]
                                    if not self._topic_registry[topic]:
                                        self._topic_registry.pop(topic, None)
                                # And drop from the TopicBus so the planner stops
                                # seeing this agent as a potential wiring target.
                                try:
                                    bus = get_topic_bus()
                                    if bus:
                                        bus.unregister(removed_name)
                                except Exception as _e:
                                    logger.debug(f"[main] TopicBus unregister failed for "
                                                 f"'{removed_name}': {_e}")
                                logger.info(f"[main] Manifest tombstone — removed '{removed_name}'")
                            continue

                        try:
                            data = json.loads(raw_payload.decode())
                        except Exception:
                            continue
                        if not isinstance(data, dict):
                            continue
                        agent_name = data.get("name", "?")
                        published  = data.get("publishes", [])
                        # Update topic registry
                        for topic in published:
                            existing = self._topic_registry.setdefault(topic, [])
                            # Replace existing entry for this agent or append
                            updated = False
                            for i, m in enumerate(existing):
                                if m.get("name") == agent_name:
                                    existing[i] = data
                                    updated = True
                                    break
                            if not updated:
                                existing.append(data)
                        # Also store full manifest by agent name for capability queries
                        self._agent_manifests[agent_name] = data

                        # ── Mirror into the TopicBus ──────────────────────────
                        # This is what makes remote agents visible to the planner's
                        # auto-wiring. Local agents register their own contracts
                        # directly; for remote agents the manifest is the only
                        # channel main has, so it MUST drive TopicBus state here.
                        # We honour observed_samples when present — those reflect
                        # the real field names the remote code publishes, which
                        # take precedence over LLM-declared produces_schema.
                        try:
                            bus = get_topic_bus()
                            if bus and agent_name and agent_name != "?":
                                observed = data.get("observed_samples", {}) or {}
                                produces = dict(data.get("produces_schema", {}) or {})
                                # Fold observed field names into produces_schema so
                                # the planner sees the actual wire format.
                                for _topic, sample in observed.items():
                                    if isinstance(sample, dict):
                                        for k, v in (sample.get("fields") or {}).items():
                                            produces[k] = v
                                contract = TopicContract(
                                    name            = agent_name,
                                    publishes       = list(published or []),
                                    subscribes      = list(data.get("subscribes", []) or []),
                                    triggers_when   = data.get("triggers_when", {}) or {},
                                    produces_schema = produces,
                                    consumes_schema = dict(data.get("consumes_schema",
                                                            data.get("input_schema", {}) or {})),
                                    actor_id        = data.get("actor_id"),
                                    node            = data.get("node"),
                                )
                                # Carry observed_samples through if the dataclass
                                # supports the field (it does on TopicContract).
                                if hasattr(contract, "observed_samples") and observed:
                                    try:
                                        contract.observed_samples = dict(observed)
                                    except Exception:
                                        pass
                                bus.register_contract(contract)
                        except Exception as _e:
                            logger.debug(f"[main] TopicBus register from manifest "
                                         f"failed for '{agent_name}': {_e}")

                        logger.debug(f"[main] Manifest from '{agent_name}': {published}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.state.value not in ("stopped", "failed"):
                    exc_str = str(e)
                    if exc_str != _last_exc_str:
                        logger.warning(f"[main] Manifest listener error: {e}. Reconnecting in 5s…")
                        _last_exc_str = exc_str
                    else:
                        logger.debug("[main] Manifest listener still unavailable — retrying in 5s…")
                    await asyncio.sleep(5)

    async def _state_return_listener(self):
        """
        Receive an agent's full config + persistent state from a remote node
        during remote→local migration triggered with the '@main' sentinel.

        Topic: nodes/+/state_return
        Payload:
          {
            "agent":        "<name>",
            "return_token": "<hex token>",          # must match a pending request
            "config":       {... full spawn config ...},   # includes code
            "state":        {... persistent state dict ...},
            "timestamp":    <unix epoch>,
          }

        On a valid match we strip the remote-node field, attach the state,
        and call _spawn_from_config() to bring the agent up locally. The 'config'
        is authoritative — it's whatever the remote runner had in memory for
        this agent, which already includes any runtime-learned contract data
        that the remote runner copied into its manifest.

        Tokens are single-use and expire after 5 minutes to keep the pending
        map bounded if a remote node never replies.
        """
        try:
            import aiomqtt
        except ImportError:
            return

        TOKEN_TTL_S = 300
        _last_exc_str: str | None = None
        while self.state.value not in ("stopped", "failed"):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe("nodes/+/state_return")
                    logger.info("[main] Subscribed to state_return topics.")
                    _last_exc_str = None
                    async for msg in client.messages:
                        # Drop empty retained messages (cleanup tombstones)
                        raw = msg.payload
                        if not raw:
                            continue
                        try:
                            data = json.loads(raw.decode())
                        except Exception:
                            continue
                        if not isinstance(data, dict):
                            continue
                        token = data.get("return_token", "")
                        pending: dict = getattr(self, "_pending_state_returns", {})

                        # Expire stale tokens before validating to keep the map tidy
                        import time as _t
                        now = _t.time()
                        for t, info in list(pending.items()):
                            if now - info.get("started_at", 0) > TOKEN_TTL_S:
                                pending.pop(t, None)

                        if not token or token not in pending:
                            logger.warning(
                                f"[main] state_return with unknown/expired token "
                                f"'{token[:8]}…' from {msg.topic} — ignoring"
                            )
                            continue
                        info = pending.pop(token)
                        agent_name = data.get("agent") or info.get("agent_name", "?")
                        from_node  = info.get("from_node", "?")
                        cfg        = data.get("config") or {}
                        state      = data.get("state")  or {}

                        if not cfg or not isinstance(cfg, dict):
                            logger.warning(
                                f"[main] state_return for '{agent_name}' from "
                                f"'{from_node}' has no config — cannot spawn locally"
                            )
                            continue

                        # Build a local spawn config: strip the remote-node
                        # field, attach state as _initial_state, force replace
                        # so any leftover local instance is cleanly swapped.
                        local_cfg = dict(cfg)
                        local_cfg.pop("node", None)
                        local_cfg.pop("_initial_state", None)
                        if state:
                            local_cfg["_initial_state"] = state
                        local_cfg["replace"] = True
                        local_cfg.setdefault("name", agent_name)

                        # For LLM agents, drop the synthesized bridge code we
                        # injected on the way out. Locally, type:"llm" routes
                        # to LLMAgent and the code field is ignored anyway —
                        # keeping it would just bloat the spawn registry and
                        # confuse anyone inspecting it. Detect by the marker
                        # comment in the synthesized template; user-written
                        # code never carries this header.
                        if (local_cfg.get("type") == "llm"
                                and "Auto-generated LLM bridge" in (local_cfg.get("code") or "")):
                            local_cfg.pop("code", None)

                        logger.info(
                            f"[main] Received state_return for '{agent_name}' "
                            f"from '{from_node}' "
                            f"({len(state) if isinstance(state, dict) else 0} "
                            f"state key(s)) — spawning locally"
                        )
                        try:
                            await self._spawn_from_config(local_cfg, save=True)
                            self._pending_notifications.append({
                                "_monitor_notification": True,
                                "message": (
                                    f"Migration of '{agent_name}' from "
                                    f"'{from_node}' → local succeeded."
                                ),
                                "severity": "info",
                                "timestamp": now,
                            })
                        except Exception as exc:
                            logger.exception(
                                f"[main] Local re-spawn after state_return failed "
                                f"for '{agent_name}': {exc}"
                            )
                            self._pending_notifications.append({
                                "_monitor_notification": True,
                                "message": (
                                    f"Migration of '{agent_name}' from "
                                    f"'{from_node}' → local FAILED: {exc}"
                                ),
                                "severity": "warning",
                                "timestamp": now,
                            })
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.state.value not in ("stopped", "failed"):
                    exc_str = str(e)
                    if exc_str != _last_exc_str:
                        logger.warning(f"[main] state_return listener error: {e}. Reconnecting in 5s…")
                        _last_exc_str = exc_str
                    else:
                        logger.debug("[main] state_return listener still unavailable — retrying in 5s…")
                    await asyncio.sleep(5)

    # ── Node deployment ────────────────────────────────────────────────────
    # /deploy lives here (not in io_agent) so every interface — CLI, UI,
    # Discord, future REST — shares one implementation. The streaming variant
    # is the canonical one; process_user_input collects its chunks for the
    # non-streaming path.

    async def _slash_deploy_stream(self, stripped: str):
        """
        Async generator implementing /deploy. Yields progress strings.

        Forms accepted:
            /deploy <node>                                     — discovery only
            /deploy <node> <host>                              — host given, ask for creds
            /deploy <node> <host> <user> <password> [broker]   — full deploy
        """
        import socket as _socket

        parts = stripped.split()
        if len(parts) < 2:
            yield ("[usage] /deploy <node-name> [host [user [password [broker]]]]\n"
                   "Run with just the node name to discover hosts automatically.")
            return

        node_name = parts[1]
        host      = parts[2] if len(parts) > 2 else ""
        user      = parts[3] if len(parts) > 3 else ""
        pw        = parts[4] if len(parts) > 4 else ""
        broker    = parts[5] if len(parts) > 5 else ""

        # ── Step 1: discover host if not provided ──────────────────────────
        if not host:
            yield f"[discover] Searching for '{node_name}' on the network..."

            # mDNS first — try a few candidate hostnames
            discovered = None
            for candidate in [f"{node_name}.local", "raspberrypi.local",
                               f"{node_name.replace('-', '')}.local"]:
                try:
                    ip = await asyncio.get_event_loop().run_in_executor(
                        None, _socket.gethostbyname, candidate
                    )
                    discovered = ip
                    yield f"[discover] Found via mDNS: {candidate} → {ip}"
                    break
                except _socket.gaierror:
                    pass

            # Fall back to subnet scan
            if not discovered:
                try:
                    local_ip = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: _socket.gethostbyname(_socket.gethostname())
                    )
                    subnet = ".".join(local_ip.split(".")[:3])
                except Exception:
                    subnet = "192.168.1"
                yield f"[discover] mDNS not found. Scanning {subnet}.1-254 for SSH..."
                found = await self._scan_subnet_ssh(subnet)
                if found:
                    host_list = "\n".join(f"  {ip}" for ip in found)
                    yield (
                        f"[discover] Found {len(found)} SSH-accessible host(s):\n{host_list}\n\n"
                        f"Re-run with the host you want:\n"
                        f"  /deploy {node_name} <host> <user> <password> [broker]"
                    )
                else:
                    yield (
                        "[discover] No SSH hosts found.\n"
                        f"Provide the host manually:\n"
                        f"  /deploy {node_name} <host> <user> <password> [broker]"
                    )
            else:
                yield (
                    f"[discover] Host found: {discovered}\n"
                    f"Re-run with credentials:\n"
                    f"  /deploy {node_name} {discovered} <user> <password> [broker]"
                )
            return

        # ── Step 2: need credentials ───────────────────────────────────────
        if not user or not pw:
            yield (
                f"[deploy] Host: {host}\n"
                f"Need SSH credentials. Re-run with:\n"
                f"  /deploy {node_name} {host} <user> <password> [broker]"
            )
            return

        # ── Step 3: deploy via installer agent ─────────────────────────────
        broker = broker or "localhost"
        if not hasattr(self, "delegate_to_installer"):
            yield "[error] Installer agent not available."
            return

        yield (f"[deploy] Deploying to {user}@{host} as node '{node_name}'...\n"
               f"(This may take 20-60 seconds while packages install on the remote machine)")
        try:
            result = await self.delegate_to_installer({
                "action":    "node_deploy",
                "host":      host,
                "user":      user,
                "password":  pw,
                "node_name": node_name,
                "broker":    broker,
            }, timeout=120.0)
        except Exception as exc:
            logger.exception(f"[main] /deploy failed for node '{node_name}'")
            yield f"[FAIL] Deploy failed: {exc}"
            return

        if result.get("success"):
            yield (
                f"[OK] Node '{node_name}' is live! It will appear in /nodes within ~15 seconds.\n\n"
                f"Spawn agents on it:\n"
                f"  \"spawn a CPU monitor agent on {node_name}\"\n"
                f"  \"spawn a temperature sensor on {node_name}\""
            )
        else:
            yield f"[FAIL] Deploy failed: {result.get('error', result)}"

    async def _scan_subnet_ssh(self, subnet: str) -> list:
        """Async port-22 scan of a /24 subnet. Returns sorted list of responding IPs."""
        found: list[str] = []
        sem = asyncio.Semaphore(60)

        async def probe(ip: str):
            async with sem:
                try:
                    _, w = await asyncio.wait_for(
                        asyncio.open_connection(ip, 22), timeout=0.4
                    )
                    w.close()
                    try:
                        await w.wait_closed()
                    except Exception:
                        pass
                    found.append(ip)
                except Exception:
                    pass

        await asyncio.gather(*[probe(f"{subnet}.{i}") for i in range(1, 255)])
        return sorted(found, key=lambda x: int(x.split(".")[-1]))

    async def migrate_agent(self, agent_name: str, target_node: str) -> dict:
        """
        Move a running agent to a different node.

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
        import time as _time

        reg = self._get_spawn_registry()
        config = reg.get(agent_name)
        have_code = bool(config and config.get("code"))

        # ── Locate the agent via every source we have ─────────────────────────
        # Registry first, then manifest, then heartbeats. We need at least to
        # know WHERE the agent is to do anything useful.
        manifest = self._agent_manifests.get(agent_name, {})
        current_node = ""
        if config:
            current_node = (config.get("node") or "").strip()
        if not current_node and manifest:
            current_node = (manifest.get("node") or "").strip()
        if not current_node:
            # Last resort — scan live heartbeats for which node lists this agent
            import time as _t
            for nd_name, nd in self._known_nodes.items():
                if _t.time() - nd.get("last_seen", 0) >= 30:
                    continue
                if agent_name in nd.get("agents", []):
                    current_node = nd_name
                    break

        # ── Verify the agent exists *somewhere* ───────────────────────────────
        # If we found nothing — not in registry, not in manifest, not heart-
        # beating from any node, not in the local registry — we genuinely
        # can't migrate what doesn't exist.
        local_alive = bool(self._registry and self._registry.find_by_name(agent_name))
        if not config and not manifest and not current_node and not local_alive:
            return {"success": False,
                    "message": f"Agent '{agent_name}' not found anywhere (no registry "
                               f"entry, no manifest, no heartbeat). Nothing to migrate."}

        if current_node == target_node:
            return {"success": False,
                    "message": f"Agent '{agent_name}' is already on "
                               f"'{target_node or 'local'}'."}

        # Normalise "local" as a target so users can type /migrate agent-name local
        is_target_local = target_node.strip().lower() in ("", "local", "main")

        if current_node and not is_target_local:
            # ── Remote → Remote migration ────────────────────────────────────
            # The source node still has the agent's compiled code and state;
            # it does the heavy lifting via its own _migrate_agent handler.
            # Main needs to update BOTH nodes' desired_state retained messages
            # so neither tries to re-spawn the agent in the wrong place after
            # a restart: source must forget the agent, target must remember it.
            logger.info(f"[{self.name}] Migrating '{agent_name}' from node "
                        f"'{current_node}' → '{target_node}'")
            await self._mqtt_publish(
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
                self._save_to_spawn_registry(updated)
                # Remove from source's desired_state (forget the agent there)
                # and add to target's desired_state (remember it on arrival).
                await self._update_node_desired_state(
                    current_node, remove_name=agent_name
                )
                await self._update_node_desired_state(target_node, updated)

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
                f"[{self.name}] Migrating '{agent_name}' from node "
                f"'{current_node}' → local (via @main sentinel; "
                f"{'spawn-registry code available as fallback' if have_code else 'no local code, fully remote-driven'})"
            )

            import secrets
            return_token = secrets.token_hex(8)
            # Stash the token so the listener knows this return is ours
            # and not from some other concurrent migration.
            self._pending_state_returns: dict = getattr(
                self, "_pending_state_returns", {}
            )
            self._pending_state_returns[return_token] = {
                "agent_name":  agent_name,
                "from_node":   current_node,
                "started_at":  _time.time(),
            }
            await self._mqtt_publish(
                f"nodes/{current_node}/migrate",
                {"name":         agent_name,
                 "target_node":  "@main",
                 "return_token": return_token},
                qos=1,
            )
            msg = (f"Migration of '{agent_name}' from '{current_node}' → local "
                   f"initiated (waiting for state from remote node).")
            logger.info(f"[{self.name}] {msg}")
            return {"success": True, "message": msg}

        else:
            # ── Local → Remote migration ─────────────────────────────────────
            # Requires the agent to be running locally. If it isn't, the
            # spawn-registry config would also be useless (no code shipped
            # over MQTT) so error early.
            if not local_alive and not have_code:
                return {"success": False,
                        "message": f"Agent '{agent_name}' not running locally and "
                                   f"no config in registry — cannot migrate."}

            logger.info(f"[{self.name}] Migrating LOCAL agent '{agent_name}' → remote node '{target_node}'")

            # Snapshot the local agent's persisted state before stopping it.
            # Only JSON-serialisable keys survive the MQTT trip.
            initial_state: dict = {}
            if self._registry:
                local = self._registry.find_by_name(agent_name)
                if local and hasattr(local, "_persistence_api") and local._persistence_api:
                    try:
                        raw = local._persistence_api.all()
                        dropped = []
                        for k, v in raw.items():
                            try:
                                import json as _json
                                _json.dumps(v)
                                initial_state[k] = v
                            except (TypeError, ValueError):
                                dropped.append(k)
                        if dropped:
                            logger.warning(
                                f"[{self.name}] Local→remote migrate '{agent_name}': "
                                f"dropping non-JSON state keys {dropped}"
                            )
                        if initial_state:
                            logger.info(
                                f"[{self.name}] Carrying {len(initial_state)} state key(s) "
                                f"from local to '{target_node}': {list(initial_state.keys())}"
                            )
                    except Exception as e:
                        logger.warning(f"[{self.name}] Could not snapshot local state for '{agent_name}': {e}")

            # Snapshot the live topic contract from the TopicBus, then merge it
            # into the config we ship to the remote node. The spawn registry
            # only has what was REQUESTED at spawn time, but the local agent
            # may have learned new topics at runtime (via publish/subscribe
            # auto-registration or declare_contract). Without this merge those
            # topics would be lost across the migration and the remote agent
            # would publish a manifest missing them — breaking auto-wiring.
            live_contract: dict = {}
            try:
                from ..core.topic_bus import get_topic_bus
                bus = get_topic_bus()
                if bus:
                    c = bus.registry.get(agent_name)
                    if c is not None:
                        live_contract = {
                            "publishes":       list(c.publishes or []),
                            "subscribes":      list(c.subscribes or []),
                            "triggers_when":   dict(c.triggers_when or {}),
                            "produces_schema": dict(c.produces_schema or {}),
                            "consumes_schema": dict(c.consumes_schema or {}),
                        }
                        # observed_samples is a separate field on the contract
                        if hasattr(c, "observed_samples") and c.observed_samples:
                            live_contract["observed_samples"] = dict(c.observed_samples)
                        logger.info(
                            f"[{self.name}] Captured live contract for '{agent_name}': "
                            f"pub={live_contract['publishes']} sub={live_contract['subscribes']}"
                        )
            except Exception as _e:
                logger.debug(f"[{self.name}] Could not capture live contract: {_e}")

            # Stop the local instance, then purge its persistence.
            #
            # We've already snapshotted `initial_state` above — that's the
            # authoritative copy now being shipped to the remote node. The
            # local SQLite rows / pickle / Redis values are about to become
            # stale ghosts. If the user later migrates the agent back here
            # without those being cleared, they'd merge with the freshly
            # arrived state and produce duplicate conversation entries.
            if self._registry:
                local = self._registry.find_by_name(agent_name)
                if local:
                    try:
                        await self._registry.unregister(local.actor_id)
                        await local.stop()
                        self._agent_manifests.pop(agent_name, None)
                        # Wipe SQLite / Redis / pickle for this agent. Uses
                        # the same purge primitive as permanent delete — the
                        # difference is the agent is being re-created on the
                        # target node with the snapshot we already have.
                        try:
                            await self._purge_local_agent_persistence(local, agent_name)
                        except Exception as e:
                            logger.warning(
                                f"[{self.name}] Could not purge local persistence "
                                f"for '{agent_name}' after local→remote migration: {e}"
                            )
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.warning(f"[{self.name}] Could not stop local '{agent_name}': {e}")

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
                if v:                       # don't overwrite with empty values
                    new_config[k] = v

            await self._spawn_remote(new_config, target_node, save=True)
            # _spawn_remote already saved the full new_config (including state +
            # live contract). The subsequent _save_to_spawn_registry below would
            # OVERWRITE it with the stale `config`, so skip it for this branch.
            msg = (f"Migrating '{agent_name}' from 'local' "
                   f"→ '{target_node}'. It will appear in the dashboard shortly.")
            logger.info(f"[{self.name}] {msg}")
            return {"success": True, "message": msg}

        # NOTE: with the @main sentinel now handling all remote→local cases
        # and the inline remote→remote registry update above, this trailing
        # block is no longer reached in normal flow. Kept as a defensive
        # net for any future path that finishes without updating the
        # spawn registry — the write is idempotent.
        if config:
            updated = dict(config)
            updated["node"] = target_node
            self._save_to_spawn_registry(updated)

        msg = (f"Migrating '{agent_name}' from '{current_node or 'local'}' "
               f"→ '{target_node or 'local'}'. It will appear in the dashboard shortly.")
        logger.info(f"[{self.name}] {msg}")
        return {"success": True, "message": msg}

    async def _node_heartbeat_listener(self):
        """
        Subscribe to nodes/+/heartbeat so main knows which remote nodes are online.
        Updates self._known_nodes which is used by list_nodes() and the LLM context.

        Also detects agents that silently vanished from a node (crash, OOM kill,
        manual kill, deploy gone wrong) by diffing each heartbeat's agent list
        against what we last saw. Anything that disappeared and is still in the
        spawn registry as belonging to this node is treated as a deletion event:
        manifest cleared, registry entry removed, history note added.
        """
        try:
            import aiomqtt
        except ImportError:
            logger.warning("[main] aiomqtt not available — node heartbeat tracking disabled.")
            return

        _last_exc_str: str | None = None
        while self.state.value not in ("stopped", "failed"):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe("nodes/+/heartbeat")
                    await client.subscribe("nodes/+/migrate_result")
                    logger.info("[main] Subscribed to node heartbeats.")
                    _last_exc_str = None
                    async for msg in client.messages:
                        topic = str(msg.topic)
                        try:
                            data = json.loads(msg.payload.decode())
                        except Exception:
                            continue

                        parts = topic.split("/")
                        if len(parts) < 3:
                            continue
                        node_name = parts[1]

                        if topic.endswith("/heartbeat"):
                            import time as _t
                            new_agents = data.get("agents", [])
                            # ── Diff against previous snapshot for this node ──
                            prev = self._known_nodes.get(node_name, {})
                            prev_agents = set(prev.get("agents", []))
                            curr_agents = set(new_agents)
                            disappeared = prev_agents - curr_agents
                            if disappeared:
                                # Only count as silent-loss if the spawn registry still
                                # claims the agent should be on this node. Migration
                                # updates the registry before the old node stops the
                                # agent, so a migrated agent won't trip this check.
                                reg = self._get_spawn_registry()
                                for agent_name in disappeared:
                                    cfg = reg.get(agent_name)
                                    if not cfg:
                                        continue   # already deleted via /agents — nothing to do
                                    if cfg.get("node", "").strip() != node_name:
                                        continue   # migrated away — expected disappearance
                                    logger.warning(
                                        f"[main] Agent '{agent_name}' silently disappeared "
                                        f"from node '{node_name}' (crash/kill suspected)"
                                    )
                                    # Same cleanup as a manual delete, minus the node-side
                                    # stop signal (it's already gone there).
                                    self._remove_from_spawn_registry(agent_name)
                                    await self._clear_agent_manifest(agent_name)
                                    self._record_agent_deletion(
                                        agent_name,
                                        reason=f"vanished from node '{node_name}' (crash or external kill)",
                                    )
                            self._known_nodes[node_name] = {
                                "last_seen":   _t.time(),
                                "agents":      new_agents,
                                "node_id":     data.get("node_id", ""),
                                "pid":         data.get("pid"),
                                "uptime_s":    data.get("uptime_s"),
                                "cpu_pct":     data.get("cpu_pct"),
                                "mem_used_mb": data.get("mem_used_mb"),
                                "mem_free_mb": data.get("mem_free_mb"),
                            }
                            # Bootstrap TopicContracts for remote agents whose
                            # retained manifest hasn't been delivered yet (e.g.
                            # main just restarted, or the remote node is mid-
                            # reconnect). The MQTT manifest listener is the
                            # source of truth for remote contracts — it carries
                            # the agent's REAL publishes/subscribes/schemas and
                            # observed_samples. The spawn-config we have here
                            # only reflects what was REQUESTED at spawn time
                            # and may be wrong/empty, so we must NEVER overwrite
                            # a manifest-derived contract with it.
                            try:
                                from ..core.topic_bus import TopicContract, get_topic_bus
                                bus = get_topic_bus()
                                if bus:
                                    spawn_reg = self._get_spawn_registry()
                                    for aname in new_agents:
                                        # Skip if the manifest listener has
                                        # already registered a contract for
                                        # this agent — that one is authoritative.
                                        if bus.registry.get(aname) is not None:
                                            continue
                                        # Skip if a manifest is already cached
                                        # in main — the listener will register
                                        # it imminently; no need to race.
                                        if aname in self._agent_manifests:
                                            continue
                                        cfg = spawn_reg.get(aname)
                                        if not cfg:
                                            continue
                                        # Only bootstrap if the spawn config
                                        # actually declared topics — an empty
                                        # contract would just pollute the bus.
                                        if not (cfg.get("publishes") or cfg.get("subscribes")):
                                            continue
                                        contract = TopicContract.from_spawn_config(
                                            {**cfg, "node": node_name,
                                             "actor_id": str(__import__("uuid").uuid5(
                                                 __import__("uuid").NAMESPACE_DNS,
                                                 f"wactorz.actor.{aname}"
                                             ))}
                                        )
                                        bus.register_contract(contract)
                                        logger.debug(
                                            f"[main] Bootstrapped TopicContract for "
                                            f"'{aname}' from spawn config (manifest pending)."
                                        )
                            except Exception as _e:
                                logger.debug(f"[main] TopicContract bootstrap failed: {_e}")
                            # P1: keep monitor's heartbeat tracker in sync for remote agents
                            # so it raises alerts when a remote agent goes silent, exactly
                            # as it does for local ones.
                            if self._registry:
                                mon = self._registry.find_by_name("monitor")
                                if mon and hasattr(mon, "_last_seen"):
                                    for aname in new_agents:
                                        # Build the same deterministic actor_id used by _RemoteAgent
                                        remote_id = str(__import__("uuid").uuid5(
                                            __import__("uuid").NAMESPACE_DNS,
                                            f"wactorz.actor.{aname}"
                                        ))
                                        mon._last_seen[remote_id] = _t.time()
                        elif topic.endswith("/migrate_result"):
                            success = data.get("success", False)
                            agent   = data.get("agent", "?")
                            to_node = data.get("to_node", "?")
                            sev     = "info" if success else "warning"
                            self._pending_notifications.append({
                                "_monitor_notification": True,
                                "message": (
                                    f"Migration of '{agent}' to '{to_node}' succeeded."
                                    if success else
                                    f"Migration of '{agent}' failed: {data.get('error', '?')}"
                                ),
                                "severity": sev,
                                "timestamp": __import__("time").time(),
                            })

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.state.value not in ("stopped", "failed"):
                    exc_str = str(e)
                    if exc_str != _last_exc_str:
                        logger.warning(f"[main] Node heartbeat listener error: {e}. Reconnecting in 5s…")
                        _last_exc_str = exc_str
                    else:
                        logger.debug("[main] Node heartbeat listener still unavailable — retrying in 5s…")
                    await asyncio.sleep(5)

    async def _node_offline_watcher(self):
        """
        Periodically check for nodes that have gone silent. If a node has not
        sent a heartbeat in NODE_OFFLINE_GRACE_S, treat all its agents as gone
        and drop the node from our tracking.

        Uses a longer threshold (90s) than the visual "offline" indicator (30s)
        so brief network blips don't trigger false deletion notes. The visual
        indicator stays at 30s for snappy UX; this watcher waits long enough
        to be sure the node is genuinely down.
        """
        import time as _t
        NODE_OFFLINE_GRACE_S = 90.0
        CHECK_INTERVAL_S      = 15.0

        while self.state.value not in ("stopped", "failed"):
            try:
                await asyncio.sleep(CHECK_INTERVAL_S)
                now = _t.time()
                # Snapshot to avoid mutation-during-iteration
                stale_nodes = [
                    (name, info)
                    for name, info in list(self._known_nodes.items())
                    if (now - info.get("last_seen", 0)) > NODE_OFFLINE_GRACE_S
                ]
                if not stale_nodes:
                    continue

                reg = self._get_spawn_registry()
                for node_name, info in stale_nodes:
                    logger.warning(
                        f"[main] Node '{node_name}' has been silent for >"
                        f"{NODE_OFFLINE_GRACE_S:.0f}s — treating as offline"
                    )
                    # Find all agents that belong to this node according to the
                    # spawn registry (the heartbeat's last-known agent list may
                    # be stale).
                    lost = [
                        n for n, cfg in reg.items()
                        if cfg.get("node", "").strip() == node_name
                    ]
                    for agent_name in lost:
                        self._remove_from_spawn_registry(agent_name)
                        await self._clear_agent_manifest(agent_name)
                        self._record_agent_deletion(
                            agent_name,
                            reason=f"node '{node_name}' went offline",
                        )
                    # Drop the node from our tracking. If it comes back, the
                    # heartbeat listener will re-add it as a fresh entry.
                    self._known_nodes.pop(node_name, None)
                    if lost:
                        self._pending_notifications.append({
                            "_monitor_notification": True,
                            "message": (
                                f"Node '{node_name}' is offline. "
                                f"Lost agents: {', '.join(lost)}."
                            ),
                            "severity": "warning",
                            "timestamp": now,
                        })
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[main] Node offline watcher error: {e}")

    # ── Delegation ─────────────────────────────────────────────────────────

    async def _llm_bridge_listener(self):
        """
        Listens on main/llm_request for LLM calls from remote agents.

        Remote agents call agent.ask_llm(prompt) or agent.chat(messages).
        The request arrives here with a _reply_topic; we run the LLM call
        using this node's configured provider and API key, then publish the
        text response back to _reply_topic so the remote agent's future resolves.

        This keeps API keys on the main machine — edge devices never need them.

        Request payload:
          prompt        — str (single-turn)    OR
          messages      — list of {role, content} (multi-turn)
          system        — optional system prompt override
          _reply_topic  — where to send the result
          agent         — name of requesting agent (for logging)
          node          — node name (for logging)
        """
        try:
            import aiomqtt
        except ImportError:
            logger.warning("[main] aiomqtt not available — LLM bridge disabled.")
            return

        _last_exc_str: str | None = None
        while self.state.value not in ("stopped", "failed"):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe("main/llm_request")
                    logger.info("[main] LLM bridge listening on main/llm_request")
                    _last_exc_str = None
                    async for msg in client.messages:
                        try:
                            data = json.loads(msg.payload.decode())
                        except Exception:
                            continue

                        reply_topic  = data.get("_reply_topic")
                        agent_name   = data.get("agent", "remote-agent")
                        node_name    = data.get("node", "?")
                        system_over  = data.get("system", "")
                        prompt       = data.get("prompt", "")
                        messages_in  = data.get("messages")   # multi-turn path

                        if not reply_topic:
                            continue

                        logger.info(f"[main] LLM bridge: request from '{agent_name}' on '{node_name}'")

                        try:
                            # Build message list — either multi-turn or single prompt
                            if messages_in and isinstance(messages_in, list):
                                llm_messages = messages_in
                            else:
                                llm_messages = [{"role": "user", "content": prompt}]

                            system = system_over or (
                                f"You are {agent_name}, an AI agent running on node {node_name}."
                            )

                            # Use this node's LLM provider
                            if self.llm is None:
                                text = ("[LLM error: no provider configured "
                                        "on main]")
                                logger.warning(
                                    f"[main] LLM bridge: request from "
                                    f"'{agent_name}' but main has no LLM "
                                    f"provider"
                                )
                            else:
                                # LLMProvider.complete() returns (text, usage)
                                response, usage = await self.llm.complete(
                                    messages=llm_messages,
                                    system=system,
                                )
                                text = response if isinstance(response, str) else str(response)
                                # Roll bridge usage into main's totals so cost
                                # tracking on this node stays accurate. Per-
                                # agent attribution lives on the agent metrics
                                # path — adding that here would need the
                                # remote agent's actor_id, which we don't
                                # require in the bridge request schema today.
                                try:
                                    self.total_input_tokens  += usage.get("input_tokens", 0)
                                    self.total_output_tokens += usage.get("output_tokens", 0)
                                    self.total_cost_usd      += usage.get("cost_usd", 0.0)
                                    self._persist_cost()
                                except Exception:
                                    pass

                        except Exception as e:
                            logger.error(f"[main] LLM bridge error for '{agent_name}': {e}")
                            text = f"LLM error: {e}"

                        await self._mqtt_publish(reply_topic, {"text": text})
                        logger.info(
                            f"[main] LLM bridge: replied to '{agent_name}' "
                            f"({len(text)} chars) → {reply_topic}"
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.state.value not in ("stopped", "failed"):
                    exc_str = str(e)
                    if exc_str != _last_exc_str:
                        logger.warning(f"[main] LLM bridge listener error: {e}. Reconnecting in 5s…")
                        _last_exc_str = exc_str
                    else:
                        logger.debug("[main] LLM bridge listener still unavailable — retrying in 5s…")
                    await asyncio.sleep(5)

    async def _remote_observed_samples_listener(self):
        """
        Subscribe to all MQTT topics published by remote agents and update
        their TopicContract.observed_samples with real payload field names.

        This is what gives the planner accurate schema context for remote
        agents — the declared produces_schema may use wrong field names, but
        the observed_samples reflect what the code ACTUALLY publishes.

        We subscribe to:
          agents/+/data/#    — agent.publish_data() calls from remote agents
          custom/#           — agent-to-agent data streams
          sensors/#          — common IoT namespace
        Additional topic patterns can be added as needed.
        """
        try:
            import aiomqtt
        except ImportError:
            return

        # Build a reverse map: topic prefix → agent name, kept fresh from spawn registry
        def _topic_to_agent(topic: str) -> Optional[str]:
            """Best-effort: find which remote agent publishes to this topic."""
            try:
                from ..core.topic_bus import get_topic_bus
                bus = get_topic_bus()
                if bus:
                    producers = bus.registry.producers_of(topic)
                    if producers:
                        return producers[0].name
            except Exception:
                pass
            return None

        while self.state.value not in ("stopped", "failed"):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    for pattern in ("agents/+/data/#", "custom/#", "sensors/#"):
                        await client.subscribe(pattern)
                    async for msg in client.messages:
                        topic = str(msg.topic)
                        # Skip internal wactorz topics
                        if any(topic.startswith(p) for p in
                               ("agents/by-name", "nodes/", "main/")):
                            continue
                        try:
                            payload = json.loads(msg.payload.decode())
                        except Exception:
                            continue
                        if not isinstance(payload, dict):
                            continue

                        agent_name = _topic_to_agent(topic)
                        if not agent_name:
                            continue

                        try:
                            from ..core.topic_bus import get_topic_bus
                            bus = get_topic_bus()
                            if bus:
                                contract = bus.registry.get(agent_name)
                                if contract:
                                    contract.update_observed(topic, payload)
                        except Exception:
                            pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.state.value not in ("stopped", "failed"):
                    await asyncio.sleep(5)

    async def delegate_to_installer(self, payload: dict, timeout: float = 300.0) -> dict:
        """
        Send a task to the installer agent and wait for the result.
        Handles node_deploy, node_install, node_run, install, check actions.
        timeout is generous (300s) because deploys involve SSH + pip installs.
        """
        if not self._registry:
            return {"error": "No registry available"}
        installer = self._registry.find_by_name("installer")
        if not installer:
            return {"error": "installer agent not found"}

        import uuid as _uuid
        task_id = f"inst_{_uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._result_futures[task_id] = future

        payload = dict(payload)
        payload["_task_id"] = task_id
        payload["task"]     = task_id

        await self.send(installer.actor_id, MessageType.TASK, payload)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"error": f"Installer timed out after {timeout}s"}
        finally:
            self._result_futures.pop(task_id, None)

    async def delegate_task(self, target_name: str, task: str, timeout: float = 60.0) -> Optional[dict]:
        """
        Send a task to a named agent and wait for its result.

        Routing priority:
          1. Local registry — fast in-process mailbox path
          2. Remote node   — MQTT request/reply via agents/by-name/{name}/task
          3. Returns None if the agent is unknown in both
        """
        if not self._registry:
            return None

        target = self._registry.find_by_name(target_name)
        if target:
            # ── Local path (fast, in-process) ────────────────────────────────
            task_id = uuid.uuid4().hex
            future  = asyncio.get_event_loop().create_future()
            self._result_futures[task_id] = future
            await self.send(target.actor_id, MessageType.TASK, {
                "text":     task,
                "_task_id": task_id,
                "task":     task_id,
                "reply_to": self.actor_id,
            })
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                self._result_futures.pop(task_id, None)

        # ── Remote path: check if agent is on a known node ───────────────────
        remote_node = None
        for node_name, nd in self._known_nodes.items():
            if target_name in nd.get("agents", []):
                remote_node = node_name
                break

        if not remote_node:
            logger.warning(f"[{self.name}] delegate_task: '{target_name}' not found locally or remotely")
            return None

        reply_topic = f"main/reply/{self.actor_id}/{uuid.uuid4().hex[:8]}"
        future      = asyncio.get_event_loop().create_future()
        self._result_futures[reply_topic] = future

        await self._mqtt_publish(
            f"agents/by-name/{target_name}/task",
            {"text": task, "payload": task, "_reply_topic": reply_topic,
             "_remote_task": True},
        )

        async def _wait_reply():
            try:
                import aiomqtt
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe(reply_topic)
                    async for msg in client.messages:
                        try:
                            data = json.loads(msg.payload.decode())
                            if not future.done():
                                future.set_result(data)
                        except Exception:
                            pass
                        return
            except Exception as e:
                if not future.done():
                    future.set_exception(e)

        reply_task = asyncio.create_task(_wait_reply())
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] delegate_task: '{target_name}' on '{remote_node}' timed out after {timeout}s")
            return None
        finally:
            reply_task.cancel()
            self._result_futures.pop(reply_topic, None)

    async def list_agents(self) -> list[dict]:
        if not self._registry:
            return []
        return [a.get_status() for a in self._registry.all_actors()]

    async def send_command(self, target_name: str, command: MessageType):
        if not self._registry:
            return
        target = self._registry.find_by_name(target_name)
        if target:
            await self.send(target.actor_id, command)

    async def delete_spawned_agent(self, name: str):
        """
        Permanently delete an agent.

        Unlike a stop (which preserves state so the agent can resume later),
        delete removes EVERY trace so a future spawn with the same name
        starts truly clean:

          - Spawn registry entry removed (so no auto-respawn on restart).
          - For remote agents: the `nodes/<node>/stop` message carries
            ``delete=True`` so the runner unlinks <name>_state.json on disk
            and purges this agent's retained MQTT topics from the broker.
          - For local agents: the underlying PersistenceAPI.purge() wipes
            SQLite kv_store rows, Redis ephemeral keys, and the agent's
            state.pkl directory.
          - Either way, main also publishes empty retained payloads on the
            per-agent MQTT topics as a defensive second pass — if the runner
            is offline or main is acting alone, the broker is still cleared.
        """
        # Find node before removing from registry
        reg  = self._get_spawn_registry()
        node = reg.get(name, {}).get("node", "").strip()

        # Capture/derive the deterministic actor_id BEFORE we tear anything down,
        # so we can purge per-agent retained topics even after the local actor
        # is gone from the registry.
        actor_id: Optional[str] = None
        if self._registry:
            target = self._registry.find_by_name(name)
            if target:
                actor_id = target.actor_id
        if not actor_id:
            # Remote agents (and local ones missing from the registry) follow the
            # same uuid5 scheme used by _RemoteAgent and Actor — derive it.
            actor_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.actor.{name}"))

        self._remove_from_spawn_registry(name)

        # Remote path — let the runner do the heavy work on the edge node.
        if node:
            await self._update_node_desired_state(node, remove_name=name)
            await self._mqtt_publish(
                f"nodes/{node}/stop",
                {"name": name, "delete": True},
                qos=1,
            )
            await self._clear_agent_manifest(name, actor_id)
            await self._purge_agent_retained_topics(actor_id)
            self._record_agent_deletion(name, reason=f"deleted from node '{node}'")
            return

        # Local path — stop the actor and wipe its persistence directly.
        if self._registry:
            target = self._registry.find_by_name(name)
            if target:
                actor_id = target.actor_id
                sv = getattr(self._registry, "_supervisor_ref", None)
                if sv is not None:
                    sv.release(name)
                await self._registry.unregister(actor_id)
                await target.stop()
                await self._purge_local_agent_persistence(target, name)
                await self._clear_agent_manifest(name, actor_id)
                await self._purge_agent_retained_topics(actor_id)
                self._record_agent_deletion(name, reason="deleted")
                return

        # Agent wasn't in the registry — still purge the broker side so any
        # stale retained messages from a previous incarnation are cleared.
        await self._clear_agent_manifest(name, actor_id)
        await self._purge_agent_retained_topics(actor_id)
        self._record_agent_deletion(name, reason="deleted (no live actor found)")

    async def _purge_agent_retained_topics(self, actor_id: str) -> None:
        """
        Publish empty retained payloads on every per-agent MQTT topic so the
        broker stops re-delivering them on later reconnects.

        Mirrors the same purge done by the remote runner on delete and by the
        monitor process — running it from all three sides is intentional, so
        deletion succeeds even when one side is offline.
        """
        if not actor_id:
            return
        for metric in ("status", "heartbeat", "metrics", "logs", "spawned",
                       "manifest", "errors", "detections", "results", "completed"):
            try:
                await self._mqtt_publish(
                    f"agents/{actor_id}/{metric}", b"", retain=True
                )
            except Exception as e:
                logger.debug(
                    f"[{self.name}] Failed to clear retained agents/{actor_id}/{metric}: {e}"
                )

    async def _purge_local_agent_persistence(self, actor, name: str) -> None:
        """
        For a local actor: hard-delete its persisted state across all
        backends (SQLite kv_store rows, Redis ephemeral keys, pickle file).

        Uses the actor's own PersistenceAPI when available so the right
        databases are touched. Falls back to a best-effort filesystem cleanup
        if the new API isn't wired up (legacy pickle-only mode).
        """
        # Preferred path: actor has the unified PersistenceAPI.
        api = getattr(actor, "_persistence_api", None) or getattr(actor, "_persistence", None)
        if api is not None and hasattr(api, "purge"):
            try:
                api.purge()
                return
            except Exception as e:
                logger.warning(
                    f"[{self.name}] PersistenceAPI.purge() failed for '{name}': {e} — "
                    f"falling back to filesystem cleanup"
                )

        # Legacy fallback: nuke the agent's pickle directory directly.
        pdir = getattr(actor, "_persistence_dir", None)
        if pdir is not None:
            try:
                import shutil
                pdir_path = str(pdir)
                shutil.rmtree(pdir_path, ignore_errors=True)
                logger.info(
                    f"[{self.name}] Removed local persistence dir for '{name}': {pdir_path}"
                )
            except Exception as e:
                logger.warning(
                    f"[{self.name}] Could not remove persistence dir for '{name}': {e}"
                )