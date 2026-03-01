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

SPAWN_REGISTRY_KEY = "_spawned_agents"

ORCHESTRATOR_PROMPT = """You are the main orchestrator in a multi-agent system.

You can spawn new agents on demand. When the user asks for an agent, you:
1. Write the Python code for its core logic
2. Wrap it in a <spawn> block

== SPAWN FORMAT ==
<spawn>
{
  "name": "agent-name",
  "description": "what this agent does",
  "poll_interval": 1.0,
  "code": "PYTHON CODE HERE"
}
</spawn>

== CODE STRUCTURE ==
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
  agent.send_to(agent_name, payload)  — send task to another agent

== RULES ==
- Always import libraries INSIDE functions (not at module level)
- Use agent.state to pass data between setup() and process()
- Keep process() non-blocking — use asyncio.sleep() for waits
- For blocking operations (cv2, torch inference) wrap in:
    import asyncio
    result = await asyncio.get_event_loop().run_in_executor(None, blocking_fn)

== EXISTING AGENTS ==
- main    : you (orchestrator)
- monitor : health monitoring

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

    def __init__(self, llm_provider: Optional[LLMProvider] = None, **kwargs):
        kwargs.setdefault("name", "main")
        kwargs.setdefault("system_prompt", ORCHESTRATOR_PROMPT)
        super().__init__(llm_provider=llm_provider, **kwargs)
        self._result_futures: dict[str, asyncio.Future] = {}
        self.protected = True

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        await super().on_start()
        await self._restore_spawned_agents()

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
            await self._handle_task(msg)
        elif msg.type == MessageType.RESULT:
            fid = msg.payload.get("task") if isinstance(msg.payload, dict) else None
            if fid and fid in self._result_futures:
                self._result_futures[fid].set_result(msg.payload)

    # ── User input ─────────────────────────────────────────────────────────

    async def process_user_input(self, text: str) -> str:
        response = await self.chat(text)
        clean, spawned = await self._process_spawn_commands(response)

        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "user_interaction", "input": text[:100], "response": clean[:200]},
        )

        if spawned:
            names = ", ".join(f"'{a.name}'" for a in spawned)
            clean += f"\n\n[System: Spawned {names} — will auto-restore on restart]"

        return clean

    # ── Spawn ──────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_spawn_config(raw: str) -> dict:
        """
        Robustly parse a spawn config block that may contain raw multiline
        code strings — which break json.loads due to unescaped newlines.
        """
        raw = raw.strip()

        # Strategy 1: standard JSON (works when LLM escapes newlines as \\n)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Strategy 2: pull the code block out manually, parse everything else
        # Match "code": "..." where ... may span multiple lines
        code_match = re.search(r'"code"\s*:\s*"(.*?)"\s*\n?\s*[}\]]', raw, re.DOTALL)
        if code_match:
            code_raw   = code_match.group(1)
            # Replace the raw code block with a safe placeholder
            placeholder = raw[:code_match.start()] + '"code": "__CODE__"' + raw[code_match.end()-1:]
            config      = json.loads(placeholder)
            # Restore code — unescape anything the LLM escaped
            config["code"] = (code_raw
                              .replace("\\n", "\n")
                              .replace('\\"', '"')
                              .replace("\\t", "\t"))
            return config

        # Strategy 3: the code value uses single-quotes or backticks — rare but handle it
        code_match2 = re.search(r'"code"\s*:\s*`(.*?)`', raw, re.DOTALL)
        if code_match2:
            code_raw    = code_match2.group(1)
            placeholder = re.sub(r'"code"\s*:\s*`.*?`', '"code": "__CODE__"', raw, flags=re.DOTALL)
            config      = json.loads(placeholder)
            config["code"] = code_raw
            return config

        raise ValueError(f"Cannot parse spawn config:\n{raw[:200]}")

    async def _process_spawn_commands(self, response: str):
        spawned = []
        pattern = r'<spawn>(.*?)</spawn>'

        for match in re.findall(pattern, response, re.DOTALL):
            try:
                config = self._parse_spawn_config(match.strip())
                if not config.get("code", "").strip():
                    logger.error(f"[{self.name}] Spawn config has no code: {config.get('name')}")
                    continue
                actor = await self._spawn_from_config(config, save=True)
                if actor:
                    spawned.append(actor)
            except Exception as e:
                logger.error(f"[{self.name}] Spawn failed: {e}\n{match[:300]}")

        clean = re.sub(pattern, '', response, flags=re.DOTALL).strip()
        return clean, spawned

    async def _spawn_from_config(self, config: dict, save: bool = True) -> Optional[Actor]:
        from .dynamic_agent import DynamicAgent

        name = config.get("name", "dynamic-agent")

        # Don't duplicate
        if self._registry and self._registry.find_by_name(name):
            logger.info(f"[{self.name}] '{name}' already exists.")
            return self._registry.find_by_name(name)

        code = config.get("code", "")
        if not code.strip():
            logger.warning(f"[{self.name}] Spawn config for '{name}' has no code.")
            return None

        actor = await self.spawn(
            DynamicAgent,
            name=name,
            code=code,
            poll_interval=float(config.get("poll_interval", 1.0)),
            description=config.get("description", ""),
            persistence_dir=str(self._persistence_dir.parent),
        )

        if actor and save:
            self._save_to_spawn_registry(config)

        return actor

    # ── Delegation ─────────────────────────────────────────────────────────

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