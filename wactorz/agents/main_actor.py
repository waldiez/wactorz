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

from ..core.actor import Actor, Message, MessageType, ActorState
from .llm_agent import LLMAgent, LLMProvider

logger = logging.getLogger(__name__)

class _SpawnPlaceholder:
    """Returned when an agent is being installed+spawned in the background."""
    def __init__(self, name: str):
        self.name = name



SPAWN_REGISTRY_KEY   = "_spawned_agents"
PIPELINE_RULES_KEY   = "_pipeline_rules"
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
    Use agent.send_to(name, payload) or mention @agent-name in your response to trigger it.
    The system will auto-spawn it before routing.
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
  agent.mqtt_get(topic, timeout=10)   — wait for one MQTT message on topic, returns parsed payload
                                        USE THIS to read data from remote/Pi agents instead of send_to()
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

  agent.llm                           — pre-configured LLM (same as main, already authenticated)
  agent.llm.chat(prompt, system="")   — single-turn LLM call, returns string
  agent.llm.complete(messages, system="") — multi-turn LLM call with full history

  The LLM provider is set at startup (Anthropic / OpenAI / Ollama / NVIDIA NIM).
  Agents always use the same provider as main — no configuration needed inside agent code.

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

== CRITICAL: NEVER PROXY TASKS ==
NEVER say "I'll forward that to X agent" and then do nothing.
NEVER pretend to send tasks on behalf of the user.
If the user wants to talk to another agent, tell them:
  "Use @agent-name to talk to that agent directly."
You are the ORCHESTRATOR. You spawn agents and answer questions.
You do NOT act as a middleman for agent conversations.

== EXISTING AGENTS ==
- main                    : you (orchestrator)
- monitor                 : health monitoring
- installer               : installs Python packages locally AND on remote nodes via SSH
                            Actions: install, node_deploy, node_install, node_run, check, history
- manual-agent            : finds device manuals online and answers questions from PDFs (type: manual)
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
AgentFlow can run agents on any machine (Raspberry Pi, VM, cloud server) that is
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

== AGENT MIGRATION ==
To move a running agent from one machine to another, call migrate_agent():

  result = await main.migrate_agent("agent-name", "target-node-name")

The system will:
  1. Stop the agent on its current machine
  2. Start it fresh on the target machine
  3. Update the spawn registry so it restores to the right machine on restart
  4. Notify you via the dashboard when migration completes

