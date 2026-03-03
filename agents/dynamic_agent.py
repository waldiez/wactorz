"""
DynamicAgent - A generic actor shell whose behavior is defined by LLM-generated code.

The LLM writes three async functions:
  async def setup(agent):        # called once on start — load models, open connections
  async def process(agent):      # called in a loop — core logic, publish results
  async def handle_task(agent, payload): # called when another agent sends a TASK

The `agent` parameter gives access to:
  agent.publish(topic, data)     # publish to MQTT
  agent.log(message)             # add to event log
  agent.alert(message, severity) # trigger an alert
  agent.name                     # agent name
  agent.actor_id                 # unique ID
  agent.state                    # current state
  agent.persist(key, val)        # save to disk
  agent.recall(key)              # load from disk
  agent.send_to(name, payload)   # send task to another agent
"""

import asyncio
import logging
import time
import traceback
from typing import Any, Optional

from ..core.actor import Actor, Message, MessageType, ActorState

logger = logging.getLogger(__name__)


class DynamicAgent(Actor):
    """
    Generic actor shell. Core behavior is provided as Python source code strings.
    The LLM writes setup/process/handle_task functions; this class runs them.
    """

    def __init__(
        self,
        code: str,                          # LLM-generated Python source
        poll_interval: float = 1.0,         # seconds between process() calls
        description: str = "",              # what this agent does
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._code           = code
        self.poll_interval   = poll_interval
        self.description     = description

        # Compiled functions — populated in on_start
        self._fn_setup       = None
        self._fn_process     = None
        self._fn_handle_task = None

        # Namespace shared across all calls (agent can store state here)
        self._ns: dict       = {}

        # Public API exposed to generated code via `agent` parameter
        self._api            = _AgentAPI(self)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_start(self):
        self._compile_code()
        if self._fn_setup:
            try:
                await self._fn_setup(self._api)
                logger.info(f"[{self.name}] setup() completed.")
            except Exception as e:
                err = traceback.format_exc()
                logger.error(f"[{self.name}] setup() failed: {e}\n{err}")
                await self._mqtt_publish(
                    f"agents/{self.actor_id}/logs",
                    {"type": "log", "message": f"SETUP ERROR: {e}", "timestamp": time.time()}
                )
        if self._fn_process:
            self._tasks.append(asyncio.create_task(self._process_loop()))

    async def on_stop(self):
        # Give generated code a chance to clean up
        cleanup = self._ns.get("cleanup")
        if cleanup:
            try:
                await cleanup(self._api)
            except Exception:
                pass

    # ── Code compilation ───────────────────────────────────────────────────

    def _compile_code(self):
        """Compile and exec the LLM-generated code into self._ns."""
        try:
            exec(compile(self._code, f"<{self.name}>", "exec"), self._ns)
            self._fn_setup       = self._ns.get("setup")
            self._fn_process     = self._ns.get("process")
            self._fn_handle_task = self._ns.get("handle_task")

            fns = [f for f in ["setup", "process", "handle_task", "cleanup"] if f in self._ns]
            logger.info(f"[{self.name}] Code compiled OK. Functions: {fns}")

            if not fns:
                logger.warning(f"[{self.name}] No functions found in code! Check the generated code.")
        except Exception as e:
            err = traceback.format_exc()
            logger.error(f"[{self.name}] Code compilation failed: {e}\n{err}")
            # Publish error so it shows in dashboard log
            asyncio.create_task(self._mqtt_publish(
                f"agents/{self.actor_id}/logs",
                {"type": "log", "message": f"CODE ERROR: {e}", "timestamp": time.time()}
            ))

    # ── Process loop ───────────────────────────────────────────────────────

    async def _process_loop(self):
        """Continuously call the generated process() function."""
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            if self.state == ActorState.PAUSED:
                await asyncio.sleep(self.poll_interval)
                continue
            try:
                await self._fn_process(self._api)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.metrics.errors += 1
                logger.error(f"[{self.name}] process() error: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(2)   # back off on errors
            await asyncio.sleep(self.poll_interval)

    # ── Message handling ───────────────────────────────────────────────────

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            self.metrics.messages_processed += 1
            if self._fn_handle_task:
                try:
                    result = await self._fn_handle_task(self._api, msg.payload or {})
                    if msg.sender_id and result is not None:
                        await self.send(msg.sender_id, MessageType.RESULT, result)
                except Exception as e:
                    logger.error(f"[{self.name}] handle_task() error: {e}")
                    if msg.sender_id:
                        await self.send(msg.sender_id, MessageType.RESULT, {"error": str(e)})
            else:
                if msg.sender_id:
                    await self.send(msg.sender_id, MessageType.RESULT,
                                    {"info": f"{self.name} has no handle_task defined"})

    def get_status(self) -> dict:
        s = super().get_status()
        s["description"] = self.description
        s["code"]        = self._code
        s["agent_type"]  = "dynamic"
        return s

    def _build_heartbeat(self) -> dict:
        hb = super()._build_heartbeat()
        hb["code"]        = self._code      # include code in every heartbeat
        hb["description"] = self.description
        hb["agent_type"]  = "dynamic"
        return hb

    def _current_task_description(self) -> str:
        return self.description or "running dynamic code"


class _AgentAPI:
    """
    Clean API surface exposed to LLM-generated code via the `agent` parameter.
    Wraps the actual Actor internals so generated code can't break the framework.
    """

    def __init__(self, actor: DynamicAgent):
        self._actor = actor
        self.name     = actor.name
        self.actor_id = actor.actor_id
        # Shared mutable namespace — generated code can store anything here
        self.state: dict = {}

    # ── MQTT ───────────────────────────────────────────────────────────────

    async def publish(self, topic: str, data: Any):
        """Publish data to an MQTT topic. topic is used as-is."""
        await self._actor._mqtt_publish(topic, data)

    async def publish_detection(self, data: Any):
        """Convenience: publish to agents/{id}/detections"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/detections", data)

    async def publish_result(self, data: Any):
        """Convenience: publish to agents/{id}/result"""
        await self._actor._mqtt_publish(f"agents/{self._actor.actor_id}/result", data)

    # ── Logging / alerting ─────────────────────────────────────────────────

    async def log(self, message: str, level: str = "info"):
        """Add a message to the event log visible in the dashboard."""
        # Encode safely for Windows terminals that can't handle all unicode
        safe_msg = message.encode("ascii", errors="replace").decode("ascii")
        getattr(logger, level, logger.info)(f"[{self.name}] {safe_msg}")
        await self._actor._mqtt_publish(
            f"agents/{self._actor.actor_id}/logs",
            {"type": "log", "message": message, "timestamp": time.time()}
        )

    async def alert(self, message: str, severity: str = "warning"):
        """Trigger an alert visible in the dashboard."""
        await self._actor._mqtt_publish(
            f"agents/{self._actor.actor_id}/alert",
            {
                "actor_id":  self._actor.actor_id,
                "name":      self.name,
                "message":   message,
                "severity":  severity,
                "timestamp": time.time(),
            }
        )

    # ── Persistence ────────────────────────────────────────────────────────

    def persist(self, key: str, value: Any):
        self._actor.persist(key, value)

    def recall(self, key: str) -> Any:
        return self._actor.recall(key)

    # ── Inter-agent messaging ──────────────────────────────────────────────

    async def send_to(self, agent_name: str, payload: Any) -> Optional[Any]:
        """Send a TASK to another agent by name and wait for result."""
        registry = self._actor._registry
        if not registry:
            return None
        target = registry.find_by_name(agent_name)
        if not target:
            logger.warning(f"[{self.name}] send_to: agent '{agent_name}' not found")
            return None
        future = asyncio.get_event_loop().create_future()
        # Simple one-shot reply via a temporary handler
        orig_handle = self._actor.handle_message
        async def _tmp_handle(msg: Message):
            if msg.type == MessageType.RESULT and not future.done():
                future.set_result(msg.payload)
            else:
                await orig_handle(msg)
        self._actor.handle_message = _tmp_handle
        await self._actor.send(target.actor_id, MessageType.TASK, payload)
        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            return None
        finally:
            self._actor.handle_message = orig_handle

    # ── Metrics ────────────────────────────────────────────────────────────

    def increment_processed(self):
        self._actor.metrics.messages_processed += 1

    def increment_errors(self):
        self._actor.metrics.errors += 1
