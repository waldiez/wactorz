"""ActorRegistry - Central registry and message router for all actors.
ActorSystem orchestrates startup, shutdown, and actor lifecycle.
Supervisor implements Erlang/OTP-style supervision trees.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .actor import Actor, Message, MessageType, SupervisorStrategy
from .paths import resolve_state_dir

logger = logging.getLogger(__name__)


# ── Supervision spec ──────────────────────────────────────────────────────────


@dataclass
class SupervisedSpec:
    """Descriptor for one actor under supervision.

    factory      : zero-arg async callable that creates and returns a fresh
                   Actor instance (already injected with MQTT / registry).
    strategy     : how to react when THIS actor crashes.
    max_restarts : max restarts within restart_window seconds before giving up.
    restart_window: sliding window in seconds for max_restarts accounting.
    restart_delay : seconds to wait before restarting (lets dependencies settle).
    """

    factory: Callable[[], Actor | Awaitable[Actor]]
    strategy: SupervisorStrategy = SupervisorStrategy.ONE_FOR_ONE
    max_restarts: int = 5
    restart_window: float = 60.0
    restart_delay: float = 1.0

    # Runtime state — managed by Supervisor, not set by caller
    actor: Actor | None = field(default=None, repr=False)
    _restart_times: list = field(default_factory=list, repr=False)
    # Set to True when an actor is intentionally stopped/deleted by the user,
    # or when it has exhausted its restart budget.  The watch_loop skips retired specs.
    retired: bool = field(default=False, repr=False)

    def record_restart(self) -> bool:
        """Record a restart attempt. Returns True if still within budget."""
        now = time.time()
        cutoff = now - self.restart_window
        self._restart_times = [t for t in self._restart_times if t > cutoff]
        self._restart_times.append(now)
        return len(self._restart_times) <= self.max_restarts

    @property
    def exhausted(self) -> bool:
        """Whether the restart budget for the current window is spent."""
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
        # Back-reference to the Supervisor — set by ActorSystem after creating both.
        # Allows Actor.spawn() to auto-register children under supervision.
        self._supervisor_ref: Supervisor | None = None

    async def register(self, actor: Actor):
        """Add an actor, replacing any earlier one holding the same id."""
        async with self._lock:
            existing = self._actors.get(actor.actor_id)
            if existing is not None and existing is not actor:
                # Same deterministic actor_id (uuid5 of name) is being re-registered.
                # The old instance's tasks (message loop, heartbeat loop, aiomqtt
                # subscribe listeners spawned from setup()) are STILL RUNNING.
                # If we just overwrite the dict entry, the old listener stays alive
                # and we get duplicate MQTT message delivery — every published event
                # invokes both callbacks. Stop the old instance asynchronously so
                # its background tasks (and any aiomqtt subscriptions) shut down.
                logger.warning(
                    "[Registry] Overwriting existing actor '%s' (%s) — stopping old instance to prevent duplicate listeners / double MQTT delivery.",
                    existing.name,
                    actor.actor_id[:8],
                )
                # Schedule stop outside this lock to avoid re-entrancy / deadlock.
                # stop() acquires no shared locks but may await on tasks that do.
                asyncio.create_task(existing.stop())
            actor._registry = self
            self._actors[actor.actor_id] = actor
            logger.info("[Registry] Registered %s (%s)", actor.name, actor.actor_id[:8])

    async def unregister(self, actor_id: str):
        """Remove an actor. Does not stop it — the caller owns that."""
        async with self._lock:
            if actor_id in self._actors:
                del self._actors[actor_id]
                logger.info("[Registry] Unregistered %s", actor_id[:8])

    async def deliver(self, target_id: str, msg: Message) -> bool:
        """Put a message in one actor's mailbox. False if no such actor."""
        actor = self._actors.get(target_id)
        if actor is None:
            logger.warning("[Registry] Unknown target: %s", target_id[:8])
            return False
        await actor.receive(msg)
        return True

    async def broadcast(self, sender_id: str, msg_type: MessageType, payload=None):
        """Send a message to every registered actor except the sender."""
        msg = Message(type=msg_type, sender_id=sender_id, payload=payload)
        for actor_id, actor in list(self._actors.items()):
            if actor_id != sender_id:
                await actor.receive(msg)

    def get(self, actor_id: str) -> Actor | None:
        """The actor with this id, or None."""
        return self._actors.get(actor_id)

    def all_actors(self) -> list[Actor]:
        """Every registered actor."""
        return list(self._actors.values())

    def find_by_name(self, name: str) -> Actor | None:
        """The first actor with this name, or None. Names are not unique."""
        for actor in self._actors.values():
            if actor.name == name:
                return actor
        return None

    def __len__(self):
        return len(self._actors)