Example:
  User: "Move temp-sensor to rpi-bedroom"
  You:  await main.migrate_agent("temp-sensor", "rpi-bedroom")

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
            await conn.run('mkdir -p ~/agentflow')
            await agent.log(f'[{node_name}] Created ~/agentflow')

            # Upload remote_runner.py
            async with conn.start_sftp_client() as sftp:
                await sftp.put(str(runner_path), f'/home/{user}/agentflow/remote_runner.py')
            await agent.log(f'[{node_name}] Uploaded remote_runner.py')

            # Install deps
            await conn.run('pip install aiomqtt psutil --break-system-packages -q 2>&1')
            await agent.log(f'[{node_name}] Dependencies installed')

            # Kill existing instance
            await conn.run(f'pkill -f "remote_runner.py.*--name {node_name}" 2>/dev/null; true')

            # Start in background
            cmd = (
                f'nohup python3 ~/agentflow/remote_runner.py '
                f'--broker {broker} --name {node_name} '
                f'> ~/agentflow/{node_name}.log 2>&1 &'
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
    agent.state['model'] = YOLO('yolov8n.pt')
    agent.state['cap'] = cv2.VideoCapture(0)
    await agent.log('Camera opened, model loaded')

async def process(agent):
    import time
    cap = agent.state.get('cap')
    model = agent.state.get('model')
    if not cap or not model:
        return
    import asyncio
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
        "Respond with exactly one token: HA, PIPELINE, or OTHER.\n\n"
        "HA = a direct, one-shot Home Assistant action or query:\n"
        "  - Turn on/off a device right now\n"
        "  - List devices, areas, entities, automations\n"
        "  - Create/edit/delete a HA automation\n"
        "  - Set temperature, dim lights, lock door — immediate action\n\n"
        "PIPELINE = a reactive rule that should run continuously:\n"
        "  - 'if X happens then do Y' — any conditional/reactive logic\n"
        "  - 'when X send me a message/notification'\n"
        "  - 'whenever X turns on/off do Y'\n"
        "  - Any rule involving a sensor state change triggering an action or notification\n"
        "  - Any webcam/camera detection triggering anything\n"
        "  - Anything involving Discord/Telegram notifications triggered by an event\n\n"
        "OTHER = general conversation, coding, questions, anything not HA or pipeline related."
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
        # Listen for agent capability manifests to build topic registry
        self._tasks.append(asyncio.create_task(self._manifest_listener()))
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

    def get_notification_urls(self) -> dict:
        """Return persisted notification webhook URLs (discord, telegram, slack, etc.)"""
        return self.recall("_notification_urls") or {}

    # ── User facts ─────────────────────────────────────────────────────────
    # Key facts extracted from conversation: HA URL, entity names, preferences,
    # user name, webhook URLs, etc. Stored separately from history so they
    # survive summarization and persist indefinitely.

    _FACTS_EXTRACT_PROMPT = (
        "Extract durable facts from this conversation exchange that would be useful to remember "
        "long-term. Focus on: names, locations, device entity IDs, URLs, credentials, preferences, "
        "configurations, and any explicit statements about the user's setup.\n"
        "Return a JSON object with short descriptive keys and concise values. "
        "Return {} if nothing worth remembering was said.\n"
        "Example: {\"ha_url\": \"http://192.168.1.10:8123\", \"user_name\": \"Alex\", "
        "\"living_room_light\": \"light.wiz_rgbw_tunable_02cba0\"}\n"
        "Output only valid JSON. No explanation, no markdown."
    )

    def get_user_facts(self) -> dict:
        return self.recall("_user_facts") or {}

    def _inject_user_facts_into_prompt(self):
        """Prepend known user facts to the system prompt so the LLM always has them."""
        facts = self.get_user_facts()
        if not facts:
            return
        facts_lines = "\n".join(f"  {k}: {v}" for k, v in facts.items())
        facts_block = f"\n\n== KNOWN USER FACTS (always keep in mind) ==\n{facts_lines}"
        # Avoid duplicating if already injected
        marker = "== KNOWN USER FACTS"
        base_prompt = ORCHESTRATOR_PROMPT
        if marker in self.system_prompt:
            # Replace existing facts block
            self.system_prompt = base_prompt + facts_block
        else:
            self.system_prompt = self.system_prompt + facts_block

    async def _extract_and_save_facts(self, user_message: str, assistant_response: str):
        """After each exchange, ask the LLM to extract any new durable facts."""
        if self.llm is None:
            return
        exchange = f"USER: {user_message[:600]}\nASSISTANT: {assistant_response[:600]}"
        try:
            raw, _ = await self.llm.complete(
                messages=[{"role": "user", "content": exchange}],
                system=self._FACTS_EXTRACT_PROMPT,
                max_tokens=200,
            )
            import json as _json, re as _re
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            new_facts = _json.loads(clean)
            if not isinstance(new_facts, dict) or not new_facts:
                return
            # Merge with existing facts
            facts = self.get_user_facts()
            facts.update(new_facts)
            self.persist("_user_facts", facts)
            self._inject_user_facts_into_prompt()
            logger.info(f"[{self.name}] User facts updated: {list(new_facts.keys())}")
        except Exception as e:
            logger.debug(f"[{self.name}] Facts extraction skipped: {e}")

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
                    await actor.stop()
                    await self._registry.unregister(actor.actor_id)
                    stopped.append(agent_name)
        del rules[rule_id]
        self.persist(PIPELINE_RULES_KEY, rules)
        task_preview = rule.get("task", "")[:60]
        return f"Rule '{rule_id}' deleted. Stopped agents: {', '.join(stopped) or 'none running'}.\nRule was: {task_preview}"

    async def _restore_spawned_agents(self):
        reg = self._get_spawn_registry()
        if not reg:
            return
        logger.info(f"[{self.name}] Restoring {len(reg)} agent(s): {list(reg.keys())}")
        for name, config in reg.items():
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
        Classify user intent as HA, PIPELINE, or OTHER using a single cheap LLM call.
        Returns one of: 'HA', 'PIPELINE', 'OTHER'
        """
        if not text or text.startswith("/"):
            return "OTHER"
        if self.llm is None:
            return "OTHER"
        try:
            decision, _ = await asyncio.wait_for(
                self.llm.complete(
                    messages=[{"role": "user", "content": text}],
                    system=self.INTENT_CLASSIFIER_PROMPT,
                    max_tokens=4,
                ),
                timeout=5.0,
            )
            token = (decision or "").strip().upper().split()[0] if decision else "OTHER"
            if token in ("HA", "PIPELINE", "OTHER"):
                return token
            return "OTHER"
        except Exception as e:
            logger.debug(f"[{self.name}] Intent classification failed: {e}")
            return "OTHER"

    async def _is_home_automation_request(self, text: str) -> bool:
        # Keep for backward compat — delegates to _classify_intent
        intent = await self._classify_intent(text)
        return intent == "HA"

    # ── User input ─────────────────────────────────────────────────────────

    async def chat(self, user_message: str) -> str:
        response = await super().chat(user_message)
        # Fire-and-forget fact extraction — don't block the response
        asyncio.create_task(self._extract_and_save_facts(user_message, response))
        return response

    async def chat_stream(self, user_message: str):
        full_response = []
        async for chunk in super().chat_stream(user_message):
            if isinstance(chunk, dict):
                yield chunk
            else:
                full_response.append(chunk)
                yield chunk
        # Extract facts from completed response
        if full_response:
            asyncio.create_task(
                self._extract_and_save_facts(user_message, "".join(full_response))
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

        # ── Direct API intercepts — handle without LLM round-trip ──────────
        stripped = text.strip().rstrip("()")
        if stripped in ("main.list_nodes", "list_nodes", "/nodes"):
            nodes = self.list_nodes()
            if not nodes:
                return note_prefix + "No remote nodes seen yet. Deploy one with /deploy <node-name>."
            import time as _t
            lines = []
            for nd in sorted(nodes, key=lambda x: x["node"]):
                status   = "🟢 online" if nd["online"] else "🔴 offline"
                agents   = ", ".join(nd["agents"]) or "(no agents)"
                age      = int(_t.time() - nd["last_seen"])
                lines.append(f"  {nd['node']:22s} {status}  |  agents: {agents}  |  last heartbeat: {age}s ago")
            return note_prefix + "Remote nodes:\n" + "\n".join(lines)

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

        # ── Webhook / notification URL management ───────────────────────────
        if stripped.startswith("/memory"):
            parts = stripped.split(None, 1)
            sub = parts[1].strip() if len(parts) > 1 else ""
            if sub == "clear":
                self.persist("_user_facts", {})
                self.persist("history_summary", "")
                self._history_summary = ""
                self.system_prompt = ORCHESTRATOR_PROMPT
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
            # Default: show memory
            facts = self.get_user_facts()
            summary = self._history_summary
            lines = []
            if facts:
                lines.append(f"User facts ({len(facts)}):")
                for k, v in facts.items():
                    lines.append(f"  {k}: {v}")
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

        if stripped.startswith("/rules"):
            keyword = stripped[14:].strip().lstrip("(").rstrip(")")
            caps = self.list_capabilities(keyword)
            if not caps:
                msg = "No agents found" + (f" matching '{keyword}'" if keyword else "") + "."
                msg += " Agents publish their capabilities on startup."
                return note_prefix + msg
            lines = ["Agent capabilities" + (" matching " + repr(keyword) if keyword else "") + ":"]
            for a in caps:
                lines.append("")
                lines.append("  [" + a["name"] + "]" + (" on " + a["node"] if a.get("node") else ""))
                lines.append("    description : " + a["description"])
                if a["capabilities"]:
                    lines.append("    capabilities: " + ", ".join(a["capabilities"]))
                if a["input_schema"]:
                    lines.append("    input       : " + str(a["input_schema"]))
                if a["output_schema"]:
                    lines.append("    output      : " + str(a["output_schema"]))
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
            return note_prefix + (result or "Planner did not return a result. Please retry.")

        # Single LLM call classifies intent: HA (direct action), PIPELINE (reactive rule), OTHER
        intent = await self._classify_intent(text)
        logger.info(f"[{self.name}] Intent: {intent} — {text[:60]}")

        if intent == "PIPELINE":
            result = await self._run_planner(text)
            return note_prefix + (result or "Planner did not return a result. Please retry.")

        if intent == "HA":
            result = await self.delegate_task("home-assistant-agent", text, timeout=120.0)
            if result and isinstance(result, dict) and result.get("result"):
                return note_prefix + str(result["result"])
            if not result:
                return note_prefix + "I could not reach the Home Assistant agent right now. Please retry."
            return note_prefix + "The Home Assistant agent did not return a result. Please retry."

        response = await self.chat(text)

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

        # Execute any @agent-name {payload} delegation patterns the LLM produced
        clean = await self._execute_llm_delegations(clean)

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "user_interaction", "input": text[:100], "response": clean[:200]},
        )

        if spawned:
            bg_names   = [a.name for a in spawned if isinstance(a, _SpawnPlaceholder)]
            live_names = [a.name for a in spawned if not isinstance(a, _SpawnPlaceholder)]
            parts = []
            if live_names:
                replaced = '"replace": true' in response or '"replace":true' in response
                action   = "Replaced" if replaced else "Spawned"
                parts.append(f"{action} {', '.join(live_names)}")
            if bg_names:
                parts.append(f"Installing packages for {', '.join(bg_names)} — will appear shortly")
            if parts:
                clean += f"\n\n[System: {' | '.join(parts)} — will auto-restore on restart]"

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

        # All slash-commands and direct API intercepts are handled by process_user_input
        # Route them there to avoid duplicating all that logic here
        _stripped = text.strip().rstrip("()")
        _is_command = (
            _stripped.startswith("/")
            or _stripped in ("list_nodes", "main.list_nodes", "rules")
            or _stripped.startswith("@")
        )
        if _is_command:
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
            yield result or "Planner did not return a result. Please retry."
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        # Single LLM call classifies intent: HA, PIPELINE, or OTHER
        intent = await self._classify_intent(text)
        logger.info(f"[{self.name}] Intent: {intent} — {text[:60]}")

        if intent == "PIPELINE":
            result = await self._run_planner(text)
            yield result or "Planner did not return a result. Please retry."
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        if intent == "HA":
            result = await self.delegate_task("home-assistant-agent", text, timeout=120.0)
            if result and isinstance(result, dict) and result.get("result"):
                yield str(result["result"])
            elif not result:
                yield "I could not reach the Home Assistant agent right now. Please retry."
            else:
                yield "The Home Assistant agent did not return a result. Please retry."
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        # Stream the LLM response chunk by chunk
        full_chunks = []
        async for chunk in self.chat_stream(text):
            if isinstance(chunk, dict):
                break   # usage dict — discard, already tracked inside chat_stream
            full_chunks.append(chunk)
            yield chunk

        full_response = "".join(full_chunks)

        # Process any <spawn> blocks in the completed response
        _, spawned = await self._process_spawn_commands(full_response)

        # Execute any @agent-name {payload} delegation patterns the LLM produced
        # If delegations ran, yield the results as an additional chunk
        delegated = await self._execute_llm_delegations(full_response)
        if delegated != full_response:
            # Find what changed and yield just the new parts
            import re as _re
            results = _re.findall(r'[✅❌]\s+\S+.*', delegated)
            if results:
                yield "\n" + "\n".join(results)
        full_response = delegated

        system_msg = ""
        if spawned:
            names      = ", ".join(f"'{a.name}'" for a in spawned if not isinstance(a, _SpawnPlaceholder))
            bg_names   = [a.name for a in spawned if isinstance(a, _SpawnPlaceholder)]
            parts = []
            if names:
                replaced = '"replace": true' in full_response or '"replace":true' in full_response
                parts.append(f"{'Replaced' if replaced else 'Spawned'} {names} — will auto-restore on restart")
            if bg_names:
                parts.append(f"Installing packages for {', '.join(bg_names)} — will appear shortly")
            system_msg = " | ".join(parts)

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

    async def _run_planner(self, task: str) -> Optional[str]:
        """Spawn a PlannerAgent, hand it the task, wait for the result."""
        from .planner_agent import PlannerAgent
        import uuid

        # Enrich vague follow-up tasks with recent conversation context
        # so the planner has the full picture (e.g. which entity was found)
        enriched_task = task
        if self._conversation_history and len(task.split()) < 15:
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
        logger.info(f"[{self.name}] Spawning planner '{planner_name}' for: {enriched_task[:60]}")

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": f"Complex task detected — spawning planner...", "timestamp": __import__('time').time()},
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

        # ── Spawn ──────────────────────────────────────────────────────────────

    async def _execute_llm_delegations(self, response: str) -> str:
        """
        Scan the LLM response for @agent-name {json} delegation patterns and execute them.
        Replaces the pattern in the response with the actual result.

        Matches lines like:
            @doc-to-pptx-agent {"file_path": "...", "output_path": "..."}
            @weather-agent {"city": "Athens"}
        """
        import re

        # Find @agent-name then scan for the matching { } block manually
        # (regex alone can't handle } inside string values reliably)
        delegations = []   # list of (full_match_str, agent_name, payload_dict)

        for m in re.finditer(r'@([\w][\w\-]*)\s+(\{)', response):
            agent_name = m.group(1)
            if agent_name == self.name:
                continue
            start = m.start(2)   # position of opening {
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
            json_str = response[start:end]
            try:
                payload = json.loads(json_str)
            except json.JSONDecodeError:
                continue
            delegations.append((response[m.start():end], agent_name, payload))

        replacements = []
        for full_match, agent_name, payload in delegations:
            # Check if agent is running, if not auto-spawn via catalog
            target = self._registry.find_by_name(agent_name) if self._registry else None
            if not target:
                manifest = self._agent_manifests.get(agent_name, {})
                if manifest.get("spawnable") and manifest.get("catalog"):
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

            if not target:
                replacements.append((full_match, f"[Could not reach {agent_name}]"))
                continue

            json_str = json.dumps(payload)
            logger.info(f"[{self.name}] Executing LLM delegation → @{agent_name} {json_str[:80]}")
            try:
                result = await self.delegate_task(agent_name, json_str, timeout=300.0)
                if result:
                    if isinstance(result, dict):
                        error = result.get("error")
                        if error:
                            result_str = f"❌ {agent_name} failed: {error}"
                        else:
                            for key in ("pptx_path", "image_path", "result", "message", "output", "text"):
                                if result.get(key):
                                    result_str = f"✅ {agent_name} completed: {key}={result[key]}"
                                    break
                            else:
                                result_str = f"✅ {agent_name} completed: {result}"
                    else:
                        result_str = f"✅ {agent_name}: {result}"
                else:
                    result_str = f"[{agent_name} did not respond]"
            except Exception as e:
                result_str = f"[{agent_name} error: {e}]"

            replacements.append((full_match, result_str))

        # Apply replacements
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
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"[{self.name}] Error stopping old '{name}': {e}")

        agent_type    = config.get("type", "dynamic")
        code          = config.get("code", "").strip()
        system_prompt = config.get("system_prompt", "").strip()

        # Route to the right agent class
        if agent_type == "ha_actuator":
            actor = await self._spawn_ha_actuator(config, name)
        elif agent_type == "manual" or name == "manual-agent":
            actor = await self._spawn_manual_agent(config, name)
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

    async def _spawn_manual_agent(self, config: dict, name: str):
        """Spawn the pre-defined ManualAgent — robust PDF manual search and Q&A."""
        from .manual_agent import ManualAgent
        logger.info(f"[{self.name}] Spawning ManualAgent '{name}'")
        actor = await self.spawn(
            ManualAgent,
            name=name,
            llm_provider=self.llm,
            persistence_dir=str(self._persistence_dir.parent),
        )
        return actor

    async def _spawn_llm_agent(self, config: dict, name: str):
        """Spawn a proper LLMAgent — best for chat, Q&A, reasoning tasks."""
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
            # Install and spawn in a background task so we don't block the user
            logger.info(f"[{self.name}] Scheduling background install+spawn for '{name}': {packages}")
            asyncio.create_task(self._install_then_spawn(config, name, code, packages))
            # Return a placeholder so the caller knows spawn is in progress
            return _SpawnPlaceholder(name)
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
        )
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

    async def _spawn_remote(self, config: dict, node: str, save: bool) -> None:
        """
        Publish a spawn command to a remote node via MQTT.
        The remote_runner.py on that machine will receive it and run the agent.
        Remote agents appear in the dashboard exactly like local ones
        because they connect to the same MQTT broker.

        Also updates nodes/{node}/desired_state (retained) with ALL agents for
        this node so the runner can self-heal after a reboot.
        """
        name = config.get("name", "remote-agent")
        logger.info(f"[{self.name}] Spawning '{name}' on remote node '{node}'")

        # Publish individual spawn (for immediate delivery)
        await self._mqtt_publish(
            f"nodes/{node}/spawn",
            config,
            retain=True,
            qos=1,
        )

        # Update desired state for the whole node (retained — survives Pi reboot)
        await self._update_node_desired_state(node, config)

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "spawned", "message": f"Spawned '{name}' on node '{node}'",
             "child_name": name, "node": node, "timestamp": __import__("time").time()}
        )

        if save:
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

        await self._mqtt_publish(
            f"nodes/{node}/desired_state",
            {"node": node, "agents": list(agents.values()),
             "timestamp": __import__("time").time()},
            retain=True,
            qos=1,
        )
        logger.info(f"[{self.name}] Desired state for '{node}': {list(agents.keys())}")

    # ── Node registry ──────────────────────────────────────────────────────

    def list_nodes(self) -> list[dict]:
        """Return all known remote nodes with their last-seen time and running agents."""
        import time as _time
        now = _time.time()
        return [
            {
                "node":      name,
                "agents":    info.get("agents", []),
                "last_seen": info.get("last_seen", 0),
                "online":    (now - info.get("last_seen", 0)) < 30,
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

        Example:
            list_capabilities()            → all agents
            list_capabilities("weather")   → agents with "weather" in description/capabilities
        """
        results = []
        kw = keyword.lower().strip()
        # Support multi-word keywords — match if ANY word appears in the haystack
        kw_words = kw.split() if kw else []
        for name, manifest in self._agent_manifests.items():
            desc  = manifest.get("description", "")
            caps  = manifest.get("capabilities", [])
            # Filter by keyword across description, capabilities, and name
            if kw_words:
                haystack = desc.lower() + " " + " ".join(caps).lower() + " " + name.lower()
                if not any(w in haystack for w in kw_words):
                    continue
            results.append({
                "name":          name,
                "node":          manifest.get("node"),
                "description":   desc,
                "capabilities":  caps,
                "input_schema":  manifest.get("input_schema",  {}),
                "output_schema": manifest.get("output_schema", {}),
                "spawnable":     manifest.get("spawnable", False),
                "running":       bool(self._registry and self._registry.find_by_name(name)),
            })
        return sorted(results, key=lambda x: x["name"])

    async def _manifest_listener(self):
        """
        Subscribe to agents/+/manifest and build a searchable topic registry.
        Retained manifests are delivered immediately on subscribe so the registry
        is populated even for agents that started before main restarted.
        """
        try:
            import aiomqtt
        except ImportError:
            return

        while self.state.value not in ("stopped", "failed"):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe("agents/+/manifest")
                    logger.info("[main] Subscribed to agent manifests.")
                    async for msg in client.messages:
                        try:
                            data = json.loads(msg.payload.decode())
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
                        logger.debug(f"[main] Manifest from '{agent_name}': {published}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.state.value not in ("stopped", "failed"):
                    logger.warning(f"[main] Manifest listener error: {e}. Reconnecting in 5s…")
                    await asyncio.sleep(5)

    async def migrate_agent(self, agent_name: str, target_node: str) -> dict:
        """
        Move a running agent to a different node.

        If the agent is local: saves updated config (with new node) and re-spawns remotely.
        If the agent is remote: publishes a migrate command to its current node.
        Returns {"success": bool, "message": str}
        """
        import time as _time

        reg = self._get_spawn_registry()
        config = reg.get(agent_name)
        if not config:
            return {"success": False, "message": f"Agent '{agent_name}' not in spawn registry."}

        current_node = config.get("node", "").strip()

        if current_node == target_node:
            return {"success": False, "message": f"Agent '{agent_name}' is already on '{target_node}'."}

        if current_node:
            # ── Remote → Remote migration ────────────────────────────────────
            logger.info(f"[{self.name}] Migrating '{agent_name}' from node '{current_node}' → '{target_node}'")
            await self._mqtt_publish(
                f"nodes/{current_node}/migrate",
                {"name": agent_name, "target_node": target_node},
            )
        else:
            # ── Local → Remote migration ─────────────────────────────────────
            logger.info(f"[{self.name}] Migrating LOCAL agent '{agent_name}' → remote node '{target_node}'")

            # Stop the local instance
            if self._registry:
                local = self._registry.find_by_name(agent_name)
                if local:
                    try:
                        await self._registry.unregister(local.actor_id)
                        await local.stop()
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.warning(f"[{self.name}] Could not stop local '{agent_name}': {e}")

            # Update config with new node target and re-spawn remotely
            new_config = dict(config)
            new_config["node"] = target_node
            new_config.pop("replace", None)

            await self._spawn_remote(new_config, target_node, save=True)

        # Update spawn registry so next restart re-spawns to the right node
        updated = dict(config)
        updated["node"] = target_node
        self._save_to_spawn_registry(updated)

        msg = (f"Migrating '{agent_name}' from '{current_node or 'local'}' "
               f"→ '{target_node}'. It will appear in the dashboard shortly.")
        logger.info(f"[{self.name}] {msg}")
        return {"success": True, "message": msg}

    async def _node_heartbeat_listener(self):
        """
        Subscribe to nodes/+/heartbeat so main knows which remote nodes are online.
        Updates self._known_nodes which is used by list_nodes() and the LLM context.
        """
        try:
            import aiomqtt
        except ImportError:
            logger.warning("[main] aiomqtt not available — node heartbeat tracking disabled.")
            return

        while self.state.value not in ("stopped", "failed"):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe("nodes/+/heartbeat")
                    await client.subscribe("nodes/+/migrate_result")
                    logger.info("[main] Subscribed to node heartbeats.")
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
                            self._known_nodes[node_name] = {
                                "last_seen": _t.time(),
                                "agents":   data.get("agents", []),
                                "node_id":  data.get("node_id", ""),
                            }
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
                    logger.warning(f"[main] Node heartbeat listener error: {e}. Reconnecting in 5s…")
                    await asyncio.sleep(5)

    # ── Delegation ─────────────────────────────────────────────────────────

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
        if not self._registry:
            return None
        target = self._registry.find_by_name(target_name)
        if not target:
            return None
        future = asyncio.get_event_loop().create_future()
        self._result_futures[task] = future
        await self.send(target.actor_id, MessageType.TASK, {"text": task, "reply_to": self.actor_id})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._result_futures.pop(task, None)

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
        # Find node before removing from registry
        reg = self._get_spawn_registry()
        node = reg.get(name, {}).get("node", "").strip()

        self._remove_from_spawn_registry(name)

        # Update desired state so Pi doesn't re-spawn on reconcile
        if node:
            await self._update_node_desired_state(node, remove_name=name)
            await self._mqtt_publish(f"nodes/{node}/stop", {"name": name}, qos=1)

        if self._registry:
            target = self._registry.find_by_name(name)
            if target:
                await self._registry.unregister(target.actor_id)
                await target.stop()