"""
MainActor - Primary conversational agent and orchestrator.
Spawns DynamicAgents whose core logic is written by the LLM on the fly.
"""

import asyncio
import logging
import json
import re
from typing import Optional

from ..core.actor import Actor, Message, MessageType, ActorState
from .llm_agent import LLMAgent, LLMProvider

logger = logging.getLogger(__name__)

class _SpawnPlaceholder:
    """Returned when an agent is being installed+spawned in the background."""
    def __init__(self, name: str):
        self.name = name



SPAWN_REGISTRY_KEY = "_spawned_agents"
NODE_REGISTRY_KEY  = "_known_nodes"       # tracks online remote nodes

ORCHESTRATOR_PROMPT = """You are the main orchestrator in a multi-agent system.

You can spawn new agents on demand. When the user asks for an agent, you:
1. Write the Python code for its core logic
2. Wrap it in a <spawn> block

== SPAWN FORMAT ==
There are TWO types of agents you can spawn:

--- TYPE 0: Manual Agent (for finding device manuals and answering questions from them) ---
Use when the user wants to look up a device manual and ask questions about it.
No code needed — this is a pre-built agent.

<spawn>
{
  "name": "manual-agent",
  "type": "manual",
  "description": "Finds device manuals online and answers questions from them"
}
</spawn>

--- TYPE 1: LLM Agent (for conversation, Q&A, reasoning, explanation) ---
Use when the agent's job is to respond to messages using language understanding.
No "code" field needed — just provide a system prompt.

<spawn>
{
  "name": "agent-name",
  "type": "llm",
  "description": "what this agent does",
  "system_prompt": "You are a helpful assistant specialized in ..."
}
</spawn>

--- TYPE 2: Dynamic Agent (for data pipelines, sensors, MQTT, APIs, tools) ---
Use when the agent needs to run custom Python logic (webcam, serial port, timers, etc.)
Provide a "code" field with the Python functions.

<spawn>
{
  "name": "agent-name",
  "type": "dynamic",
  "description": "what this agent does",
  "poll_interval": 1.0,
  "code": "PYTHON CODE HERE"
}
</spawn>

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
  agent.send_to(agent_name, payload)          — send task to agent, wait for result (60s timeout)
  agent.send_to_many([(name, payload), ...])  — send to multiple agents IN PARALLEL, returns list

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
  "description": "Reads temperature from DHT22 sensor on the kitchen Pi",
  "poll_interval": 30,
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
To see which remote nodes are currently online:
  nodes = main.list_nodes()
  # Returns: [{"node": "rpi-kitchen", "agents": ["temp-sensor"], "online": True, "last_seen": ...}]

Use this before spawning to verify the target node is reachable.
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
  "description": "Manages remote node deployment via SSH",
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


== EXAMPLE — Webcam YOLO agent ==
<spawn>
{
  "name": "yolo-agent",
  "description": "Webcam YOLO object detection, publishes detections to MQTT",
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

    HOME_AUTOMATION_INTENT_SYSTEM_PROMPT = (
        "You are an intent classifier for Home Assistant routing. "
        "Respond with exactly one token: HA or NOT_HA.\n"
        "HA = user is asking to automate/control a physical home environment, devices, scenes, "
        "sensors, routines, or events (lights, doors, climate, presence, ambiance, alerts, etc.), "
        "including natural-language goals that imply automation.\n"
        "NOT_HA = anything else (coding, general chat, pure web/software tasks, unrelated requests)."
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

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        await super().on_start()
        await self._restore_spawned_agents()
        # Listen for remote node heartbeats so we know what's online
        self._tasks.append(asyncio.create_task(self._node_heartbeat_listener()))

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

    async def _is_home_automation_request(self, text: str) -> bool:
        if self._looks_like_home_automation_request(text):
            return True
        if not text or text.lower().startswith("spawn ") or text.startswith("/"):
            return False
        if self.llm is None:
            return False
        try:
            decision_task = self.llm.complete(
                messages=[{"role": "user", "content": text}],
                system=self.HOME_AUTOMATION_INTENT_SYSTEM_PROMPT,
                max_tokens=4,
            )
            decision, _ = await asyncio.wait_for(decision_task, timeout=4.0)
            return (decision or "").strip().upper().startswith("HA")
        except Exception as e:
            logger.debug(f"[{self.name}] HA intent fallback failed: {e}")
            return False

    # ── User input ─────────────────────────────────────────────────────────

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

        # Route home automation requests to the unified HA agent
        if await self._is_home_automation_request(text):
            result = await self.delegate_task("home-assistant-agent", text, timeout=120.0)
            if result and isinstance(result, dict) and result.get("result"):
                return note_prefix + str(result["result"])
            if not result:
                return note_prefix + "I could not reach the Home Assistant agent right now. Please retry."
            return note_prefix + "The Home Assistant agent did not return a result. Please retry."

        # Detect complex multi-agent tasks and route to PlannerAgent
        if await self._needs_planning(text):
            result = await self._run_planner(text)
            if result:
                return note_prefix + result

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

        # HA routing has no streaming — fall back to blocking for those
        if await self._is_home_automation_request(text):
            result = await self.delegate_task("home-assistant-agent", text, timeout=120.0)
            if result and isinstance(result, dict) and result.get("result"):
                yield str(result["result"])
            elif not result:
                yield "I could not reach the Home Assistant agent right now. Please retry."
            else:
                yield "The Home Assistant agent did not return a result. Please retry."
            yield {"done": True, "spawned": [], "system_msg": ""}
            return

        # Detect complex multi-agent tasks and route to PlannerAgent
        if await self._needs_planning(text):
            result = await self._run_planner(text)
            if result:
                yield result
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
    ]

    async def _needs_planning(self, text: str) -> bool:
        """
        Heuristic: does this task benefit from multi-agent coordination?
        Keeps main fast — only escalates genuinely complex requests.
        """
        import re
        lowered = text.lower()

        # Explicit user request for coordination
        if any(w in lowered for w in ("coordinate:", "plan:", "pipeline:", "@planner")):
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

        planner_name = f"planner-{uuid.uuid4().hex[:6]}"
        logger.info(f"[{self.name}] Spawning planner '{planner_name}' for: {task[:60]}")

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": f"Complex task detected — spawning planner...", "timestamp": __import__('time').time()},
        )

        task_id = f"plan_{uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._result_futures[task_id] = future

        try:
            planner = await self.spawn(
                PlannerAgent,
                name=planner_name,
                llm_provider=self.llm,
                task=task,
                reply_to_id=self.actor_id,
                reply_task_id=task_id,   # so main can match the result future
                auto_terminate=True,
                persistence_dir=str(self._persistence_dir.parent),
            )
            if not planner:
                return None

            # Planner will call on_start → _run_plan → send RESULT back to us
            # We wait here with a generous timeout
            result_payload = await asyncio.wait_for(future, timeout=120.0)
            answer = result_payload.get("result") or result_payload.get("text") or ""
            # Surface any agents the planner spawned
            spawned_names = result_payload.get("spawned", [])
            if spawned_names:
                answer += f"\n\n[System: Planner created new agents: {', '.join(spawned_names)} — saved for future use]"
            return answer

        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Planner timed out for: {task[:60]}")
            return None
        except Exception as e:
            logger.error(f"[{self.name}] Planner error: {e}")
            return None
        finally:
            self._result_futures.pop(task_id, None)

        # ── Spawn ──────────────────────────────────────────────────────────────

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
        if agent_type == "manual" or name == "manual-agent":
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
        """
        name = config.get("name", "remote-agent")
        logger.info(f"[{self.name}] Spawning '{name}' on remote node '{node}'")

        await self._mqtt_publish(
            f"nodes/{node}/spawn",
            config,
        )

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "spawned", "message": f"Spawned '{name}' on node '{node}'",
             "child_name": name, "node": node, "timestamp": __import__("time").time()}
        )

        if save:
            self._save_to_spawn_registry(config)

        # Return None — remote actors don't have a local Python object
        # but they will appear in the dashboard via MQTT heartbeats
        return None

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
        self._remove_from_spawn_registry(name)
        if self._registry:
            target = self._registry.find_by_name(name)
            if target:
                await self._registry.unregister(target.actor_id)
                await target.stop()