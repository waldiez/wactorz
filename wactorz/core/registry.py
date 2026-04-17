"""
ActorRegistry - Central registry and message router for all actors.
ActorSystem orchestrates startup, shutdown, and actor lifecycle.
Supervisor implements Erlang/OTP-style supervision trees.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Type

from .actor import Actor, Message, MessageType, SupervisorStrategy

logger = logging.getLogger(__name__)


# ── Supervision spec ──────────────────────────────────────────────────────────

@dataclass
class SupervisedSpec:
    """
    Descriptor for one actor under supervision.

    factory      : zero-arg async callable that creates and returns a fresh
                   Actor instance (already injected with MQTT / registry).
    strategy     : how to react when THIS actor crashes.
    max_restarts : max restarts within restart_window seconds before giving up.
    restart_window: sliding window in seconds for max_restarts accounting.
    restart_delay : seconds to wait before restarting (lets dependencies settle).
    """
    factory:        Callable[[], "Actor"]
    strategy:       SupervisorStrategy = SupervisorStrategy.ONE_FOR_ONE
    max_restarts:   int   = 5
    restart_window: float = 60.0
    restart_delay:  float = 1.0

    # Runtime state — managed by Supervisor, not set by caller
    actor:          Optional["Actor"] = field(default=None, repr=False)
    _restart_times: list = field(default_factory=list, repr=False)

    def record_restart(self) -> bool:
        """Record a restart attempt. Returns True if still within budget."""
        now = time.time()
        cutoff = now - self.restart_window
        self._restart_times = [t for t in self._restart_times if t > cutoff]
        self._restart_times.append(now)
        return len(self._restart_times) <= self.max_restarts

    @property
    def exhausted(self) -> bool:
        now = time.time()
        cutoff = now - self.restart_window
        recent = [t for t in self._restart_times if t > cutoff]
        return len(recent) >= self.max_restarts

logger = logging.getLogger(__name__)


class ActorRegistry:
    """Maintains a map of all living actors and routes messages between them."""

    def __init__(self):
        self._actors: dict[str, Actor] = {}
        self._lock = asyncio.Lock()

    async def register(self, actor: Actor):
        async with self._lock:
            actor._registry = self
            self._actors[actor.actor_id] = actor
            logger.info(f"[Registry] Registered {actor.name} ({actor.actor_id[:8]})")

    async def unregister(self, actor_id: str):
        async with self._lock:
            if actor_id in self._actors:
                del self._actors[actor_id]
                logger.info(f"[Registry] Unregistered {actor_id[:8]}")

    async def deliver(self, target_id: str, msg: Message) -> bool:
        actor = self._actors.get(target_id)
        if actor is None:
            logger.warning(f"[Registry] Unknown target: {target_id[:8]}")
            return False
        await actor.receive(msg)
        return True

    async def broadcast(self, sender_id: str, msg_type: MessageType, payload=None):
        msg = Message(type=msg_type, sender_id=sender_id, payload=payload)
        for actor_id, actor in list(self._actors.items()):
            if actor_id != sender_id:
                await actor.receive(msg)

    def get(self, actor_id: str) -> Optional[Actor]:
        return self._actors.get(actor_id)

    def all_actors(self) -> list[Actor]:
        return list(self._actors.values())

    def find_by_name(self, name: str) -> Optional[Actor]:
        for actor in self._actors.values():
            if actor.name == name:
                return actor
        return None

    def __len__(self):
        return len(self._actors)


class Supervisor:
    """
    OTP-inspired supervision tree node.

    Sits above ActorSystem and owns a set of critical actors.  When one of
    those actors crashes (state == FAILED or task raises), the Supervisor
    applies the configured SupervisorStrategy and restarts the affected actors
    automatically — without requiring the monitor or the LLM to intervene.

    Strategies
    ----------
    ONE_FOR_ONE   restart only the crashed actor.
    ONE_FOR_ALL   restart ALL supervised actors.
    REST_FOR_ONE  restart the crashed actor plus every actor registered after it.

    Usage
    -----
    supervisor = Supervisor(registry, mqtt_inject_fn)
    supervisor.supervise("main",    main_factory,    strategy=ONE_FOR_ONE, max_restarts=10)
    supervisor.supervise("monitor", monitor_factory, strategy=ONE_FOR_ONE, max_restarts=10)
    await supervisor.start()
    # …later, supervisor watches actors in the background via _watch_loop
    """

    def __init__(self, registry: "ActorRegistry", inject_fn: Callable[["Actor"], None],
                 poll_interval: float = 2.0):
        self._registry     = registry
        self._inject       = inject_fn           # sets MQTT client + broker/port on actor
        self._poll_interval = poll_interval       # seconds between liveness checks
        self._specs:     dict[str, SupervisedSpec] = {}   # name → spec (ordered)
        self._order:     list[str] = []                   # insertion order for REST_FOR_ONE
        self._watch_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ── Registration ──────────────────────────────────────────────────────────

    def supervise(
        self,
        name:           str,
        factory:        Callable[[], "Actor"],
        strategy:       SupervisorStrategy = SupervisorStrategy.ONE_FOR_ONE,
        max_restarts:   int   = 5,
        restart_window: float = 60.0,
        restart_delay:  float = 1.0,
    ) -> "Supervisor":
        """Register an actor to be supervised. Call before start()."""
        spec = SupervisedSpec(
            factory        = factory,
            strategy       = strategy,
            max_restarts   = max_restarts,
            restart_window = restart_window,
            restart_delay  = restart_delay,
        )
        self._specs[name] = spec
        self._order.append(name)
        return self   # fluent

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self):
        """Spawn all supervised actors and start the watch loop."""
        for name in self._order:
            spec = self._specs[name]
            actor = await self._spawn_actor(name, spec)
            spec.actor = actor

        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info(f"[Supervisor] Started. Supervising: {list(self._specs)}")

    # ── Watch loop ────────────────────────────────────────────────────────────

    async def _watch_loop(self):
        """Poll supervised actors for failure and trigger restarts."""
        from .actor import ActorState
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                async with self._lock:
                    for name, spec in list(self._specs.items()):
                        actor = spec.actor
                        if actor is None:
                            continue
                        if actor.state == ActorState.FAILED:
                            logger.warning(
                                f"[Supervisor] '{name}' is FAILED — "
                                f"applying {spec.strategy.value} strategy."
                            )
                            await self._apply_strategy(name, spec)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[Supervisor] watch_loop error: {exc}", exc_info=True)

    # ── Strategy application ─────────────────────────────────────────────────

    async def _apply_strategy(self, crashed_name: str, crashed_spec: SupervisedSpec):
        if crashed_spec.strategy == SupervisorStrategy.ONE_FOR_ONE:
            await self._restart_one(crashed_name, crashed_spec)

        elif crashed_spec.strategy == SupervisorStrategy.ONE_FOR_ALL:
            logger.info("[Supervisor] ONE_FOR_ALL — restarting all supervised actors.")
            # Stop all others first (reverse order), then restart in order
            for name in reversed(self._order):
                spec = self._specs[name]
                if spec.actor and name != crashed_name:
                    await self._stop_actor(name, spec)
            for name in self._order:
                await self._restart_one(name, self._specs[name])

        elif crashed_spec.strategy == SupervisorStrategy.REST_FOR_ONE:
            idx = self._order.index(crashed_name)
            affected = self._order[idx:]   # crashed + everyone registered after it
            logger.info(
                f"[Supervisor] REST_FOR_ONE — restarting: {affected}"
            )
            for name in reversed(affected):
                spec = self._specs[name]
                if spec.actor and name != crashed_name:
                    await self._stop_actor(name, spec)
            for name in affected:
                await self._restart_one(name, self._specs[name])

    # ── Individual restart ────────────────────────────────────────────────────

    async def _restart_one(self, name: str, spec: SupervisedSpec):
        if spec.exhausted:
            logger.critical(
                f"[Supervisor] '{name}' has exhausted its restart budget "
                f"({spec.max_restarts} restarts / {spec.restart_window}s). "
                f"Giving up — manual intervention required."
            )
            await self._notify_main(
                f"🚨 Supervisor gave up on '{name}' after "
                f"{spec.max_restarts} restarts. Manual intervention required."
            )
            return

        within_budget = spec.record_restart()
        if not within_budget:
            return  # exhausted check above already handles this edge case

        if spec.restart_delay > 0:
            await asyncio.sleep(spec.restart_delay)

        logger.info(
            f"[Supervisor] Restarting '{name}' "
            f"(attempt {len(spec._restart_times)}/{spec.max_restarts})."
        )

        # Stop the old actor cleanly if possible
        if spec.actor:
            await self._stop_actor(name, spec)

        # Spawn a fresh one
        new_actor = await self._spawn_actor(name, spec)
        spec.actor = new_actor
        new_actor.metrics.restart_count = len(spec._restart_times)

        logger.info(f"[Supervisor] '{name}' restarted successfully.")
        await self._notify_main(
            f"🔄 Supervisor restarted '{name}' "
            f"(restart #{new_actor.metrics.restart_count})."
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _spawn_actor(self, name: str, spec: SupervisedSpec) -> "Actor":
        """Create actor via factory, inject MQTT, register, and start."""
        actor = await spec.factory() if asyncio.iscoroutinefunction(spec.factory) \
                else spec.factory()
        self._inject(actor)
        actor.supervisor_id = id(self)
        await self._registry.register(actor)
        await actor.start()
        logger.debug(f"[Supervisor] Spawned '{name}' ({actor.actor_id[:8]}).")
        return actor

    async def _stop_actor(self, name: str, spec: SupervisedSpec):
        """Stop an actor gracefully, unregister it, swallow errors."""
        actor = spec.actor
        if actor is None:
            return
        try:
            await actor.stop()
        except Exception as exc:
            logger.warning(f"[Supervisor] Error stopping '{name}': {exc}")
        try:
            await self._registry.unregister(actor.actor_id)
        except Exception:
            pass
        spec.actor = None

    async def _notify_main(self, message: str):
        """Best-effort notification to the main actor."""
        try:
            main = self._registry.find_by_name("main")
            if main and hasattr(main, "_pending_notifications"):
                main._pending_notifications.append({
                    "severity": "critical",
                    "message":  message,
                    "source":   "supervisor",
                    "timestamp": time.time(),
                })
        except Exception as exc:
            logger.warning(f"[Supervisor] Could not notify main: {exc}")

    # ── Introspection ─────────────────────────────────────────────────────────

    def status(self) -> list[dict]:
        """Return a snapshot of all supervised actors for dashboard/CLI."""
        result = []
        for name in self._order:
            spec = self._specs[name]
            actor = spec.actor
            result.append({
                "name":          name,
                "strategy":      spec.strategy.value,
                "max_restarts":  spec.max_restarts,
                "restarts_used": len(spec._restart_times),
                "exhausted":     spec.exhausted,
                "actor_state":   actor.state.value if actor else "none",
                "actor_id":      actor.actor_id[:8] if actor else None,
            })
        return result

    async def stop(self):
        if self._watch_task:
            self._watch_task.cancel()
        async with self._lock:
            for name in reversed(self._order):
                await self._stop_actor(name, self._specs[name])
        logger.info("[Supervisor] Stopped.")


class ActorSystem:
    """Top-level orchestrator."""

    def __init__(self, mqtt_broker: str = "localhost", mqtt_port: int = 1883,
                 state_dir: str = "./state"):
        self.registry     = ActorRegistry()
        self._mqtt_broker = mqtt_broker
        self._mqtt_port   = mqtt_port
        self._mqtt_client = None
        self._running     = False
        self._supervisor: Optional[Supervisor] = None
        self._state_dir   = state_dir

    def _inject(self, actor: Actor):
        """Inject MQTT client + broker/port into an actor so it can publish and subscribe."""
        actor._mqtt_client = self._mqtt_client
        actor._mqtt_broker = self._mqtt_broker
        actor._mqtt_port   = self._mqtt_port

    @property
    def supervisor(self) -> Supervisor:
        """Lazy-create the Supervisor bound to this system's registry and inject function."""
        if self._supervisor is None:
            self._supervisor = Supervisor(self.registry, self._inject)
        return self._supervisor

    def mqtt_status(self) -> dict:
        """Return current MQTT publisher health — useful for dashboard and /nodes."""
        if self._mqtt_client is None:
            return {"connected": False, "queue_depth": 0, "available": False}
        return {
            "connected":   getattr(self._mqtt_client, "connected", False),
            "queue_depth": getattr(self._mqtt_client, "queue_depth", 0),
            "available":   getattr(self._mqtt_client, "_available", False),
            "client_id":   getattr(self._mqtt_client, "_client_id", "?"),
        }

    async def start(self, *initial_actors: Actor):
        self._running = True
        import os
        os.makedirs(self._state_dir, exist_ok=True)
        db_path = os.path.join(self._state_dir, "mqtt_outbox.db")
        self._mqtt_client = await _MQTTPublisher.create(
            self._mqtt_broker, self._mqtt_port, db_path=db_path
        )

        # ── Initialise TopicBus (reactive pub/sub coordination layer) ─────
        from .topic_bus import init_topic_bus
        self.topic_bus = init_topic_bus(
            mqtt_client  = self._mqtt_client,
            mqtt_broker  = self._mqtt_broker,
            mqtt_port    = self._mqtt_port,
        )
        logger.info("[ActorSystem] TopicBus initialised")

        for actor in initial_actors:
            self._inject(actor)
            await self.registry.register(actor)
            await actor.start()

        logger.info(f"[ActorSystem] Started with {len(initial_actors)} actors.")

    async def spawn(self, actor_class: Type[Actor], **kwargs) -> Actor:
        """Spawn and register a new actor in the system."""
        actor = actor_class(**kwargs)
        self._inject(actor)
        await self.registry.register(actor)
        await actor.start()
        return actor

    async def stop_all(self):
        self._running = False
        # Stop supervisor first so it doesn't try to restart actors we're about to stop
        if self._supervisor:
            await self._supervisor.stop()
        actors = self.registry.all_actors()
        await asyncio.gather(*[a.stop() for a in actors], return_exceptions=True)
        if self._mqtt_client:
            await self._mqtt_client.disconnect()
        logger.info("[ActorSystem] All actors stopped.")

    async def run_forever(self):
        try:
            while self._running:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("[ActorSystem] Shutdown signal received.")
            await self.stop_all()