class Supervisor:
    """OTP-inspired supervision tree node.

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

    def __init__(
        self,
        registry: ActorRegistry,
        inject_fn: Callable[[Actor], None],
        poll_interval: float = 2.0,
    ):
        self._registry = registry
        self._inject = inject_fn  # sets MQTT client + broker/port on actor
        self._poll_interval = poll_interval  # seconds between liveness checks
        self._specs: dict[str, SupervisedSpec] = {}  # name → spec (ordered)
        self._order: list[str] = []  # insertion order for REST_FOR_ONE
        self._watch_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ── Registration ──────────────────────────────────────────────────────────

    def supervise(
        self,
        name: str,
        factory: Callable[[], Actor | Awaitable[Actor]],
        strategy: SupervisorStrategy = SupervisorStrategy.ONE_FOR_ONE,
        max_restarts: int = 5,
        restart_window: float = 60.0,
        restart_delay: float = 1.0,
    ) -> Supervisor:
        """Register an actor to be supervised. Call before start()."""
        spec = SupervisedSpec(
            factory=factory,
            strategy=strategy,
            max_restarts=max_restarts,
            restart_window=restart_window,
            restart_delay=restart_delay,
        )
        self._specs[name] = spec
        self._order.append(name)
        return self  # fluent

    def release(self, name: str):
        """Voluntarily remove an actor from supervision — the Erlang 'unlink' equivalent.

        Call this BEFORE sending a stop or delete command to an actor so the
        Supervisor doesn't race to restart it.  Safe to call even if the name
        is not currently supervised (no-op).

        This is the fix for Issue 2: stop/delete commands were setting state=STOPPED
        but the heartbeat-silence detector would fire 35s later and restart the actor
        anyway, because it didn't know the stop was intentional.
        """
        spec = self._specs.get(name)
        if spec is not None:
            spec.retired = True
            spec.actor = None
            logger.info("[Supervisor] Released '%s' from supervision (intentional stop).", name)

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self):
        """Spawn all supervised actors and start the watch loop."""
        for name in self._order:
            spec = self._specs[name]
            actor = await self._spawn_actor(name, spec)
            spec.actor = actor

        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info("[Supervisor] Started. Supervising: %s", list(self._specs))

    # ── Watch loop ────────────────────────────────────────────────────────────

    # ── Watchdog thresholds ───────────────────────────────────────────────────
    # An actor is considered "silent" if its heartbeat is older than this.
    # (Actor heartbeats every 10s by default; allow 3× grace period.)
    HEARTBEAT_TIMEOUT = 35.0  # seconds without a heartbeat → treat as crashed
    # An actor that has accumulated this many errors is "storming" — restart it.
    ERROR_STORM_THRESHOLD = 10  # cumulative errors within the actor's lifetime

    async def _watch_loop(self):
        """Poll supervised actors for failure and trigger restarts.

        Detects three Erlang-style failure modes:
        1. state == FAILED   — actor explicitly marked itself dead (compile/setup/process exhausted)
        2. Heartbeat silence — actor task is frozen/deadlocked and stopped updating metrics
        3. Error storm       — actor is alive but accumulating errors beyond a safe threshold
        """
        from .actor import ActorState

        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                async with self._lock:
                    for name, spec in list(self._specs.items()):
                        # ── Skip specs that are intentionally retired ──────────
                        # retired = user-stopped/deleted OR budget-exhausted.
                        # We never restart these — that's the whole point.
                        if spec.retired:
                            continue

                        actor = spec.actor
                        if actor is None:
                            continue

                        # ── Skip actors that were intentionally stopped ─────────
                        # STOPPED means a deliberate stop/delete command was issued.
                        # PAUSED means the user explicitly paused it.
                        # Neither is a crash — do NOT restart them.
                        if actor.state in (ActorState.STOPPED, ActorState.PAUSED):
                            continue

                        # ── Mode 1: FAILED state ───────────────────────────────
                        if actor.state == ActorState.FAILED:
                            logger.warning(
                                "[Supervisor] '%s' is FAILED — applying %s strategy.",
                                name,
                                spec.strategy.value,
                            )
                            await self._apply_strategy(name, spec)
                            continue

                        # ── Mode 2: Heartbeat silence ──────────────────────────
                        # Skip actors that just started (give them 2× the timeout to warm up).
                        uptime = actor.metrics.uptime
                        if uptime > self.HEARTBEAT_TIMEOUT * 2:
                            silence = time.time() - actor.metrics.last_heartbeat
                            if silence > self.HEARTBEAT_TIMEOUT:
                                logger.warning(
                                    "[Supervisor] '%s' last heartbeat was %.0fs ago "
                                    "(threshold=%ss) — presumed crashed. "
                                    "Forcing FAILED and restarting.",
                                    name,
                                    silence,
                                    self.HEARTBEAT_TIMEOUT,
                                )
                                actor.state = ActorState.FAILED
                                await self._apply_strategy(name, spec)
                                continue

                        # ── Mode 3: Error storm ────────────────────────────────
                        if actor.metrics.errors >= self.ERROR_STORM_THRESHOLD:
                            logger.warning(
                                "[Supervisor] '%s' has %s errors (threshold=%s) — error storm detected. Forcing FAILED and restarting.",
                                name,
                                actor.metrics.errors,
                                self.ERROR_STORM_THRESHOLD,
                            )
                            actor.state = ActorState.FAILED
                            await self._apply_strategy(name, spec)
                            continue

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[Supervisor] watch_loop error: %s", exc, exc_info=True)

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
            affected = self._order[idx:]  # crashed + everyone registered after it
            logger.info("[Supervisor] REST_FOR_ONE — restarting: %s", affected)
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
                "[Supervisor] '%s' has exhausted its restart budget (%s restarts / %ss). Retiring — manual intervention required.",
                name,
                spec.max_restarts,
                spec.restart_window,
            )
            # Mark retired so the watch_loop stops polling this spec permanently.
            # Issue 1 fix: without this, the watch_loop would keep calling _restart_one
            # every poll cycle even after budget is gone, logging the same critical
            # message endlessly and potentially re-entering the restart path.
            spec.retired = True
            spec.actor = None
            await self._notify_main(
                f"🚨 **{name}** has crashed {spec.max_restarts} times and the Supervisor has given up. "
                f"It is permanently stopped. Delete it and spawn a new one, or fix its code.",
                severity="critical",
            )
            return

        within_budget = spec.record_restart()
        if not within_budget:
            spec.retired = True
            spec.actor = None
            return

        if spec.restart_delay > 0:
            await asyncio.sleep(spec.restart_delay)

        logger.info(
            "[Supervisor] Restarting '%s' (attempt %s/%s).",
            name,
            len(spec._restart_times),
            spec.max_restarts,
        )

        # Stop the old actor cleanly if possible
        if spec.actor:
            await self._stop_actor(name, spec)

        # Spawn a fresh one
        new_actor = await self._spawn_actor(name, spec)
        spec.actor = new_actor
        new_actor.metrics.restart_count = len(spec._restart_times)
        # Fresh start — reset error counter so error-storm detector doesn't
        # immediately re-trigger on the very first poll after restart.
        new_actor.metrics.errors = 0

        logger.info("[Supervisor] '%s' restarted successfully.", name)
        await self._notify_main(
            f"♻️ **{name}** crashed and was automatically restarted "
            f"(restart #{new_actor.metrics.restart_count} of {spec.max_restarts}). "
            f"It is running again.",
            severity="warning",
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _spawn_actor(self, name: str, spec: SupervisedSpec) -> Actor:
        """Create actor via factory, inject MQTT, register, and start."""
        # Ask the result, not the callable: iscoroutinefunction is False for an
        # async callable object, and for functools.partial of a coroutine
        # function before 3.12 — either would have been registered un-awaited,
        # putting a coroutine object where the supervisor expects an actor.
        made = spec.factory()
        actor = await made if inspect.isawaitable(made) else made
        self._inject(actor)
        actor.supervisor_id = str(id(self))
        await self._registry.register(actor)
        await actor.start()
        logger.debug("[Supervisor] Spawned '%s' (%s).", name, actor.actor_id[:8])
        return actor

    async def _stop_actor(self, name: str, spec: SupervisedSpec):
        """Stop an actor gracefully, unregister it, swallow errors."""
        actor = spec.actor
        if actor is None:
            return
        try:
            await actor.stop()
        except Exception as exc:
            logger.warning("[Supervisor] Error stopping '%s': %s", name, exc)
        try:
            await self._registry.unregister(actor.actor_id)
        except Exception:
            pass
        spec.actor = None

    async def _notify_main(self, message: str, severity: str = "critical"):
        """Send a supervision event to MainActor via the actor message queue.

        Uses MessageType.TASK with _monitor_notification=True so main's
        handle_message intercepts it and queues it in _pending_notifications —
        exactly the same path the Monitor uses.  This replaces the old direct
        object-mutation approach (main._pending_notifications.append(...)) which
        bypassed the message queue and could race with main's own loop.
        """
        try:
            if not self._registry:
                return
            main = self._registry.find_by_name("main")
            if main is None:
                return

            # Find any actor we can send from — use the first supervised actor,
            # or fall back to directly appending if none are available yet.
            sender = next(
                (
                    spec.actor
                    for spec in self._specs.values()
                    if spec.actor is not None and not spec.retired
                ),
                None,
            )

            if sender is not None:
                msg = Message(
                    type=MessageType.TASK,
                    sender_id=sender.actor_id,
                    payload={
                        "_monitor_notification": True,
                        "agent_name": "supervisor",
                        "message": message,
                        "severity": severity,
                        "timestamp": time.time(),
                    },
                    message_id=str(uuid.uuid4()),
                )
                await main.receive(msg)
            else:
                # No running actor to send from — fall back to direct append
                if hasattr(main, "_pending_notifications"):
                    main._pending_notifications.append(  # pyright: ignore[reportAttributeAccessIssue]
                        {
                            "severity": severity,
                            "message": message,
                            "source": "supervisor",
                            "timestamp": time.time(),
                        }
                    )
        except Exception as exc:
            logger.warning("[Supervisor] Could not notify main: %s", exc)

    # ── Introspection ─────────────────────────────────────────────────────────

    def status(self) -> list[dict]:
        """Return a snapshot of all supervised actors for dashboard/CLI."""
        result = []
        for name in self._order:
            spec = self._specs[name]
            actor = spec.actor
            result.append(
                {
                    "name": name,
                    "strategy": spec.strategy.value,
                    "max_restarts": spec.max_restarts,
                    "restarts_used": len(spec._restart_times),
                    "exhausted": spec.exhausted,
                    "retired": spec.retired,
                    "actor_state": actor.state.value if actor else "none",
                    "actor_id": actor.actor_id[:8] if actor else None,
                }
            )
        return result

    async def stop(self):
        """Stop supervising, then stop every supervised actor."""
        if self._watch_task:
            self._watch_task.cancel()
        async with self._lock:
            for name in reversed(self._order):
                await self._stop_actor(name, self._specs[name])
        logger.info("[Supervisor] Stopped.")


class ActorSystem:
    """Top-level orchestrator."""

    def __init__(
        self, mqtt_broker: str = "localhost", mqtt_port: int = 1883, state_dir: str | None = None
    ):
        self.registry = ActorRegistry()
        self._mqtt_broker = mqtt_broker
        self._mqtt_port = mqtt_port
        self._mqtt_client = None
        self._running = False
        self._supervisor: Supervisor | None = None
        self._state_dir = resolve_state_dir(state_dir)
        # Created in start(), which is the first point an MQTT client exists to
        # give it. Declared here so the attribute is not conjured into being
        # halfway through the object's life.
        self.topic_bus = None

    def _inject(self, actor: Actor):
        """Inject MQTT client + broker/port into an actor so it can publish and subscribe."""
        actor._mqtt_client = self._mqtt_client
        actor._mqtt_broker = self._mqtt_broker
        actor._mqtt_port = self._mqtt_port

    @property
    def supervisor(self) -> Supervisor:
        """Lazy-create the Supervisor bound to this system's registry and inject function."""
        if self._supervisor is None:
            self._supervisor = Supervisor(self.registry, self._inject)
            # Give the registry a back-reference so Actor.spawn() children are auto-supervised
            self.registry._supervisor_ref = self._supervisor
        return self._supervisor

    def mqtt_status(self) -> dict:
        """Return current MQTT publisher health — useful for dashboard and /nodes."""
        if self._mqtt_client is None:
            return {"connected": False, "queue_depth": 0, "available": False}
        return {
            "connected": getattr(self._mqtt_client, "connected", False),
            "queue_depth": getattr(self._mqtt_client, "queue_depth", 0),
            "available": getattr(self._mqtt_client, "_available", False),
            "client_id": getattr(self._mqtt_client, "_client_id", "?"),
        }

    async def start(self, *initial_actors: Actor):
        """Bring the system up: MQTT, topic bus, the given actors, supervision."""
        self._running = True

        os.makedirs(self._state_dir, exist_ok=True)
        db_path = os.path.join(self._state_dir, "mqtt_outbox.db")
        self._mqtt_client = await MQTTPublisher.create(
            self._mqtt_broker, self._mqtt_port, db_path=db_path
        )

        # ── Initialise TopicBus (reactive pub/sub coordination layer) ─────
        from .topic_bus import init_topic_bus

        self.topic_bus = init_topic_bus(
            mqtt_client=self._mqtt_client,
            mqtt_broker=self._mqtt_broker,
            mqtt_port=self._mqtt_port,
        )
        logger.info("[ActorSystem] TopicBus initialised")

        for actor in initial_actors:
            self._inject(actor)
            await self.registry.register(actor)
            await actor.start()

        logger.info("[ActorSystem] Started with %s actors.", len(initial_actors))

    async def spawn(self, actor_class: type[Actor], **kwargs) -> Actor:
        """Spawn and register a new actor in the system."""
        actor = actor_class(**kwargs)
        self._inject(actor)
        await self.registry.register(actor)
        await actor.start()
        return actor

    async def stop_all(self):
        """Shut everything down in reverse: supervisor, actors, then MQTT."""
        self._running = False
        # Stop supervisor first so it doesn't try to restart actors we're about to stop
        if self._supervisor:
            await self._supervisor.stop()
        actors = self.registry.all_actors()
        await asyncio.gather(*[a.stop() for a in actors], return_exceptions=True)
        if self._mqtt_client:
            await self._mqtt_client.disconnect()
            self._mqtt_client = None  # drop ref so GC can collect paho client now
        gc.collect()  # break aiomqtt↔paho ref cycle while loop is open
        logger.info("[ActorSystem] All actors stopped.")

    async def run_forever(self):
        """Block until the system is stopped or interrupted."""
        try:
            while self._running:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("[ActorSystem] Shutdown signal received.")
            await self.stop_all()


