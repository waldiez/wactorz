"""
Base Actor - the foundation of the Actor Model framework.
Every agent IS an actor. Actors communicate via message passing only.
"""

import asyncio
import uuid
import time
import psutil
import logging
import json
import pickle
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class ActorState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"


class MessageType(str, Enum):
    # Lifecycle
    START = "start"
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"
    DELETE = "delete"
    # Communication
    TASK = "task"
    RESULT = "result"
    HEARTBEAT = "heartbeat"
    SPAWN = "spawn"
    # Internal
    TICK = "tick"
    STATUS_REQUEST = "status_request"
    STATUS_RESPONSE = "status_response"


@dataclass
class Message:
    type: MessageType
    sender_id: str
    payload: Any = None
    reply_to: Optional[str] = None
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "sender_id": self.sender_id,
            "payload": self.payload,
            "reply_to": self.reply_to,
            "message_id": self.message_id,
            "timestamp": self.timestamp,
        }


@dataclass
class ActorMetrics:
    messages_processed: int = 0
    errors: int = 0
    start_time: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    tasks_completed: int = 0
    tasks_failed: int = 0

    @property
    def uptime(self) -> float:
        return time.time() - self.start_time


class Actor(ABC):
    """
    Base Actor class. All agents inherit from this.
    Actors are fully async and communicate only through messages.
    """

    def __init__(
        self,
        actor_id: Optional[str] = None,
        name: Optional[str] = None,
        persistence_dir: str = "./actor_state",
        mailbox_size: int = 1000,
    ):
        if actor_id:
            self.actor_id = actor_id
        elif name:
            # Deterministic UUID from name — same name always gets same ID across restarts
            self.actor_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"agentflow.actor.{name}"))
        else:
            self.actor_id = str(uuid.uuid4())
        self.name = name or f"actor-{self.actor_id[:8]}"
        self.state = ActorState.IDLE
        self.metrics = ActorMetrics()

        # Async mailbox (inbox)
        self._mailbox: asyncio.Queue = asyncio.Queue(maxsize=mailbox_size)
        self._outbox: dict[str, asyncio.Queue] = {}  # actor_id -> queue ref

        # Registry reference (set by ActorSystem)
        self._registry: Optional["ActorRegistry"] = None
        self._mqtt_client: Optional[Any] = None
        self._mqtt_broker: str = "localhost"
        self._mqtt_port: int = 1883

        # Persistence
        # Use name as persistence folder so it survives restarts with same name
        # Falls back to actor_id for anonymous actors
        safe_name = self.name.replace("/", "_").replace("\\", "_")
        self._persistence_dir = Path(persistence_dir) / safe_name
        self._persistence_dir.mkdir(parents=True, exist_ok=True)
        self._persistent_state: dict = {}

        # Protection — if True, stop/delete/pause commands are ignored
        self.protected: bool = False

        # Handlers
        self._handlers: dict[MessageType, Callable] = {}
        self._setup_default_handlers()

        # Background tasks
        self._tasks: list[asyncio.Task] = []

        logger.info(f"[{self.name}] Actor created with id={self.actor_id}")

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        """Start the actor's event loop."""
        self.state = ActorState.RUNNING
        self.metrics.start_time = time.time()
        await self._load_persistent_state()
        await self.on_start()
        self._tasks.append(asyncio.create_task(self._message_loop()))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._tasks.append(asyncio.create_task(self._command_listener()))
        await self._publish_status()
        logger.info(f"[{self.name}] Actor started.")

    async def stop(self):
        """Gracefully stop the actor."""
        self.state = ActorState.STOPPED
        for task in self._tasks:
            task.cancel()
        await self.on_stop()                  # on_stop() calls persist() first
        await self._save_persistent_state()   # THEN save to disk
        await self._publish_status()
        logger.info(f"[{self.name}] Actor stopped.")

    async def pause(self):
        self.state = ActorState.PAUSED
        await self._publish_status()

    async def resume(self):
        self.state = ActorState.RUNNING
        await self._publish_status()

    # ─── Message Loop ─────────────────────────────────────────────────────────

    async def _message_loop(self):
        """Main message processing loop."""
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                if self.state == ActorState.PAUSED:
                    await asyncio.sleep(0.1)
                    continue

                msg = await asyncio.wait_for(self._mailbox.get(), timeout=1.0)
                # Only count meaningful messages — not heartbeats, status pings, lifecycle
                _noise = {MessageType.HEARTBEAT, MessageType.STATUS_REQUEST,
                          MessageType.STATUS_RESPONSE, MessageType.STOP,
                          MessageType.PAUSE, MessageType.RESUME}
                if msg.type not in _noise:
                    self.metrics.messages_processed += 1
                await self._dispatch(msg)
                self._mailbox.task_done()

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.metrics.errors += 1
                logger.error(f"[{self.name}] Error in message loop: {e}", exc_info=True)

    async def _dispatch(self, msg: Message):
        """Dispatch message to the appropriate handler."""
        handler = self._handlers.get(msg.type)
        if handler:
            await handler(msg)
        else:
            await self.handle_message(msg)

    def _setup_default_handlers(self):
        self._handlers = {
            MessageType.STOP: self._handle_stop,
            MessageType.PAUSE: self._handle_pause,
            MessageType.RESUME: self._handle_resume,
            MessageType.STATUS_REQUEST: self._handle_status_request,
            MessageType.HEARTBEAT: self._handle_heartbeat_msg,
        }

    async def _handle_stop(self, msg: Message):
        await self.stop()

    async def _handle_pause(self, msg: Message):
        await self.pause()

    async def _handle_resume(self, msg: Message):
        await self.resume()

    async def _handle_status_request(self, msg: Message):
        status = self.get_status()
        # Reply to sender_id (always), reply_to is optional override
        target = msg.reply_to or msg.sender_id
        if target:
            await self.send(target, MessageType.STATUS_RESPONSE, status)

    async def _handle_heartbeat_msg(self, msg: Message):
        pass  # Monitor actor handles these

    # ─── Heartbeat ────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self, interval: float = 10.0):
        """Periodically publish heartbeat via MQTT."""
        # Publish immediately on start so monitor sees agent right away
        await asyncio.sleep(0.5)
        await self._mqtt_publish(f"agents/{self.actor_id}/heartbeat", self._build_heartbeat())
        await self._mqtt_publish(f"agents/{self.actor_id}/metrics", self._build_metrics())
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                await asyncio.sleep(interval)
                hb = self._build_heartbeat()
                self.metrics.last_heartbeat = time.time()
                await self._mqtt_publish(f"agents/{self.actor_id}/heartbeat", hb)
                await self._mqtt_publish(f"agents/{self.actor_id}/metrics", self._build_metrics())
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[{self.name}] Heartbeat error: {e}")

    def _build_heartbeat(self) -> dict:
        proc = psutil.Process()
        return {
            "actor_id":  self.actor_id,
            "name":      self.name,
            "timestamp": time.time(),
            "state":     self.state.value,
            "cpu":       proc.cpu_percent(interval=0.1),
            "memory_mb": proc.memory_info().rss / 1024 / 1024,
            "task":      self._current_task_description(),
            "protected": self.protected,
        }

    def _build_metrics(self) -> dict:
        return {
            "actor_id": self.actor_id,
            "messages_processed": self.metrics.messages_processed,
            "errors": self.metrics.errors,
            "uptime": self.metrics.uptime,
            "tasks_completed": self.metrics.tasks_completed,
            "tasks_failed": self.metrics.tasks_failed,
        }

    async def _command_listener(self):
        """Listen for commands published to agents/{id}/commands via MQTT."""
        try:
            import aiomqtt
        except ImportError:
            return

        topic = f"agents/{self.actor_id}/commands"
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                async with aiomqtt.Client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe(topic)
                    logger.debug(f"[{self.name}] Subscribed to {topic}")
                    async for message in client.messages:
                        try:
                            data    = json.loads(message.payload.decode())
                            command = data.get("command", "")
                            logger.info(f"[{self.name}] Received command: {command}")
                            if self.protected and command in ("stop", "pause", "delete"):
                                logger.warning(f"[{self.name}] Ignoring '{command}' — actor is protected.")
                                continue
                            if command == "stop":
                                await self.stop()
                                return
                            elif command == "pause":
                                await self.pause()
                            elif command == "resume":
                                await self.resume()
                            elif command == "delete":
                                # If main actor knows about this agent, remove from spawn registry
                                if self._registry:
                                    main = self._registry.find_by_name("main")
                                    if main and hasattr(main, "_remove_from_spawn_registry"):
                                        main._remove_from_spawn_registry(self.name)
                                    await self._registry.unregister(self.actor_id)
                                await self.stop()
                                return
                        except Exception as e:
                            logger.error(f"[{self.name}] Command parse error: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.state not in (ActorState.STOPPED, ActorState.FAILED):
                    await asyncio.sleep(5)

    def _current_task_description(self) -> str:
        return "idle"  # Override in subclasses

    # ─── Messaging ────────────────────────────────────────────────────────────

    async def send(self, target_id: str, msg_type: MessageType, payload: Any = None) -> bool:
        """Send a message to another actor."""
        if self._registry is None:
            logger.warning(f"[{self.name}] No registry attached, cannot send messages.")
            return False
        msg = Message(type=msg_type, sender_id=self.actor_id, payload=payload)
        return await self._registry.deliver(target_id, msg)

    async def broadcast(self, msg_type: MessageType, payload: Any = None):
        """Broadcast to all registered actors."""
        if self._registry:
            await self._registry.broadcast(self.actor_id, msg_type, payload)

    async def receive(self, msg: Message):
        """External entry point - put message in mailbox."""
        await self._mailbox.put(msg)

    # ─── Actor Spawning ───────────────────────────────────────────────────────

    async def spawn(self, actor_class: type, **kwargs) -> "Actor":
        """
        Spawn a child actor. The child inherits:
        - MQTT client (so it can publish heartbeats/status)
        - Registry (so it can send/receive messages)
        - Persistence dir defaults to same root
        """
        # Default persistence to same root as parent
        kwargs.setdefault("persistence_dir", str(self._persistence_dir.parent))

        child = actor_class(**kwargs)

        # Inherit everything from parent
        child._mqtt_client  = self._mqtt_client   # MQTT publish connection
        child._mqtt_broker  = self._mqtt_broker   # broker address for command listener
        child._mqtt_port    = self._mqtt_port     # broker port
        child._registry     = self._registry      # message routing

        # Register in registry
        if self._registry:
            await self._registry.register(child)

        # Start the child
        await child.start()

        # Immediately announce to monitor - don't wait for heartbeat loop
        await child._publish_status()
        await child._mqtt_publish(
            f"agents/{child.actor_id}/heartbeat",
            child._build_heartbeat(),
        )
        await child._mqtt_publish(
            f"agents/{child.actor_id}/metrics",
            child._build_metrics(),
        )

        # Notify parent's topic that it spawned a child
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawned",
            {"child_id": child.actor_id, "child_name": child.name, "timestamp": time.time()},
        )
        logger.info(f"[{self.name}] Spawned: {child.name} ({child.actor_id[:8]})")
        return child

    # ─── Persistence ──────────────────────────────────────────────────────────

    async def _save_persistent_state(self):
        path = self._persistence_dir / "state.pkl"
        try:
            with open(path, "wb") as f:
                pickle.dump(self._persistent_state, f)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to save state: {e}")

    async def _load_persistent_state(self):
        path = self._persistence_dir / "state.pkl"
        if path.exists():
            try:
                with open(path, "rb") as f:
                    self._persistent_state = pickle.load(f)
                logger.info(f"[{self.name}] Loaded persistent state.")
            except Exception as e:
                logger.error(f"[{self.name}] Failed to load state: {e}")

    def persist(self, key: str, value: Any):
        self._persistent_state[key] = value

    def recall(self, key: str, default: Any = None) -> Any:
        return self._persistent_state.get(key, default)

    # ─── MQTT ─────────────────────────────────────────────────────────────────

    async def _mqtt_publish(self, topic: str, payload: Any):
        if self._mqtt_client:
            try:
                await self._mqtt_client.publish(topic, json.dumps(payload))
            except Exception as e:
                logger.debug(f"[{self.name}] MQTT publish failed: {e}")

    async def _publish_status(self):
        await self._mqtt_publish(f"agents/{self.actor_id}/status", self.get_status())

    # ─── Status ───────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "actor_id": self.actor_id,
            "name": self.name,
            "state": self.state.value,
            "uptime": self.metrics.uptime,
            "messages_processed": self.metrics.messages_processed,
        }

    # ─── Abstract / Override ──────────────────────────────────────────────────

    async def on_start(self):
        """Called when actor starts. Override for init logic."""
        pass

    async def on_stop(self):
        """Called when actor stops. Override for cleanup."""
        pass

    @abstractmethod
    async def handle_message(self, msg: Message):
        """Handle messages not caught by default handlers."""
        pass

    def __repr__(self):
        return f"<Actor name={self.name} id={self.actor_id[:8]} state={self.state.value}>"