class _MQTTPublisher:
    """
    Reliable async MQTT publisher with:
      - Persistent in-memory outbox queue (messages survive reconnects)
      - SQLite-backed durable outbox (messages survive process crashes)
      - clean_session=False + fixed client_id (broker holds QoS 1 messages)
      - QoS 1 for critical messages, QoS 0 for telemetry
      - Automatic reconnection with exponential backoff
      - Never blocks callers — publish() always returns immediately

    Message priority:
      qos=1  → goes to durable SQLite outbox, guaranteed delivery
      qos=0  → in-memory only, dropped if disconnected (telemetry/logs)
      retain → stored at broker, replayed to new subscribers
    """

    # Topics that must use QoS 1 regardless of caller setting
    _CRITICAL_TOPIC_PREFIXES = (
        "nodes/",        # spawn, stop, desired_state
        "agents/by-name/",  # task routing
    )
    # Topics that are purely telemetry — always QoS 0 to avoid queue bloat
    _TELEMETRY_TOPIC_SUFFIXES = (
        "/logs",
        "/metrics",
        "/status",
        "/heartbeat",
    )

    def __init__(self, db_path: str = "./state/mqtt_outbox.db"):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._available = False
        self._db_path = db_path
        self._client_id = f"wactorz-publisher"
        self._connected = False

    @classmethod
    async def create(cls, broker: str, port: int,
                     db_path: str = "./state/mqtt_outbox.db") -> "_MQTTPublisher":
        pub = cls(db_path=db_path)
        try:
            import aiomqtt  # noqa
            pub._init_db()
            pub._load_pending_from_db()
            pub._task = asyncio.create_task(pub._run(broker, port))
            pub._available = True
            logger.info(f"[MQTT] Publisher started → {broker}:{port} | "
                        f"client_id={pub._client_id} | outbox_db={db_path}")
        except ImportError:
            logger.warning("[MQTT] aiomqtt not installed. MQTT disabled.")
        except Exception as e:
            logger.warning(f"[MQTT] Publisher unavailable: {e}")
        return pub

    # ── SQLite outbox ──────────────────────────────────────────────────────

    def _init_db(self):
        """Create outbox table if it doesn't exist."""
        import sqlite3, os
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        with sqlite3.connect(self._db_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS outbox (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic   TEXT    NOT NULL,
                    payload TEXT    NOT NULL,
                    retain  INTEGER NOT NULL DEFAULT 0,
                    qos     INTEGER NOT NULL DEFAULT 1,
                    ts      REAL    NOT NULL
                )
            """)
            db.commit()

    def _save_to_db(self, topic: str, payload: str, retain: bool, qos: int) -> int:
        """Persist a message to SQLite. Returns row id."""
        import sqlite3, time as _t
        try:
            with sqlite3.connect(self._db_path) as db:
                cur = db.execute(
                    "INSERT INTO outbox (topic, payload, retain, qos, ts) VALUES (?,?,?,?,?)",
                    (topic, payload if isinstance(payload, str) else payload.decode("utf-8", errors="replace"),
                     int(retain), qos, _t.time())
                )
                db.commit()
                return cur.lastrowid
        except Exception as e:
            logger.debug(f"[MQTT] Outbox write failed: {e}")
            return -1

    def _delete_from_db(self, row_id: int):
        """Remove a delivered message from the outbox."""
        import sqlite3
        try:
            with sqlite3.connect(self._db_path) as db:
                db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
                db.commit()
        except Exception as e:
            logger.debug(f"[MQTT] Outbox delete failed: {e}")

    def _load_pending_from_db(self):
        """On startup, reload undelivered QoS 1 messages into the in-memory queue."""
        import sqlite3
        try:
            with sqlite3.connect(self._db_path) as db:
                rows = db.execute(
                    "SELECT id, topic, payload, retain, qos FROM outbox ORDER BY id"
                ).fetchall()
            if rows:
                logger.info(f"[MQTT] Replaying {len(rows)} undelivered message(s) from outbox")
            for row_id, topic, payload, retain, qos in rows:
                self._queue.put_nowait((topic, payload, bool(retain), qos, row_id))
        except Exception as e:
            logger.debug(f"[MQTT] Outbox load failed: {e}")

    # ── Public API ─────────────────────────────────────────────────────────

    async def publish(self, topic: str, payload, retain: bool = False, qos: int = 0):
        if not self._available:
            return

        # Auto-upgrade critical topics to QoS 1
        if any(topic.startswith(p) for p in self._CRITICAL_TOPIC_PREFIXES):
            qos = max(qos, 1)

        # Auto-downgrade telemetry to QoS 0 (avoid queue bloat)
        if any(topic.endswith(s) for s in self._TELEMETRY_TOPIC_SUFFIXES):
            qos = 0

        if qos >= 1:
            # Durable: persist to SQLite first, then enqueue
            row_id = self._save_to_db(topic, payload if isinstance(payload, str)
                                      else payload, retain, qos)
            await self._queue.put((topic, payload, retain, qos, row_id))
        else:
            # Best-effort: in-memory only
            await self._queue.put((topic, payload, retain, qos, -1))

    async def disconnect(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ── Background drain loop ──────────────────────────────────────────────

    async def _run(self, broker: str, port: int):
        """
        Background loop: maintain persistent MQTT connection and drain the outbox.
        - clean_session=False: broker holds subscriptions + QoS 1 messages across reconnects
        - Fixed client_id: same session resumed after reconnect
        - Messages are NOT dequeued until successfully published (no loss on disconnect)
        """
        import aiomqtt
        backoff = 1.0

        while True:
            try:
                async with aiomqtt.Client(
                    broker, port,
                    identifier   = self._client_id,
                    clean_session = False,
                    keepalive    = 30,
                ) as client:
                    self._connected = True
                    backoff = 1.0  # reset backoff on successful connect
                    logger.info(f"[MQTT] Publisher connected | client_id={self._client_id}")

                    while True:
                        # Peek at item without removing from queue
                        item = await self._queue.get()
                        topic, payload, retain, qos, row_id = item

                        try:
                            await client.publish(
                                topic, payload, retain=retain, qos=qos
                            )
                            # Only remove from queue AFTER successful publish
                            self._queue.task_done()
                            # Remove from SQLite outbox if it was persisted
                            if row_id >= 0:
                                self._delete_from_db(row_id)
                        except Exception as pub_err:
                            # Put back at front of queue and reconnect
                            logger.warning(f"[MQTT] Publish failed: {pub_err} — requeueing")
                            await self._queue.put(item)  # re-enqueue
                            self._queue.task_done()
                            raise  # trigger reconnect

            except asyncio.CancelledError:
                self._connected = False
                break
            except Exception as e:
                self._connected = False
                logger.warning(
                    f"[MQTT] Publisher disconnected: {e}. "
                    f"Reconnecting in {backoff:.1f}s... "
                    f"(queue depth: {self._queue.qsize()})"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)  # exponential backoff, cap at 30s


class _NoOpMQTT:
    async def publish(self, topic: str, payload: str):
        pass
    async def disconnect(self):
        pass