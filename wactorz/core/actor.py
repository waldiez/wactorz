"""Base Actor - the foundation of the Actor Model framework.
Every agent IS an actor. Actors communicate via message passing only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pickle
import sys
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from .atomic_io import write_pickle

if TYPE_CHECKING:
    # Imported for type hints only — avoids a runtime import cycle (registry imports actor).
    from .persistence import PersistenceAPI
    from .registry import ActorRegistry


class SupervisorStrategy(str, Enum):
    """Restart strategy for supervised actors — inspired by Erlang/OTP.

    ONE_FOR_ONE   — restart only the crashed actor, leave siblings untouched.
                    Use for independent workers (weather-agent, news-agent, …).

    ONE_FOR_ALL   — if one supervised actor crashes, restart ALL siblings too.
                    Use when actors share state or have hard ordering dependencies.

    REST_FOR_ONE  — restart the crashed actor AND every actor that was registered
                    after it (i.e. downstream dependents).
                    Use when later actors depend on earlier ones being healthy first.
    """

    ONE_FOR_ONE = "one_for_one"
    ONE_FOR_ALL = "one_for_all"
    REST_FOR_ONE = "rest_for_one"


logger = logging.getLogger(__name__)


class ActorState(str, Enum):
    """Where an actor is in its lifecycle."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"


class MessageType(str, Enum):
    """What a message is asking of its recipient."""

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
    """One unit of communication between actors."""

    type: MessageType
    sender_id: str
    payload: Any = None
    reply_to: str | None = None
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """A JSON-serialisable form, for MQTT and the dashboard."""
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
    """Running counters for one actor, reported in heartbeats."""

    messages_processed: int = 0
    errors: int = 0
    start_time: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    tasks_completed: int = 0
    tasks_failed: int = 0
    restart_count: int = 0  # incremented by Supervisor on each restart

    @property
    def uptime(self) -> float:
        """Seconds since the actor started."""
        return time.time() - self.start_time


