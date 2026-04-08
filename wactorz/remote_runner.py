#!/usr/bin/env python3
"""
remote_runner.py — Wactorz edge node runner.

Deploy this single file to any machine (Raspberry Pi, VM, edge device).
It connects to the shared MQTT broker, listens for spawn commands from main,
and runs DynamicAgents locally. Those agents heartbeat back to the same broker
so they appear in the central dashboard exactly like local agents.

Usage on the remote machine:
    pip install aiomqtt paho-mqtt psutil aiohttp --break-system-packages
    python3 remote_runner.py --broker 192.168.1.10 --name rpi-livingroom

From the main Wactorz chat (automatic via devops-agent):
    "deploy node rpi-livingroom to pi@192.168.1.50 with broker 192.168.1.10"

Or manually in the chat spawn block:
    <spawn>
    {
      "name": "temp-sensor-agent",
      "node": "rpi-livingroom",
      "type": "dynamic",
      "description": "Reads temperature from DHT22 sensor",
      "poll_interval": 30,
      "max_restarts": 5,
      "restart_delay": 3.0,
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
wactorz package installed on the edge device.

Each agent runs under a local supervisor (mirroring the main machine's OTP-style
ONE_FOR_ONE strategy). If an agent crashes, the supervisor restarts it with
exponential back-off (3s → 6s → 12s … capped at 60s). After max_restarts
consecutive failures the agent is marked failed and removed from the registry.
Compile errors and setup() fatals are never retried — broken code won't fix itself.
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
        self._published_topics: set = set()

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
        if topic not in self._published_topics:
            self._published_topics.add(topic)
            await self._publish_manifest()

    async def _publish_manifest(self):
        """Advertise this agent's published topics so main can discover them."""
        cfg = self._agent._config
        manifest = {
            "name":          self.name,
            "actor_id":      self.actor_id,
            "node":          self.node,
            "description":   cfg.get("description", ""),
            "capabilities":  cfg.get("capabilities", []),
            "input_schema":  cfg.get("input_schema",  {}),
            "output_schema": cfg.get("output_schema", {}),
            "publishes":     sorted(self._published_topics),
            "timestamp":     time.time(),
        }
        await self._agent._runner.publish(
            f"agents/{self.actor_id}/manifest", manifest, retain=True
        )

    async def publish_result(self, data: Any):
        """Publish agent result to agents/{id}/results — mirrors DynamicAgent API."""
        await self._agent._publish(
            f"agents/{self.actor_id}/results",
            {"agent": self.name, "node": self.node, "result": data, "timestamp": time.time()},
        )

    async def publish_detection(self, data: Any):
        """Publish detection results to agents/{id}/detections — mirrors DynamicAgent API."""
        await self._agent._publish(
            f"agents/{self.actor_id}/detections",
            {"agent": self.name, "node": self.node, "detections": data, "timestamp": time.time()},
        )
        # Also publish to a human-friendly topic for easy MQTT subscription
        await self.publish(f"{self.node}/{self.name}/detections", data)

    async def set_status(self, status: str):
        """Update agent task status string visible in dashboard."""
        self._agent._status = status

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
        self.actor_id   = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.actor.{self.name}"))
        self.node_name  = runner.node_name
        self._runner    = runner
        self._config    = config
        self._code      = config.get("code", "")
        self._poll      = float(config.get("poll_interval", 5.0))
        self._ns: dict  = {}               # shared namespace for user code
        self._state: dict = {}             # agent.state dict for user code
        self._persistent_state: dict = {}
        self._state_path = f"/tmp/wactorz_{self.name}_state.json"
        self._pending_replies: dict[str, asyncio.Future] = {}
        self._api       = _RemoteAgentAPI(self)
        self._tasks:    list[asyncio.Task] = []
        self._running   = False

        self._fn_setup       = None
        self._fn_process     = None
        self._fn_handle_task = None

        # ── Supervisor state ──────────────────────────────────────────────────
        self._max_restarts   = int(config.get("max_restarts", 5))
        self._restart_delay  = float(config.get("restart_delay", 3.0))
        self._restart_count  = 0
        self._failed         = False   # True = budget exhausted, do not restart

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
        """
        Start the agent under a supervision loop.
        The supervisor restarts the agent on unexpected crashes up to
        max_restarts times, with exponential back-off between attempts.
        Compile errors and deliberate stop() calls are never retried.
        """
        self._running = True
        asyncio.create_task(self._supervisor_loop())

    async def _supervisor_loop(self):
        """
        Supervisor that mirrors the local OTP ONE_FOR_ONE strategy.
        Runs _run_lifecycle() in a loop; on crash, waits and retries.
        """
        while self._running and not self._failed:
            try:
                await self._run_lifecycle()
            except asyncio.CancelledError:
                break   # deliberate stop() — do not restart
            except Exception as e:
                if not self._running:
                    break   # stop() was called mid-crash, don't restart

                self._restart_count += 1
                if self._restart_count > self._max_restarts:
                    self._failed = True
                    logger.error(
                        f"[{self.name}] Crashed {self._restart_count} times — "
                        f"giving up (max_restarts={self._max_restarts})."
                    )
                    await self._publish(
                        f"agents/{self.actor_id}/errors",
                        {"phase": "supervisor", "severity": "fatal",
                         "error": f"Restart budget exhausted after {self._restart_count} crashes: {e}",
                         "restart_count": self._restart_count,
                         "agent": self.name, "timestamp": time.time()},
                    )
                    await self._publish_heartbeat("failed")
                    # Remove from runner registry so /nodes shows it as gone
                    self._runner._agents.pop(self.name, None)
                    break

                delay = min(self._restart_delay * (2 ** (self._restart_count - 1)), 60.0)
                logger.warning(
                    f"[{self.name}] Crashed (attempt {self._restart_count}/{self._max_restarts}). "
                    f"Restarting in {delay:.1f}s..."
                )
                await self._publish(
                    f"agents/{self.actor_id}/errors",
                    {"phase": "supervisor", "severity": "warning",
                     "error": f"Agent crashed, restarting in {delay:.1f}s (attempt "
                              f"{self._restart_count}/{self._max_restarts}): {e}",
                     "restart_count": self._restart_count,
                     "agent": self.name, "timestamp": time.time()},
                )
                await self._publish_heartbeat("restarting")
                # Cancel any leftover tasks from the crashed run
                for t in self._tasks:
                    t.cancel()
                self._tasks.clear()
                await asyncio.sleep(delay)
                # Re-compile fresh (code doesn't change, but namespace must be clean)
                self._ns = {}

    async def _run_lifecycle(self):
        """
        One full agent lifecycle: compile → setup → process loop + heartbeat loop.
        Raises on unhandled exceptions so _supervisor_loop can catch and restart.
        Compile errors and setup fatals publish an error event then return cleanly
        (no restart — broken code won't fix itself on retry).
        """
        # Reset per-run namespace and function pointers
        self._ns = {}
        self._fn_setup = self._fn_process = self._fn_handle_task = None

        err = self._compile()
        if err:
            logger.error(f"[{self.name}] {err}")
            await self._publish(
                f"agents/{self.actor_id}/errors",
                {"phase": "compile", "severity": "fatal",
                 "error": err, "agent": self.name, "timestamp": time.time()},
            )
            self._running = False   # compile error → stop supervising
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
                self._running = False   # setup fatal → stop supervising
                return

        inner_tasks = []
        if self._fn_process:
            inner_tasks.append(asyncio.create_task(self._process_loop()))
        inner_tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._tasks = inner_tasks

        # Publish manifest immediately so main knows this remote agent exists
        # even before it calls publish() on any data topic
        await self._api._publish_manifest()

        # Wait for any task to finish (process escalation OR deliberate stop/cancel).
        # We use first-exception semantics: as soon as one task raises, cancel the rest.
        done, pending = await asyncio.wait(
            inner_tasks, return_when=asyncio.FIRST_EXCEPTION
        )
        # Cancel any still-running tasks (e.g. _heartbeat_loop after process escalation)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Re-raise any non-cancellation exception so the supervisor can restart
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc

    async def stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        self._save_state()
        await self._publish_heartbeat("stopped")
        logger.info(f"[{self.name}] Stopped.")

    # ── Loops ─────────────────────────────────────────────────────────────────

    # After this many consecutive process() errors, raise to trigger a supervisor restart
    _PROCESS_ESCALATE_AFTER = 5

    async def _process_loop(self):
        """
        Run process() in a loop with per-error backoff.
        After _PROCESS_ESCALATE_AFTER consecutive errors, raises RuntimeError
        so the supervisor loop gets a clean restart (fresh namespace, reset state).
        A single successful call resets the consecutive counter.
        """
        consecutive_errors = 0
        successful_runs    = 0
        while self._running:
            try:
                await self._fn_process(self._api)
                consecutive_errors  = 0
                successful_runs    += 1
                # After sustained healthy operation, credit back one restart token
                if successful_runs >= 10:
                    successful_runs = 0
                    if self._restart_count > 0:
                        self._restart_count -= 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                successful_runs     = 0
                err_str = traceback.format_exc()
                severity = "critical" if consecutive_errors >= 3 else "warning"
                logger.error(f"[{self.name}] process() error #{consecutive_errors}: {e}")
                await self._publish(
                    f"agents/{self.actor_id}/errors",
                    {"phase": "process", "severity": severity,
                     "error": str(e), "consecutive": consecutive_errors,
                     "traceback": err_str[:800],
                     "agent": self.name, "timestamp": time.time()},
                )
                if consecutive_errors >= self._PROCESS_ESCALATE_AFTER:
                    # Too many consecutive failures — let supervisor restart with clean namespace
                    raise RuntimeError(
                        f"process() failed {consecutive_errors} times in a row, "
                        f"last error: {e}"
                    )
                # Exponential backoff before next attempt
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
        self._pub_queue: asyncio.Queue = None   # created in run() inside the event loop
        self._running   = False

    # ── MQTT publish (queue-based, reconnect-safe) ────────────────────────────

    async def publish(self, topic: str, data: Any, retain: bool = False):
        payload = json.dumps(data) if not isinstance(data, (str, bytes)) else data
        if isinstance(payload, str):
            payload = payload.encode()
        await self._pub_queue.put((topic, payload, retain))

    # ── Spawn / stop agents ───────────────────────────────────────────────────

    async def spawn_agent(self, config: dict):
        if not isinstance(config, dict):
            logger.warning(f"[runner] spawn_agent: invalid config type {type(config)}, ignoring.")
            return
        name = config.get("name", f"agent-{uuid.uuid4().hex[:6]}")
        logger.info(f"[runner] Spawning agent '{name}'...")
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

        try:
            agent = _RemoteAgent(config, self)
            self._agents[name] = agent
            await agent.start()
            logger.info(f"[runner] Agent '{name}' started.")
        except Exception as e:
            logger.error(f"[runner] Failed to start agent '{name}': {e}")
            self._agents.pop(name, None)
            await self.publish(
                f"agents/{self.node_name}/logs",
                {"type": "error", "message": f"Failed to start '{name}': {e}",
                 "node": self.node_name, "timestamp": time.time()},
            )
            return

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

    async def _node_heartbeat_loop(self, interval: float = 10.0):
        """Publish a heartbeat for the runner process itself so it appears in dashboard."""
        node_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.node.{self.node_name}"))
        while self._running:
            try:
                agent_names = list(self._agents.keys())
                await self.publish(
                    f"nodes/{self.node_name}/heartbeat",
                    {
                        "node":        self.node_name,
                        "node_id":     node_id,
                        "timestamp":   time.time(),
                        "agents":      agent_names,
                        "agent_count": len(agent_names),
                        "broker":      self.broker,
                    },
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(interval)

    # ── MQTT publisher task (paho-mqtt direct — aiomqtt v2.x doesn't flush reliably) ──

    async def _publisher_loop(self):
        """
        Uses paho-mqtt directly for reliable fire-and-forget publishing.
        aiomqtt v2.x wraps paho but its internal network loop doesn't get CPU
        time when we block on queue.get(), causing silent message loss.
        paho.loop_start() runs a background thread that handles ACKs/keepalives.
        """
        import paho.mqtt.client as paho_mqtt
        loop = asyncio.get_event_loop()

        def _connect():
            c = paho_mqtt.Client(client_id=f"runner-pub-{self.node_name}-{uuid.uuid4().hex[:6]}")
            c.connect(self.broker, self.port, keepalive=60)
            c.loop_start()
            return c

        client = None
        while self._running:
            try:
                if client is None:
                    client = await loop.run_in_executor(None, _connect)
                    logger.info(f"[runner] Publisher connected to {self.broker}:{self.port}")

                item = await self._pub_queue.get()
                topic, payload = item[0], item[1]
                retain = item[2] if len(item) > 2 else False
                client.publish(topic, payload, qos=1, retain=retain)
                self._pub_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[runner] Publisher error: {e}. Reconnecting in 3s...")
                if client:
                    try: client.loop_stop(); client.disconnect()
                    except Exception: pass
                    client = None
                await asyncio.sleep(3)

        if client:
            try: client.loop_stop(); client.disconnect()
            except Exception: pass

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
            f"nodes/{self.node_name}/desired_state",   # reconciliation on reboot
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

                        if topic_str == f"nodes/{self.node_name}/desired_state":
                            # Reconcile: start any agents in desired state not already running
                            if not msg.payload or not isinstance(data, dict):
                                continue
                            desired = data.get("agents", [])
                            if not desired:
                                continue
                            logger.info(f"[runner] Reconciling desired state: {[a.get('name') for a in desired]}")
                            for agent_config in desired:
                                aname = agent_config.get("name")
                                if not aname:
                                    continue
                                if aname in self._agents:
                                    logger.info(f"[runner] '{aname}' already running, skipping.")
                                else:
                                    logger.info(f"[runner] Reconcile: starting missing agent '{aname}'")
                                    def _log_exc(t):
                                        if not t.cancelled() and t.exception():
                                            logger.error(f"[runner] reconcile task failed: {t.exception()}")
                                    task = asyncio.create_task(self.spawn_agent(agent_config))
                                    task.add_done_callback(_log_exc)

                        elif topic_str == f"nodes/{self.node_name}/spawn":
                            if not msg.payload:   # empty = retain-clear message, ignore
                                continue
                            def _log_task_exc(t):
                                if not t.cancelled() and t.exception():
                                    logger.error(f"[runner] spawn_agent task failed: {t.exception()}")
                            task = asyncio.create_task(self.spawn_agent(data))
                            task.add_done_callback(_log_task_exc)
                            # Clear the retained message so this spawn doesn't
                            # re-fire every time the subscriber reconnects/restarts
                            asyncio.create_task(self.publish(topic_str, b"", retain=True))

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
                                        # Normalise to dict so the caller can use .get()
                                        if not isinstance(result, dict):
                                            result = {"result": str(result) if result is not None else ""}
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
        self._pub_queue = asyncio.Queue()   # must be created inside the running event loop
        logger.info(f"[runner] Starting node '{self.node_name}' → broker {self.broker}:{self.port}")

        tasks = [
            asyncio.create_task(self._publisher_loop()),
            asyncio.create_task(self._subscriber_loop()),
            asyncio.create_task(self._node_heartbeat_loop()),
        ]

        await asyncio.sleep(1)   # let publisher connect before anything else fires
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
        description="Wactorz edge node runner — deploy on Raspberry Pi or any remote machine"
    )
    parser.add_argument("--broker",  default=os.getenv("WACTORZ_BROKER", "localhost"),
                        help="MQTT broker host (default: localhost or $WACTORZ_BROKER)")
    parser.add_argument("--port",    type=int, default=1883,
                        help="MQTT broker port (default: 1883)")
    _default_node = os.getenv("WACTORZ_NODE", f"node-{uuid.uuid4().hex[:6]}")
    parser.add_argument("--name",    default=_default_node,
                        help="Unique node name (default: $WACTORZ_NODE or random)")
    parser.add_argument("--node",    default=None,
                        help="Alias for --name (either works)")
    parser.add_argument("--loglevel", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    # --node takes priority over --name if both supplied
    node_name = args.node if args.node else args.name

    logging.basicConfig(
        level=getattr(logging, args.loglevel),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    runner = _RemoteRunner(broker=args.broker, port=args.port, node_name=node_name)

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


# ── Self-test (python3 remote_runner.py --test) ───────────────────────────────




async def _run_supervisor_tests():
    """Standalone tests. No MQTT broker required."""
    passed = 0
    failed = 0

    class _StubRunner:
        node_name = "test-node"
        def __init__(self):
            self._agents = {}
            self.events  = []
        async def publish(self, topic, data):
            self.events.append((topic, data if isinstance(data, dict) else data))

    def make_agent(code, max_restarts=3, restart_delay=0.01, poll_interval=0.01, escalate_after=5):
        runner = _StubRunner()
        config = {
            "name": "test-agent", "code": code,
            "max_restarts": max_restarts, "restart_delay": restart_delay,
            "poll_interval": poll_interval,
        }
        agent = _RemoteAgent(config, runner)
        agent._PROCESS_ESCALATE_AFTER = escalate_after
        agent._running = True   # start() sets this; we call _supervisor_loop directly in tests
        runner._agents["test-agent"] = agent
        return agent, runner

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS  {name}")
            passed += 1
        else:
            print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))
            failed += 1

    print("\n-- remote_runner supervisor tests --")

    # Test 1: Stable agent never restarted
    agent, runner = make_agent("async def process(agent): pass")
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.15)
    agent._running = False
    await asyncio.sleep(0.05)
    task.cancel()
    try: await task
    except: pass
    check("stable: restart_count=0", agent._restart_count == 0, f"got {agent._restart_count}")
    check("stable: not failed", not agent._failed)

    # Test 2: Crashing process escalates and triggers supervisor restart
    crash_code = "async def process(agent):\n    raise RuntimeError('boom')"
    agent, runner = make_agent(crash_code, max_restarts=3, restart_delay=0.01,
                                poll_interval=0.001, escalate_after=2)
    task = asyncio.create_task(agent._supervisor_loop())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
    except asyncio.TimeoutError:
        pass
    check("crash: error events published", any(
        isinstance(e, dict) and e.get("phase") in ("process","supervisor")
        for _, e in runner.events), f"{[(t,d.get('phase') if isinstance(d,dict) else '?') for t,d in runner.events[:5]]}")
    check("crash: restart_count > 0", agent._restart_count > 0, f"got {agent._restart_count}")
    # Either failed completely, or has accumulated restarts (budget=3 may not exhaust in time)
    check("crash: supervisor restarted at least once",
          agent._failed or agent._restart_count >= 1, f"count={agent._restart_count}")

    # Test 3: Budget exhaustion
    agent, runner = make_agent(crash_code, max_restarts=1, restart_delay=0.01,
                                poll_interval=0.001, escalate_after=1)
    task = asyncio.create_task(agent._supervisor_loop())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
    except asyncio.TimeoutError:
        pass
    check("budget: _failed=True", agent._failed, f"count={agent._restart_count}")
    check("budget: fatal event", any(
        isinstance(e, dict) and e.get("severity") == "fatal"
        for _, e in runner.events))
    check("budget: removed from runner", "test-agent" not in runner._agents)

    # Test 4: deliberate stop() no restart
    agent, runner = make_agent("async def process(agent): pass")
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.05)
    await agent.stop()
    task.cancel()
    try: await task
    except: pass
    check("stop(): restart_count=0", agent._restart_count == 0)
    check("stop(): not failed", not agent._failed)

    # Test 5: health credit after 10 successful runs
    agent, runner = make_agent("async def process(agent): pass", poll_interval=0.001)
    agent._restart_count = 2
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.3)
    agent._running = False
    await asyncio.sleep(0.05)
    task.cancel()
    try: await task
    except: pass
    check("health credit: restart_count < 2", agent._restart_count < 2, f"got {agent._restart_count}")

    # Test 6: compile error stops supervision
    agent, runner = make_agent("this is not valid python !!!")
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.15)
    task.cancel()
    try: await task
    except: pass
    check("compile: _running=False", not agent._running)
    check("compile: restart_count=0", agent._restart_count == 0)
    check("compile: fatal event", any(
        isinstance(e, dict) and e.get("phase") == "compile"
        for _, e in runner.events), f"{runner.events}")

    # Test 7: setup() error stops supervision
    setup_fail = "async def setup(agent):\n    raise RuntimeError('bad')\nasync def process(agent):\n    pass"
    agent, runner = make_agent(setup_fail)
    task = asyncio.create_task(agent._supervisor_loop())
    await asyncio.sleep(0.15)
    task.cancel()
    try: await task
    except: pass
    check("setup: _running=False", not agent._running)
    check("setup: restart_count=0", agent._restart_count == 0)

    print(f"\n  {passed} passed, {failed} failed\n")
    return failed == 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        ok = asyncio.run(_run_supervisor_tests())
        sys.exit(0 if ok else 1)
    else:
        main()
