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

    def __init__(self, mqtt_broker: str = "localhost", mqtt_port: int = 1883):
        self.registry     = ActorRegistry()
        self._mqtt_broker = mqtt_broker
        self._mqtt_port   = mqtt_port
        self._mqtt_client = None
        self._running     = False
        self._supervisor: Optional[Supervisor] = None

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

    async def start(self, *initial_actors: Actor):
        self._running = True
        self._mqtt_client = await _MQTTPublisher.create(self._mqtt_broker, self._mqtt_port)

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
    Persistent async MQTT publisher using a background task + queue.
    Automatically reconnects on failure. Never blocks callers.
    """

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._available = False

    @classmethod
    async def create(cls, broker: str, port: int) -> "_MQTTPublisher":
        pub = cls()
        try:
            import aiomqtt  # noqa - check installed
            pub._task = asyncio.create_task(pub._run(broker, port))
            pub._available = True
            logger.info(f"[ActorSystem] MQTT publisher started -> {broker}:{port}")
        except ImportError:
            logger.warning("[ActorSystem] aiomqtt not installed. MQTT disabled.")
        except Exception as e:
            logger.warning(f"[ActorSystem] MQTT unavailable: {e}")
        return pub

    async def publish(self, topic: str, payload: str):
        if self._available:
            await self._queue.put((topic, payload))

    async def disconnect(self):
        if self._task:
            self._task.cancel()

    async def _run(self, broker: str, port: int):
        """Background loop: hold connection, drain publish queue."""
        import aiomqtt
        while True:
            try:
                async with aiomqtt.Client(broker, port) as client:
                    logger.info("[MQTT] Publisher connected.")
                    while True:
                        topic, payload = await self._queue.get()
                        await client.publish(topic, payload)
                        self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[MQTT] Publisher disconnected: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3)


class _NoOpMQTT:
    async def publish(self, topic: str, payload: str):
        pass
    async def disconnect(self):
        pass