class Actor(ABC):
    """Base Actor class. All agents inherit from this.
    Actors are fully async and communicate only through messages.
    """

    def __init__(
        self,
        actor_id: str | None = None,
        name: str | None = None,
        persistence_dir: str = "./actor_state",
        mailbox_size: int = 1000,
    ):
        if actor_id:
            self.actor_id = actor_id
        elif name:
            # Deterministic UUID from name — same name always gets same ID across restarts
            self.actor_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.actor.{name}"))
        else:
            self.actor_id = str(uuid.uuid4())
        self.name = name or f"actor-{self.actor_id[:8]}"
        self.state = ActorState.IDLE
        self.metrics = ActorMetrics()

        # Async mailbox (inbox)
        self._mailbox: asyncio.Queue = asyncio.Queue(maxsize=mailbox_size)
        self._outbox: dict[str, asyncio.Queue] = {}  # actor_id -> queue ref

        # Registry reference (set by ActorSystem)
        self._registry: ActorRegistry | None = None
        self._mqtt_client: Any | None = None
        self._mqtt_broker: str = "localhost"
        self._mqtt_port: int = 1883

        # Persistence
        # Use name as persistence folder so it survives restarts with same name
        # Falls back to actor_id for anonymous actors
        safe_name = self.name.replace("/", "_").replace("\\", "_")
        self._persistence_dir = Path(persistence_dir) / safe_name
        self._persistence_dir.mkdir(parents=True, exist_ok=True)
        self._persistent_state: dict = {}

        # Unified persistence API — set by ActorSystem if available,
        # otherwise falls back to legacy pickle behavior
        self._persistence_api: PersistenceAPI | None = None

        # Protection — if True, stop/delete/pause commands are ignored
        self.protected: bool = False

        # Supervisor reference — set by Supervisor when this actor is registered under it
        self.supervisor_id: str | None = None

        # Handlers
        self._handlers: dict[MessageType, Callable] = {}
        self._setup_default_handlers()

        # Background tasks
        self._tasks: list[asyncio.Task] = []

        # Cached process handle for heartbeat metrics — one per actor so each
        # has an independent cpu_percent baseline (interval=None, non-blocking).
        self._proc: Any | None = None
        try:
            self._proc = psutil.Process()
            self._proc.cpu_percent(interval=None)  # prime the baseline
        except Exception:
            pass

        logger.info("[%s] Actor created with id=%s", self.name, self.actor_id)

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        """Start the actor's event loop."""
        self.state = ActorState.RUNNING
        self.metrics.start_time = time.time()
        await self._load_persistent_state()
        # Restore message count from previous session
        saved_msgs = self.recall("_messages_processed", {})
        if isinstance(saved_msgs, dict) and saved_msgs.get("count"):
            self.metrics.messages_processed += int(saved_msgs["count"])
        await self.on_start()
        self._tasks.append(asyncio.create_task(self._message_loop()))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._tasks.append(asyncio.create_task(self._command_listener()))
        await self._publish_status()
        logger.info("[%s] Actor started.", self.name)

    async def stop(self):
        """Gracefully stop the actor."""
        self.state = ActorState.STOPPED
        for task in self._tasks:
            task.cancel()
        # Shield cleanup from CancelledError — chat tasks run as fire-and-forget
        # asyncio tasks outside actor._tasks and get cancelled by asyncio.run()
        # cleanup BEFORE these awaits if we don't shield them.
        try:
            await asyncio.shield(self.on_stop())
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await asyncio.shield(self._save_persistent_state())
        except (asyncio.CancelledError, Exception):
            pass

        # ── Persist message count so overview survives restarts ──────────
        if self.metrics.messages_processed > 0:
            self.persist("_messages_processed", {"count": self.metrics.messages_processed})

        # ── Persist final cost metrics (for LLM-backed agents) ─────────
        # Cost data lives in-memory and dies with the agent object.
        # Persist it so the UI can show lifetime costs for deleted agents.
        if hasattr(self, "total_cost_usd") and getattr(self, "total_cost_usd", 0) > 0:
            self.persist(
                "_final_cost",
                {
                    "input_tokens": getattr(self, "total_input_tokens", 0),
                    "output_tokens": getattr(self, "total_output_tokens", 0),
                    "cost_usd": round(getattr(self, "total_cost_usd", 0), 6),
                    "name": self.name,
                    "stopped_at": time.time(),
                },
            )

        # ── Publish final metrics before the agent disappears ──────────
        # The heartbeat loop is already cancelled at this point, so this
        # is the UI's last chance to capture cost/usage data.
        try:
            final_metrics = self._build_metrics()
            final_metrics["final"] = True  # signals UI this is the last message
            await self._mqtt_publish(
                f"agents/{self.actor_id}/metrics",
                final_metrics,
            )
        except Exception:
            pass

        await self._publish_status()
        # ── Unregister from TopicBus ───────────────────────────────────
        # Remove this agent's TopicContract so the planner doesn't wire
        # against topics from stopped/deleted/replaced agents.
        try:
            from .topic_bus import get_topic_bus

            bus = get_topic_bus()
            if bus:
                bus.unregister(self.name)
        except Exception:
            pass  # TopicBus not initialised or unavailable — not fatal
        logger.info("[%s] Actor stopped.", self.name)

    async def pause(self):
        """Stop processing messages, keeping the mailbox and registration."""
        self.state = ActorState.PAUSED
        await self._publish_status()

    async def resume(self):
        """Start processing messages again after a pause."""
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
                _noise = {
                    MessageType.HEARTBEAT,
                    MessageType.STATUS_REQUEST,
                    MessageType.STATUS_RESPONSE,
                    MessageType.STOP,
                    MessageType.PAUSE,
                    MessageType.RESUME,
                }
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
                logger.error("[%s] Error in message loop: %s", self.name, e, exc_info=True)

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
        # Release from supervision before stopping so the heartbeat watchdog
        # doesn't race to restart us after we go quiet.
        if self._registry and hasattr(self._registry, "_supervisor_ref"):
            sup = self._registry._supervisor_ref
            if sup is not None:
                sup.release(self.name)
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
                logger.warning("[%s] Heartbeat error: %s", self.name, e)

    def _estimate_memory_mb(self) -> float:
        """Bounded deep-size walk over this actor's own data structures.
        Uses only sys.getsizeof — no new deps, works on Windows.
        """
        seen: set = set()

        def _deep(obj: object, depth: int) -> int:
            if depth > 5:
                return 0
            oid = id(obj)
            if oid in seen:
                return 0
            seen.add(oid)
            sz = sys.getsizeof(obj)
            if isinstance(obj, dict):
                sz += sum(_deep(k, depth + 1) + _deep(v, depth + 1) for k, v in obj.items())
            elif isinstance(obj, (list, tuple, set, frozenset)):
                sz += sum(_deep(item, depth + 1) for item in obj)
            return sz

        total = 0
        for attr in ("_persistent_state", "_outbox", "metrics"):
            val = getattr(self, attr, None)
            if val is not None:
                total += _deep(val, 0)
        # Rough proxy for messages sitting in the mailbox (~512 B each)
        total += self._mailbox.qsize() * 512
        return total / (1024 * 1024)

    def _build_heartbeat(self) -> dict:
        cpu = 0.0
        try:
            if self._proc is not None:
                cpu = self._proc.cpu_percent(interval=None)
        except Exception:
            pass
        return {
            "actor_id": self.actor_id,
            "name": self.name,
            "timestamp": time.time(),
            "state": self.state.value,
            "cpu": cpu,
            "memory_mb": self._estimate_memory_mb(),
            "task": self._current_task_description(),
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
            "restart_count": self.metrics.restart_count,
        }

    LIFECYCLE_COMMANDS = ("pause", "resume", "stop", "delete")

    def _release_from_supervision(self) -> None:
        """Tell the supervisor this actor is leaving deliberately.

        The Erlang unlink. Stopping without it is indistinguishable from
        crashing, so the heartbeat-silence watchdog fires ~35s later and
        restarts the actor that was just deliberately stopped.
        """
        if self._registry and hasattr(self._registry, "_supervisor_ref"):
            sup = self._registry._supervisor_ref
            if sup is not None:
                sup.release(self.name)

    async def apply_command(self, command: str) -> bool:
        """Run a lifecycle command on this actor. Returns whether it took effect.

        These are not a dispatch table, which is why they live here rather than
        in the transport below: ``stop`` must release from supervision first, and
        ``delete`` also clears the spawn registry and unregisters. A caller that
        holds the actor and simply awaits ``stop()`` gets it restarted half a
        minute later by the watchdog.

        Every entry point routes through this — the broker listener, and the web
        layer when the actor is local — so the two cannot drift apart.

        Returns ``False`` when the command was refused (protected actor) or
        unknown, so a caller can report that rather than claim success.
        """
        if self.protected and command in ("stop", "pause", "delete"):
            logger.warning("[%s] Ignoring %r — actor is protected.", self.name, command)
            return False

        if command == "pause":
            await self.pause()
        elif command == "resume":
            await self.resume()
        elif command == "stop":
            self._release_from_supervision()
            await self.stop()
        elif command == "delete":
            self._release_from_supervision()
            if self._registry:
                main = self._registry.find_by_name("main")
                if main and hasattr(main, "_remove_from_spawn_registry"):
                    main._remove_from_spawn_registry(self.name)  # pyright: ignore[reportAttributeAccessIssue]
                await self._registry.unregister(self.actor_id)
            await self.stop()
        else:
            logger.warning("[%s] Unknown command: %r", self.name, command)
            return False
        return True

    async def _command_listener(self):
        """Carry commands from agents/{id}/commands to :meth:`apply_command`.

        Transport only. Needed for actors a caller cannot reach in process —
        after the direct-dispatch change that means agents on remote nodes.
        """
        try:
            import aiomqtt  # noqa: F401  # pylint: disable=unused-import
        except ImportError:
            return
        from .mqtt import mqtt_client  # local: avoids core/__init__ import cycle

        topic = f"agents/{self.actor_id}/commands"
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                async with mqtt_client(self._mqtt_broker, self._mqtt_port) as client:
                    await client.subscribe(topic)
                    logger.debug("[%s] Subscribed to %s", self.name, topic)
                    async for message in client.messages:
                        try:
                            data = json.loads(message.payload.decode())
                            command = data.get("command", "")
                            logger.info("[%s] Received command: %s", self.name, command)
                            applied = await self.apply_command(command)
                            # Only stop listening if it actually took effect: a
                            # protected actor refuses stop/delete and must keep
                            # receiving commands afterwards.
                            if applied and command in ("stop", "delete"):
                                return
                        except Exception as exc:
                            logger.error("[%s] Command parse error: %s", self.name, exc)
            except asyncio.CancelledError:
                break
            except Exception:
                if self.state not in (ActorState.STOPPED, ActorState.FAILED):
                    await asyncio.sleep(5)

    def _current_task_description(self) -> str:
        return "idle"  # Override in subclasses

    # ─── Messaging ────────────────────────────────────────────────────────────

    async def send(self, target_id: str, msg_type: MessageType, payload: Any = None) -> bool:
        """Send a message to another actor."""
        if self._registry is None:
            logger.warning("[%s] No registry attached, cannot send messages.", self.name)
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

    async def spawn(self, actor_class: type[Actor], **kwargs: Any) -> Actor:
        """Spawn a child actor. The child inherits:
        - MQTT client (so it can publish heartbeats/status)
        - Registry (so it can send/receive messages)
        - Persistence dir defaults to same root
        - Persistence API (SQLite/memory/Pickle routing)

        Erlang/OTP supervision: if the owning ActorSystem has a Supervisor,
        the child is automatically registered under it with ONE_FOR_ONE so it
        will be restarted if it crashes — no child is an orphan.
        """
        # Default persistence to same root as parent
        kwargs.setdefault("persistence_dir", str(self._persistence_dir.parent))

        child = actor_class(**kwargs)

        # Inherit everything from parent
        child._mqtt_client = self._mqtt_client  # MQTT publish connection
        child._mqtt_broker = self._mqtt_broker  # broker address for command listener
        child._mqtt_port = self._mqtt_port  # broker port
        child._registry = self._registry  # message routing

        # Inherit persistence API if available
        if self._persistence_api is not None:
            try:
                from .persistence import PersistenceAPI, get_db, get_pickle_store

                db = get_db()
                pkl = get_pickle_store()
                if db and pkl:
                    child._persistence_api = PersistenceAPI(db, pkl, child.name)
            except ImportError:
                pass

        # Register in registry
        if self._registry:
            await self._registry.register(child)

        # Start the child
        await child.start()

        # ── Erlang/OTP: register child under Supervisor so it's never an orphan ──
        # We reach into the registry to find the ActorSystem's supervisor.
        # If no supervisor is available this is a safe no-op.
        try:
            if self._registry and hasattr(self._registry, "_supervisor_ref"):
                supervisor = self._registry._supervisor_ref
                if supervisor is not None and child.name not in supervisor._specs:
                    # Snapshot what the child was built from. The factory runs
                    # again on every restart, possibly much later, and must
                    # rebuild the child as it was rather than from whatever this
                    # actor's settings have become since.
                    cls = actor_class
                    kw = dict(kwargs)
                    mc = self._mqtt_client
                    mb = self._mqtt_broker
                    mp = self._mqtt_port
                    papi = self._persistence_api

                    async def _child_factory() -> Actor:
                        c = cls(**kw)
                        c._mqtt_client = mc
                        c._mqtt_broker = mb
                        c._mqtt_port = mp
                        if papi is not None:
                            try:
                                from .persistence import (
                                    PersistenceAPI,
                                    get_db,
                                    get_pickle_store,
                                )

                                db = get_db()
                                pkl = get_pickle_store()
                                if db and pkl:
                                    c._persistence_api = PersistenceAPI(db, pkl, c.name)
                            except ImportError:
                                pass
                        return c

                    supervisor.supervise(
                        child.name,
                        _child_factory,
                        strategy=SupervisorStrategy.ONE_FOR_ONE,
                        max_restarts=5,
                        restart_window=60.0,
                        restart_delay=2.0,
                    )
                    # Point spec.actor at the already-running child so the watch loop
                    # starts monitoring immediately without a redundant restart.
                    supervisor._specs[child.name].actor = child
                    if child.name not in supervisor._order:
                        supervisor._order.append(child.name)
                    child.supervisor_id = str(id(supervisor))
                    logger.info(
                        "[%s] Child '%s' auto-registered under Supervisor.", self.name, child.name
                    )
        except Exception as _sup_err:
            # Never let supervision registration crash the spawn itself
            logger.warning(
                "[%s] Could not auto-supervise child '%s': %s", self.name, child.name, _sup_err
            )

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
        logger.info("[%s] Spawned: %s (%s)", self.name, child.name, child.actor_id[:8])
        return child

    # ─── Persistence ──────────────────────────────────────────────────────────

    async def _save_persistent_state(self):
        """Save state to disk. Called on stop() after on_stop()."""
        if self._persistence_api is not None:
            # New path: state is written per-key via persist(), nothing to batch-save.
            # But keep pickle save for agent.state (arbitrary objects) backward compat.
            return
        # Legacy pickle path
        try:
            write_pickle(self._persistence_dir / "state.pkl", self._persistent_state)
        except Exception as e:
            logger.error("[%s] Failed to save state: %s", self.name, e)

    async def _load_persistent_state(self):
        """Load state from disk. Called on start() before on_start()."""
        if self._persistence_api is not None:
            # New path: state is loaded per-key via recall(), nothing to batch-load.
            # But load legacy pickle for backward compat if it exists.
            path = self._persistence_dir / "state.pkl"
            if path.exists():
                try:
                    with open(path, "rb") as f:
                        self._persistent_state = pickle.load(f)
                    logger.info(
                        "[%s] Loaded legacy persistent state (will migrate on first persist).",
                        self.name,
                    )
                except Exception as e:
                    logger.error("[%s] Failed to load legacy state: %s", self.name, e)
            return
        # Legacy pickle path
        path = self._persistence_dir / "state.pkl"
        if path.exists():
            try:
                with open(path, "rb") as f:
                    self._persistent_state = pickle.load(f)
                logger.info("[%s] Loaded persistent state.", self.name)
            except Exception as e:
                logger.error("[%s] Failed to load state: %s", self.name, e)

    def persist(self, key: str, value: Any):
        """Persist a key-value pair. Routes to the correct backend:
          - Known structured keys → SQLite
          - Known ephemeral keys → process memory
          - Everything else → Pickle

        If the new PersistenceAPI is not available, falls back to legacy
        pickle behavior (writes entire dict to disk on every call).
        """
        if self._persistence_api is not None:
            self._persistence_api.set(key, value)
            return

        # Legacy pickle path — the whole dict goes to disk on every call, so an
        # interrupted write here would lose every key, not just this one.
        self._persistent_state[key] = value
        try:
            write_pickle(self._persistence_dir / "state.pkl", self._persistent_state)
        except Exception as e:
            logger.warning("[%s] persist write failed for %r: %s", self.name, key, e)

    def recall(self, key: str, default: Any = None) -> Any:
        """Recall a persisted value. Routes to the correct backend.
        Returns default if the key doesn't exist.
        """
        if self._persistence_api is not None:
            # Check new store first, then fall back to legacy in-memory dict
            # (handles migration period where some keys are in pickle, some in new store)
            result = self._persistence_api.get(key)
            if result is not None:
                return result
            # Fallback: check legacy in-memory state (loaded from old .pkl)
            return self._persistent_state.get(key, default)

        # Legacy pickle path
        return self._persistent_state.get(key, default)

    # ─── MQTT ─────────────────────────────────────────────────────────────────

    async def _mqtt_publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0):
        if self._mqtt_client:
            try:
                # Stamp telemetry frames with the agent's own name. logs/spawned
                # payloads carry only the topic id, and while an agent is being
                # spawned or installing deps it isn't in any registry yet — but it
                # always knows self.name, so the feed can attribute the row.
                if isinstance(payload, dict) and (topic.endswith(("/logs", "/spawned"))):
                    payload.setdefault("name", self.name)
                # Empty bytes = clear a retained message (MQTT spec)
                # Must send raw empty bytes, not JSON-encoded
                if payload == b"" or (payload is None and retain):
                    encoded = b""
                elif isinstance(payload, (bytes, bytearray)):
                    encoded = payload
                else:
                    encoded = json.dumps(payload)
                await self._mqtt_client.publish(topic, encoded, retain=retain, qos=qos)
            except Exception as e:
                logger.debug("[%s] MQTT publish failed: %s", self.name, e)

    async def _publish_status(self):
        await self._mqtt_publish(f"agents/{self.actor_id}/status", self.get_status())

    async def notify_user(self, text: str, **extra):
        """Push a user-facing chat message to the UI from this actor, OUTSIDE the
        synchronous request/reply turn.

        The monitor forwards agents/{id}/chat messages to the chat panel as live
        chat frames, so use this for autonomous notifications the user should see
        in chat without having just asked for them: a long delegated task
        finishing after the reply already returned, sensor alerts, scheduled
        output, reactive automations, etc.
        """
        payload = {
            "from": self.name,
            "to": "user",
            "content": str(text),
            "timestamp": time.time(),
        }
        if extra:
            payload.update(extra)
        await self._mqtt_publish(f"agents/{self.actor_id}/chat", payload)

    # ─── Status ───────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """This actor's identity, state and counters, as the dashboard shows them."""
        return {
            "actor_id": self.actor_id,
            "name": self.name,
            "state": self.state.value,
            "uptime": self.metrics.uptime,
            "messages_processed": self.metrics.messages_processed,
            "restart_count": self.metrics.restart_count,
            "supervised": self.supervisor_id is not None,
        }

    # ─── Abstract / Override ──────────────────────────────────────────────────

    async def on_start(self):
        """Called when actor starts. Override for init logic."""

    async def publish_manifest(
        self,
        description: str = "",
        publishes: list[str] | None = None,
        capabilities: list[str] | None = None,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        """Publish a capability manifest so main's topic registry can discover this actor.
        Call from on_start() in any actor that wants to be discoverable.
        Manifests are retained — main sees them immediately even after restart.

        input_schema / output_schema — dicts describing expected payload fields, e.g.:
            input_schema  = {"city": "str — city name to fetch weather for"}
            output_schema = {"temp_c": "float", "condition": "str", "humidity": "int"}
        """
        manifest = {
            "name": self.name,
            "actor_id": self.actor_id,
            "description": description,
            "publishes": publishes or [],
            "capabilities": capabilities or [],
            "input_schema": input_schema or {},
            "output_schema": output_schema or {},
            "timestamp": time.time(),
        }
        await self._mqtt_publish(f"agents/{self.actor_id}/manifest", manifest, retain=True)

    async def on_stop(self):
        """Called when actor stops. Override for cleanup."""

    @abstractmethod
    async def handle_message(self, msg: Message):
        """Handle messages not caught by default handlers."""

    def __repr__(self):
        return f"<Actor name={self.name} id={self.actor_id[:8]} state={self.state.value}>"