class MQTTPublisher:
    """Reliable async MQTT publisher with:
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
        "nodes/",  # spawn, stop, desired_state
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
        self._task: asyncio.Task | None = None
        self._available = False
        self._db_path = db_path
        self._client_id = "wactorz-publisher"
        self._connected = False

    @classmethod
    async def create(
        cls, broker: str, port: int, db_path: str = "./state/mqtt_outbox.db"
    ) -> MQTTPublisher:
        """Build a publisher and connect it, or return one that quietly no-ops."""
        pub = cls(db_path=db_path)
        try:
            import aiomqtt  # noqa: F401  # pylint: disable=unused-import

            pub._init_db()
            pub._load_pending_from_db()
            pub._task = asyncio.create_task(pub._run(broker, port))
            pub._available = True
            logger.info(
                "[MQTT] Publisher started → %s:%s | client_id=%s | outbox_db=%s",
                broker,
                port,
                pub._client_id,
                db_path,
            )
        except ImportError:
            logger.warning("[MQTT] aiomqtt not installed. MQTT disabled.")
        except Exception as e:
            logger.warning("[MQTT] Publisher unavailable: %s", e)
        return pub

    # ── SQLite outbox ──────────────────────────────────────────────────────

    def _init_db(self):
        """Create outbox table if it doesn't exist."""
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
        try:
            with sqlite3.connect(self._db_path) as db:
                cur = db.execute(
                    "INSERT INTO outbox (topic, payload, retain, qos, ts) VALUES (?,?,?,?,?)",
                    (
                        topic,
                        payload
                        if isinstance(payload, str)
                        else payload.decode("utf-8", errors="replace"),
                        int(retain),
                        qos,
                        time.time(),
                    ),
                )
                db.commit()
                return cur.lastrowid or 0
        except Exception as e:
            logger.debug("[MQTT] Outbox write failed: %s", e)
            return -1

    def _delete_from_db(self, row_id: int):
        """Remove a delivered message from the outbox."""
        try:
            with sqlite3.connect(self._db_path) as db:
                db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
                db.commit()
        except Exception as e:
            logger.debug("[MQTT] Outbox delete failed: %s", e)

    def _load_pending_from_db(self):
        """On startup, reload undelivered QoS 1 messages into the in-memory queue."""
        try:
            with sqlite3.connect(self._db_path) as db:
                rows = db.execute(
                    "SELECT id, topic, payload, retain, qos FROM outbox ORDER BY id"
                ).fetchall()
            if rows:
                logger.info("[MQTT] Replaying %s undelivered message(s) from outbox", len(rows))
            for row_id, topic, payload, retain, qos in rows:
                self._queue.put_nowait((topic, payload, bool(retain), qos, row_id))
        except Exception as e:
            logger.debug("[MQTT] Outbox load failed: %s", e)

    # ── Public API ─────────────────────────────────────────────────────────

    async def publish(self, topic: str, payload, retain: bool = False, qos: int = 0):
        """Queue a message for delivery. Returns without waiting for the broker."""
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
            row_id = self._save_to_db(topic, payload, retain, qos)
            await self._queue.put((topic, payload, retain, qos, row_id))
        else:
            # Best-effort: in-memory only
            await self._queue.put((topic, payload, retain, qos, -1))

    async def disconnect(self):
        """Stop the drain loop and close the connection."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def connected(self) -> bool:
        """Whether the broker connection is currently up."""
        return self._connected

    @property
    def queue_depth(self) -> int:
        """How many messages are waiting to be sent."""
        return self._queue.qsize()

    # ── Background drain loop ──────────────────────────────────────────────

    async def _run(self, broker: str, port: int):
        """Background loop: maintain persistent MQTT connection and drain the outbox.
        - clean_session=False: broker holds subscriptions + QoS 1 messages across reconnects
        - Fixed client_id: same session resumed after reconnect
        - Messages are NOT dequeued until successfully published (no loss on disconnect)
        """
        from .mqtt import mqtt_client  # local: avoids core/__init__ import cycle

        backoff = 1.0
        _last_exc_str: str | None = None

        while True:
            try:
                async with mqtt_client(
                    broker,
                    port,
                    identifier=self._client_id,
                    clean_session=False,
                    keepalive=30,
                ) as client:
                    self._connected = True
                    logger.info("[MQTT] Publisher connected | client_id=%s", self._client_id)

                    while True:
                        # Peek at item without removing from queue
                        item = await self._queue.get()
                        topic, payload, retain, qos, row_id = item

                        try:
                            await client.publish(topic, payload, retain=retain, qos=qos)
                            # Only remove from queue AFTER successful publish
                            self._queue.task_done()
                            # Remove from SQLite outbox if it was persisted
                            if row_id >= 0:
                                self._delete_from_db(row_id)
                            # Reset backoff and error dedup only after a successful publish
                            backoff = 1.0
                            _last_exc_str = None
                        except Exception as pub_err:
                            # Put back at front of queue and reconnect
                            logger.warning("[MQTT] Publish failed: %s — requeueing", pub_err)
                            await self._queue.put(item)  # re-enqueue
                            self._queue.task_done()
                            raise  # trigger reconnect

            except asyncio.CancelledError:
                self._connected = False
                break
            except Exception as e:
                self._connected = False
                exc_str = str(e)
                if exc_str != _last_exc_str:
                    logger.warning(
                        "[MQTT] Publisher disconnected: %s. "
                        "Reconnecting in %.1fs... "
                        "(queue depth: %d)",
                        e,
                        backoff,
                        self._queue.qsize(),
                    )
                    _last_exc_str = exc_str
                else:
                    logger.debug("[MQTT] Still disconnected — retrying in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)  # exponential backoff, cap at 30s
