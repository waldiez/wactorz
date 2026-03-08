#!/usr/bin/env python3
"""
remote_runner.py — AgentFlow edge node runner.

Deploy this single file to any machine (Raspberry Pi, VM, edge device).
It connects to the shared MQTT broker, listens for spawn commands from main,
and runs DynamicAgents locally. Those agents heartbeat back to the same broker
so they appear in the central dashboard exactly like local agents.

Usage on the remote machine:
    pip install aiomqtt psutil aiohttp --break-system-packages
    python3 remote_runner.py --broker 192.168.1.10 --name rpi-livingroom

From the main AgentFlow chat (automatic via devops-agent):
    "deploy node rpi-livingroom to pi@192.168.1.50 with broker 192.168.1.10"

Or manually in the chat spawn block:
    <spawn>
    {
      "name": "temp-sensor-agent",
      "node": "rpi-livingroom",
      "type": "dynamic",
      "description": "Reads temperature from DHT22 sensor",
      "poll_interval": 30,
      "code": "
        async def setup(agent):
            await agent.log('DHT22 sensor agent ready')

        async def process(agent):
            import random   # replace with real adafruit_dht read
            temp = round(20 + random.uniform(-2, 2), 1)
            await agent.publish('sensors/temperature', {'value': temp, 'unit': 'C'})
            await agent.log(f'Temperature: {temp}C')
      "
    }
    </spawn>

Architecture:
    [Main machine]                    [Raspberry Pi / Edge node]
    main_actor ──MQTT──► nodes/{name}/spawn ──► remote_runner.py
                                                  │ compiles + runs DynamicAgent
                                                  │ heartbeats via MQTT
    dashboard  ◄──MQTT── agents/{id}/heartbeat ◄──┘

The remote runner is intentionally self-contained — it reimplements just enough
of the Actor/DynamicAgent contract to run user code without needing the full
agentflow package installed on the edge device.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import traceback
import uuid
from typing import Any, Optional

logger = logging.getLogger("remote_runner")


# ── Minimal Actor API exposed to generated code ───────────────────────────────

class _RemoteAgentAPI:
    """
    Mirrors the agent API that DynamicAgent provides to generated code.
    All methods that touch MQTT go through the shared client.
    """

    def __init__(self, agent: "_RemoteAgent"):
        self._agent = agent

    # ── Identity ──────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:         return self._agent.name
    @property
    def actor_id(self) -> str:     return self._agent.actor_id
    @property
    def state(self) -> dict:       return self._agent._state
    @property
    def node(self) -> str:         return self._agent.node_name

    # ── MQTT ──────────────────────────────────────────────────────────────────
    async def publish(self, topic: str, data: Any):
        await self._agent._publish(topic, data)

    # ── Logging ───────────────────────────────────────────────────────────────
    async def log(self, message: str):
        logger.info(f"[{self.name}] {message}")
        await self._agent._publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": message,
             "agent": self.name, "timestamp": time.time()},
        )

    async def alert(self, message: str, severity: str = "warning"):
        logger.warning(f"[{self.name}] ALERT({severity}): {message}")
        await self._agent._publish(
            f"agents/{self.actor_id}/alert",
            {"message": message, "severity": severity,
             "agent": self.name, "timestamp": time.time()},
        )

    # ── Persistence ───────────────────────────────────────────────────────────
    def persist(self, key: str, value: Any):
        self._agent._persistent_state[key] = value
        self._agent._save_state()

    def recall(self, key: str, default: Any = None) -> Any:
        return self._agent._persistent_state.get(key, default)

    # ── Agent-to-agent (via MQTT request/response) ────────────────────────────
    async def send_to(self, agent_name: str, payload: Any, timeout: float = 30.0) -> Any:
        """
        Send a task to any agent (local or remote) via MQTT and wait for reply.
        Uses a reply-to topic unique to this call so responses can be correlated.
        """
        reply_topic = f"nodes/{self._agent.node_name}/reply/{uuid.uuid4().hex[:8]}"
        request = {
            "_remote_task": True,
            "_reply_topic": reply_topic,
            "payload": payload,
        }
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._agent._pending_replies[reply_topic] = future

        await self._agent._publish(f"agents/by-name/{agent_name}/task", request)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] send_to '{agent_name}' timed out")
            return None
        finally:
            self._agent._pending_replies.pop(reply_topic, None)

    # Alias used in DynamicAgent code
    async def delegate(self, agent_name: str, payload: Any, timeout: float = 30.0) -> Any:
        return await self.send_to(agent_name, payload, timeout)

    def agents(self) -> list:
        """Return list of known agents on this node."""
        return [
            {"name": a.name, "actor_id": a.actor_id, "node": a.node_name}
            for a in self._agent._runner._agents.values()
        ]


# ── Remote agent (lightweight DynamicAgent equivalent) ───────────────────────

class _RemoteAgent:
    """
    Lightweight equivalent of DynamicAgent that runs on the edge node.
    Holds compiled user code and drives setup/process/handle_task.
    """

    def __init__(self, config: dict, runner: "_RemoteRunner"):
        self.name       = config.get("name", f"remote-agent-{uuid.uuid4().hex[:6]}")
        self.actor_id   = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agentflow.actor.{self.name}"))
        self.node_name  = runner.node_name
        self._runner    = runner
        self._config    = config
        self._code      = config.get("code", "")
        self._poll      = float(config.get("poll_interval", 5.0))
        self._ns: dict  = {}               # shared namespace for user code
        self._state: dict = {}             # agent.state dict for user code
        self._persistent_state: dict = {}
        self._state_path = f"/tmp/agentflow_{self.name}_state.json"
        self._pending_replies: dict[str, asyncio.Future] = {}
        self._api       = _RemoteAgentAPI(self)
        self._tasks:    list[asyncio.Task] = []
        self._running   = False

        self._fn_setup       = None
        self._fn_process     = None
        self._fn_handle_task = None

        self._load_state()

    # ── State persistence (JSON, not pickle — portable across Python versions) ─

    def _save_state(self):
        try:
            with open(self._state_path, "w") as f:
                json.dump(self._persistent_state, f)
        except Exception as e:
            logger.warning(f"[{self.name}] State save failed: {e}")

    def _load_state(self):
        if os.path.exists(self._state_path):
            try:
                with open(self._state_path) as f:
                    self._persistent_state = json.load(f)
                logger.info(f"[{self.name}] Loaded persistent state.")
            except Exception:
                pass

    # ── MQTT publish helper ───────────────────────────────────────────────────

    async def _publish(self, topic: str, data: Any):
        await self._runner.publish(topic, data)

    # ── Code compilation ──────────────────────────────────────────────────────

    def _compile(self) -> Optional[str]:
        """Compile user code into self._ns. Returns error string or None."""
        try:
            exec(compile(self._code, f"<{self.name}>", "exec"), self._ns)
            self._fn_setup       = self._ns.get("setup")
            self._fn_process     = self._ns.get("process")
            self._fn_handle_task = self._ns.get("handle_task")
            return None
        except Exception as e:
            return f"Compile error: {e}\n{traceback.format_exc()}"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        err = self._compile()
        if err:
            logger.error(f"[{self.name}] {err}")
            await self._publish(
                f"agents/{self.actor_id}/errors",
                {"phase": "compile", "severity": "fatal",
                 "error": err, "agent": self.name, "timestamp": time.time()},
            )
            return

        await self._publish_heartbeat("running")

        if self._fn_setup:
            try:
                await self._fn_setup(self._api)
                logger.info(f"[{self.name}] setup() completed.")
            except Exception as e:
                err_str = traceback.format_exc()
                logger.error(f"[{self.name}] setup() failed: {e}")
                await self._publish(
                    f"agents/{self.actor_id}/errors",
                    {"phase": "setup", "severity": "fatal",
                     "error": str(e), "traceback": err_str,
                     "agent": self.name, "timestamp": time.time()},
                )

        if self._fn_process:
            self._tasks.append(asyncio.create_task(self._process_loop()))

        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))

    async def stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._save_state()
        await self._publish_heartbeat("stopped")
        logger.info(f"[{self.name}] Stopped.")

    # ── Loops ─────────────────────────────────────────────────────────────────

    async def _process_loop(self):
        consecutive_errors = 0
        while self._running:
            try:
                await self._fn_process(self._api)
                consecutive_errors = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                err_str = traceback.format_exc()
                logger.error(f"[{self.name}] process() error #{consecutive_errors}: {e}")
                await self._publish(
                    f"agents/{self.actor_id}/errors",
                    {"phase": "process", "severity": "critical" if consecutive_errors >= 3 else "warning",
                     "error": str(e), "consecutive": consecutive_errors,
                     "agent": self.name, "timestamp": time.time()},
                )
                # Exponential backoff
                await asyncio.sleep(min(2 ** consecutive_errors, 30))
                continue
            try:
                await asyncio.sleep(self._poll)
            except asyncio.CancelledError:
                break

    async def _heartbeat_loop(self, interval: float = 10.0):
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self._publish_heartbeat("running")
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _publish_heartbeat(self, state: str):
        await self._publish(
            f"agents/{self.actor_id}/heartbeat",
            {
                "actor_id":  self.actor_id,
                "name":      self.name,
                "timestamp": time.time(),
                "state":     state,
                "node":      self.node_name,   # extra field — shows in dashboard
                "cpu":       0.0,
                "memory_mb": 0.0,
                "task":      "running" if state == "running" else state,
                "protected": False,
            },
        )

    # ── Task handling ─────────────────────────────────────────────────────────

    async def handle_task(self, payload: dict) -> Any:
        if not self._fn_handle_task:
            return {"error": f"Agent '{self.name}' has no handle_task function."}
        try:
            result = await self._fn_handle_task(self._api, payload)
            return result or {}
        except Exception as e:
            err_str = traceback.format_exc()
            logger.error(f"[{self.name}] handle_task() error: {e}")
            await self._publish(
                f"agents/{self.actor_id}/errors",
                {"phase": "handle_task", "severity": "warning",
                 "error": str(e), "traceback": err_str,
                 "agent": self.name, "timestamp": time.time()},
            )
            return {"error": str(e), "error_phase": "handle_task", "agent": self.name}

    def deliver_reply(self, reply_topic: str, data: Any):
        """Called by runner when an inbound reply arrives for this agent."""
        fut = self._pending_replies.get(reply_topic)
        if fut and not fut.done():
            fut.set_result(data)


# ── Remote runner (the process that lives on the Pi) ─────────────────────────

class _RemoteRunner:
    """
    The long-running process on the edge node.
    Connects to the MQTT broker, listens for spawn commands, manages agents.
    """

    def __init__(self, broker: str, port: int, node_name: str):
        self.broker     = broker
        self.port       = port
        self.node_name  = node_name
        self._agents:   dict[str, _RemoteAgent] = {}   # name → agent
        self._pub_queue: asyncio.Queue = asyncio.Queue()
        self._running   = False

    # ── MQTT publish (queue-based, reconnect-safe) ────────────────────────────

    async def publish(self, topic: str, data: Any):
        payload = json.dumps(data) if not isinstance(data, str) else data
        await self._pub_queue.put((topic, payload))

    # ── Spawn / stop agents ───────────────────────────────────────────────────

    async def spawn_agent(self, config: dict):
        name = config.get("name", f"agent-{uuid.uuid4().hex[:6]}")
        if name in self._agents:
            if config.get("replace", False):
                logger.info(f"[runner] Replacing agent '{name}'")
                await self.stop_agent(name)
            else:
                logger.info(f"[runner] Agent '{name}' already running (use replace=true)")
                return

        packages = config.get("install", [])
        if packages:
            await self._install_packages(packages)

        agent = _RemoteAgent(config, self)
        self._agents[name] = agent
        await agent.start()
        logger.info(f"[runner] Agent '{name}' started.")

        await self.publish(
            f"agents/{self.node_name}/logs",
            {"type": "spawned", "message": f"Remote agent '{name}' started on {self.node_name}",
             "child_name": name, "node": self.node_name, "timestamp": time.time()},
        )

    async def stop_agent(self, name: str):
        agent = self._agents.pop(name, None)
        if agent:
            await agent.stop()

    async def stop_all(self):
        for name in list(self._agents):
            await self.stop_agent(name)

    async def _install_packages(self, packages: list):
        """Install pip packages on the edge node."""
        import subprocess
        pkgs = " ".join(packages)
        logger.info(f"[runner] Installing: {pkgs}")
        proc = await asyncio.create_subprocess_shell(
            f"pip install {pkgs} --break-system-packages -q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(f"[runner] pip install warning: {stderr.decode()[:200]}")

    # ── Status heartbeat for the node itself ──────────────────────────────────

    async def _node_heartbeat_loop(self, interval: float = 15.0):
        """Publish a heartbeat for the runner process itself so it appears in dashboard."""
        node_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agentflow.node.{self.node_name}"))
        while self._running:
            try:
                await asyncio.sleep(interval)
                agent_names = list(self._agents.keys())
                await self.publish(
                    f"nodes/{self.node_name}/heartbeat",
                    {
                        "node":       self.node_name,
                        "node_id":    node_id,
                        "timestamp":  time.time(),
                        "agents":     agent_names,
                        "agent_count": len(agent_names),
                        "broker":     self.broker,
                    },
                )
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ── MQTT publisher task (reconnect-safe) ──────────────────────────────────

    async def _publisher_loop(self):
        import aiomqtt
        while self._running:
            try:
                async with aiomqtt.Client(self.broker, self.port) as client:
                    logger.info(f"[runner] Publisher connected to {self.broker}:{self.port}")
                    while self._running:
                        topic, payload = await self._pub_queue.get()
                        await client.publish(topic, payload)
                        self._pub_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[runner] Publisher disconnected: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3)

    # ── MQTT subscriber task ──────────────────────────────────────────────────

    async def _subscriber_loop(self):
        """
        Subscribes to:
          nodes/{node_name}/spawn          — spawn a new agent
          nodes/{node_name}/stop           — stop a named agent
          nodes/{node_name}/stop_all       — stop all agents and shut down
          nodes/{node_name}/list           — publish list of running agents
          nodes/{node_name}/reply/#        — route replies back to waiting agents
          agents/by-name/+/task           — task addressed to a named agent
        """
        import aiomqtt
        topics = [
            f"nodes/{self.node_name}/spawn",
            f"nodes/{self.node_name}/stop",
            f"nodes/{self.node_name}/stop_all",
            f"nodes/{self.node_name}/migrate",
            f"nodes/{self.node_name}/list",
            f"nodes/{self.node_name}/reply/#",
            "agents/by-name/+/task",
        ]

        while self._running:
            try:
                async with aiomqtt.Client(self.broker, self.port) as client:
                    for topic in topics:
                        await client.subscribe(topic)
                    logger.info(f"[runner] Subscribed to control topics on node '{self.node_name}'")

                    async for msg in client.messages:
                        topic_str = str(msg.topic)
                        try:
                            data = json.loads(msg.payload.decode())
                        except Exception:
                            data = msg.payload.decode()

                        if topic_str == f"nodes/{self.node_name}/spawn":
                            asyncio.create_task(self.spawn_agent(data))

                        elif topic_str == f"nodes/{self.node_name}/stop":
                            name = data.get("name") if isinstance(data, dict) else str(data)
                            asyncio.create_task(self.stop_agent(name))

                        elif topic_str == f"nodes/{self.node_name}/migrate":
                            # Migrate a running agent to another node
                            # payload: {"name": "agent-name", "target_node": "rpi-bedroom"}
                            asyncio.create_task(self._migrate_agent(data))

                        elif topic_str == f"nodes/{self.node_name}/stop_all":
                            logger.info("[runner] stop_all received — shutting down.")
                            asyncio.create_task(self._shutdown())

                        elif topic_str == f"nodes/{self.node_name}/list":
                            await self.publish(
                                f"nodes/{self.node_name}/agents",
                                {
                                    "node": self.node_name,
                                    "agents": [
                                        {"name": a.name, "actor_id": a.actor_id}
                                        for a in self._agents.values()
                                    ],
                                    "timestamp": time.time(),
                                },
                            )

                        elif topic_str.startswith(f"nodes/{self.node_name}/reply/"):
                            # Route reply back to the waiting agent
                            for agent in self._agents.values():
                                agent.deliver_reply(topic_str, data)

                        elif "/task" in topic_str:
                            # agents/by-name/{agent_name}/task
                            parts = topic_str.split("/")
                            if len(parts) >= 4:
                                agent_name = parts[2]
                                agent = self._agents.get(agent_name)
                                if agent and isinstance(data, dict):
                                    payload    = data.get("payload", data)
                                    reply_topic = data.get("_reply_topic")
                                    result = await agent.handle_task(payload)
                                    if reply_topic:
                                        await self.publish(reply_topic, result)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning(f"[runner] Subscriber disconnected: {e}. Reconnecting in 3s...")
                    await asyncio.sleep(3)

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        logger.info(f"[runner] Starting node '{self.node_name}' → broker {self.broker}:{self.port}")

        tasks = [
            asyncio.create_task(self._publisher_loop()),
            asyncio.create_task(self._subscriber_loop()),
            asyncio.create_task(self._node_heartbeat_loop()),
        ]

        # Announce presence
        await asyncio.sleep(1)   # let publisher connect first
        await self.publish(
            f"nodes/{self.node_name}/heartbeat",
            {"node": self.node_name, "status": "online",
             "timestamp": time.time(), "agents": []},
        )
        logger.info(f"[runner] Node '{self.node_name}' online.")

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop_all()
            for t in tasks:
                t.cancel()

    async def _migrate_agent(self, payload: dict):
        """
        Move a running agent to a different node.
        Grabs its config from the spawn registry, publishes it to the target
        node, then stops the local instance.

        payload: {"name": "agent-name", "target_node": "rpi-bedroom"}
        """
        name        = payload.get("name")
        target_node = payload.get("target_node")
        if not name or not target_node:
            logger.warning(f"[runner] migrate: missing 'name' or 'target_node' in payload")
            return

        agent = self._agents.get(name)
        if not agent:
            logger.warning(f"[runner] migrate: agent '{name}' not running here")
            await self.publish(
                f"nodes/{self.node_name}/migrate_result",
                {"success": False, "error": f"Agent '{name}' not found on {self.node_name}",
                 "agent": name, "timestamp": time.time()},
            )
            return

        # Capture config before stopping — clone it with the new node target
        config = dict(agent._config)
        config["node"] = target_node
        config.pop("replace", None)   # clean slate on new node

        logger.info(f"[runner] Migrating '{name}' from {self.node_name} → {target_node}")

        # Stop locally first
        await self.stop_agent(name)
        await asyncio.sleep(0.3)    # let heartbeat "stopped" reach broker

        # Publish spawn to target node via MQTT
        await self.publish(f"nodes/{target_node}/spawn", config)

        await self.publish(
            f"nodes/{self.node_name}/migrate_result",
            {"success": True, "agent": name,
             "from_node": self.node_name, "to_node": target_node,
             "timestamp": time.time()},
        )
        logger.info(f"[runner] Migration of '{name}' to '{target_node}' dispatched.")

    async def _shutdown(self):
        self._running = False
        await self.stop_all()
        await self.publish(
            f"nodes/{self.node_name}/heartbeat",
            {"node": self.node_name, "status": "offline", "timestamp": time.time()},
        )
        sys.exit(0)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AgentFlow edge node runner — deploy on Raspberry Pi or any remote machine"
    )
    parser.add_argument("--broker",  default=os.getenv("AGENTFLOW_BROKER", "localhost"),
                        help="MQTT broker host (default: localhost or $AGENTFLOW_BROKER)")
    parser.add_argument("--port",    type=int, default=1883,
                        help="MQTT broker port (default: 1883)")
    parser.add_argument("--name",    default=os.getenv("AGENTFLOW_NODE", f"node-{uuid.uuid4().hex[:6]}"),
                        help="Unique node name (default: $AGENTFLOW_NODE or random)")
    parser.add_argument("--loglevel", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.loglevel),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    runner = _RemoteRunner(broker=args.broker, port=args.port, node_name=args.name)

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _signal_handler():
        logger.info("[runner] Shutdown signal received.")
        loop.create_task(runner._shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, AttributeError):
            pass   # Windows doesn't support add_signal_handler for all signals

    try:
        loop.run_until_complete(runner.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
