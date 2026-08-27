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
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .actor import Actor, ActorState, Message, MessageType, SupervisorStrategy
from .mqtt_publisher import MQTTPublisher
from .paths import resolve_state_dir

logger = logging.getLogger(__name__)


class ProtectedActorCollision(RuntimeError):
    """Registering an actor would have evicted a running protected one.

    Raised rather than logged and skipped: the caller has already built the
    actor and must not go on to start it, and a spawn that quietly did nothing
    would be indistinguishable from one that worked.
    """


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

    def __init__(self) -> None:
        self._actors: dict[str, Actor] = {}
        self._lock = asyncio.Lock()
        # Back-reference to the Supervisor — set by ActorSystem after creating both.
        # Allows Actor.spawn() to auto-register children under supervision.
        self._supervisor_ref: Supervisor | None = None

    async def register(self, actor: Actor) -> None:
        """Add an actor, replacing any earlier one holding the same id."""
        superseded: Actor | None = None
        async with self._lock:
            existing = self._actors.get(actor.actor_id)
            if existing is not None and existing is not actor:
                if getattr(existing, "protected", False):
                    # Because actor_id is a uuid5 of the name, an actor named
                    # after a system agent does not collide with it — it lands
                    # on the same id and the replacement below would stop the
                    # original. The orchestrator would go down and the spawn
                    # would report success. Callers reach this by any route, so
                    # the refusal belongs here rather than only at the spawn path.
                    raise ProtectedActorCollision(
                        f"'{actor.name}' resolves to the same actor id as the running "
                        f"protected actor '{existing.name}' ({actor.actor_id[:8]}); "
                        f"registering it would stop the original. Use a different name."
                    )
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
                # Stopped outside this lock to avoid re-entrancy: stop()
                # acquires no shared lock but may await tasks that do.
                superseded = existing
            actor._registry = self
            self._actors[actor.actor_id] = actor
            logger.info("[Registry] Registered %s (%s)", actor.name, actor.actor_id[:8])

        if superseded is not None:
            # Awaited, not fired and forgotten. `create_task` here kept no
            # reference, so the task could be garbage-collected mid-stop, and an
            # exception inside it went to nobody — the failure mode being the one
            # this code exists to prevent: an old instance still subscribed, so
            # every published event is delivered twice.
            try:
                await superseded.stop()
            except Exception as exc:
                logger.error(
                    "[Registry] Stopping the superseded '%s' failed — its listeners may still be live: %s",
                    superseded.name,
                    exc,
                )

    async def unregister(self, actor_id: str) -> None:
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

    async def broadcast(self, sender_id: str, msg_type: MessageType, payload: Any = None) -> None:
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

    def __len__(self) -> int:
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
            if spec.actor is not None:
                # Cleared, or the actor goes on reporting supervised=True in
                # get_status() — and so to the dashboard and /api/actors — after
                # it has been deliberately released.
                spec.actor.supervisor_id = None
            spec.retired = True
            spec.actor = None
            logger.info("[Supervisor] Released '%s' from supervision (intentional stop).", name)

    def resupervise(self, name: str, actor: Actor) -> None:
        """Put a released actor back under supervision — the 'link' to release().

        A deliberate stop retires the spec, which is what keeps the watchdog from
        undoing it. Starting the actor again therefore has to say so explicitly,
        or it would run unsupervised: crashing without being restarted, and
        without anyone being told.

        A no-op for a name that is not supervised, matching release().
        """
        spec = self._specs.get(name)
        if spec is None:
            return
        spec.retired = False
        spec.actor = actor
        actor.supervisor_id = str(id(self))
        # The old crashes are not this run's. Leaving them counted means an actor
        # started after a rough patch gets a fraction of a restart budget.
        spec._restart_times.clear()
        logger.info("[Supervisor] '%s' is supervised again.", name)

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self):
        """Spawn all supervised actors and start the watch loop."""
        for name in self._order:
            spec = self._specs[name]
            actor = await self._spawn_actor(name, spec)
            spec.actor = actor

        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info("[Supervisor] Started. Supervising: %s", list(self._specs))

    # ── Watchdog thresholds ───────────────────────────────────────────────────
    # An actor is considered "silent" if its heartbeat is older than this.
    # (Actor heartbeats every 10s by default; allow 3× grace period.)
    HEARTBEAT_TIMEOUT = 35.0  # seconds without a heartbeat → treat as crashed
    # An actor that has accumulated this many errors is "storming" — restart it.
    ERROR_STORM_THRESHOLD = 10  # cumulative errors within the actor's lifetime

    async def _watch_loop(self):
        """Poll supervised actors for failure and trigger restarts.

        Detection runs under the lock; the restart itself does not. Restarting
        means waiting out ``restart_delay``, stopping an actor and starting a
        new one — holding the lock across all of that made one slow actor stall
        supervision of every other, and blocked anything else that needed it.
        """
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                for name, spec in await self._detect_failures():
                    await self._supervise_one(name, spec)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[Supervisor] watch_loop error: %s", exc, exc_info=True)

    def _failure_reason(self, spec: SupervisedSpec) -> str | None:
        """Why this spec needs supervision, or None if there is nothing to do.

        Three Erlang-style failure modes, plus the case of a spec that should
        have an actor and does not. Reads state without changing any, so it can
        be asked again later to confirm the answer still holds.
        """
        if spec.retired:
            return None

        actor = spec.actor
        if actor is None:
            # Should be running and is not: a respawn failed, or never ran.
            return "no running actor"

        # A deliberate stop or delete, which is not a crash.
        if actor.state == ActorState.STOPPED:
            return None

        if actor.state == ActorState.FAILED:
            return f"state is FAILED — applying {spec.strategy.value}"

        # Give a freshly started actor twice the timeout to warm up.
        if actor.metrics.uptime > self.HEARTBEAT_TIMEOUT * 2:
            silence = time.time() - actor.metrics.last_heartbeat
            if silence > self.HEARTBEAT_TIMEOUT:
                return (
                    f"last heartbeat {silence:.0f}s ago "
                    f"(threshold {self.HEARTBEAT_TIMEOUT}s) — presumed crashed"
                )

        if actor.metrics.errors >= self.ERROR_STORM_THRESHOLD:
            return (
                f"{actor.metrics.errors} errors "
                f"(threshold {self.ERROR_STORM_THRESHOLD}) — error storm"
            )

        return None

    async def _detect_failures(self) -> list[tuple[str, SupervisedSpec]]:
        """Specs needing attention this cycle. Brief, and the only locked part."""
        async with self._lock:
            return [
                (name, spec)
                for name, spec in list(self._specs.items())
                if self._failure_reason(spec) is not None
            ]

    async def _supervise_one(self, name: str, spec: SupervisedSpec) -> None:
        """Act on one detected failure, having confirmed it is still real."""
        async with self._lock:
            # The lock was released between detection and now, so the spec may
            # have been retired, replaced, or have recovered on its own.
            if self._specs.get(name) is not spec:
                return
            why = self._failure_reason(spec)
            if why is None:
                return
            actor = spec.actor
            if actor is not None:
                actor.state = ActorState.FAILED

        logger.warning("[Supervisor] '%s': %s.", name, why)
        await self._apply_strategy(name, spec)

    # ── Strategy application ─────────────────────────────────────────────────

    async def _apply_strategy(self, crashed_name: str, crashed_spec: SupervisedSpec):
        """Restart the siblings the strategy calls for, skipping retired specs.

        A retired spec is one that was deliberately released — stopped, deleted,
        or given up on after exhausting its budget. Restarting it because a
        *different* actor crashed would undo a decision someone made on purpose.
        """
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
                spec = self._specs[name]
                if spec.retired:
                    continue
                await self._restart_one(name, spec)

        elif crashed_spec.strategy == SupervisorStrategy.REST_FOR_ONE:
            idx = self._order.index(crashed_name)
            affected = self._order[idx:]  # crashed + everyone registered after it
            logger.info("[Supervisor] REST_FOR_ONE — restarting: %s", affected)
            for name in reversed(affected):
                spec = self._specs[name]
                if spec.actor and name != crashed_name:
                    await self._stop_actor(name, spec)
            for name in affected:
                spec = self._specs[name]
                if spec.retired:
                    continue
                await self._restart_one(name, spec)

    # ── Individual restart ────────────────────────────────────────────────────

    async def _retire(self, name: str, spec: SupervisedSpec, why: str) -> None:
        """Stop supervising an actor for good, and say so where someone will see it.

        Retiring is the end of the line for a spec: nothing restarts it
        afterwards. Doing that silently leaves an agent absent from the system
        with no account of why, which is only discovered when someone notices
        the work it was doing has stopped.
        """
        # The watch loop skips retired specs, so this also stops the same
        # critical message repeating on every poll.
        spec.retired = True
        # _stop_actor clears spec.actor; dropping the reference without stopping
        # the actor first leaves its tasks running with nothing left to stop them.
        await self._stop_actor(name, spec)
        logger.critical("[Supervisor] Retiring '%s': %s. Manual intervention required.", name, why)
        await self._notify_main(
            f"🚨 **{name}** has crashed {spec.max_restarts} times and the Supervisor has given up. "
            f"It is permanently stopped. Delete it and spawn a new one, or fix its code.",
            severity="critical",
        )

    async def _restart_one(self, name: str, spec: SupervisedSpec):
        if spec.exhausted:
            await self._retire(
                name,
                spec,
                f"exhausted its restart budget ({spec.max_restarts} restarts "
                f"/ {spec.restart_window}s)",
            )
            return

        if not spec.record_restart():
            await self._retire(name, spec, "restart budget exceeded")
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

        # Spawn a fresh one. _stop_actor above has already cleared spec.actor, so
        # a failure here leaves the spec with no actor — and the watch loop's own
        # error handler only logs. Without catching it the spec sat at None
        # forever, skipped on every subsequent poll: one bad factory call and the
        # agent was gone for the life of the process, with a single log line.
        try:
            new_actor = await self._spawn_actor(name, spec)
        except Exception as exc:
            spec.actor = None
            logger.error("[Supervisor] Respawn of '%s' failed: %s", name, exc, exc_info=True)
            if spec.exhausted:
                await self._retire(name, spec, "every restart attempt failed to start it")
            # Otherwise the spec keeps its actor at None, which the watch loop
            # now reads as "should be running but isn't" and tries again — each
            # attempt costing budget, so a persistent failure ends rather than
            # retrying forever.
            return
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
        if self._watch_task and self._watch_task is not asyncio.current_task():
            # Awaited, not just cancelled: restarts now run outside the lock, so
            # without waiting for the loop to actually stop, a restart already in
            # flight would finish afterwards and register a fresh actor into a
            # system that has just been shut down.
            self._watch_task.cancel()
            # gather rather than a bare await: the watch loop's own
            # CancelledError is returned as a value, so ignoring it cannot also
            # swallow a cancellation aimed at the caller of stop().
            (outcome,) = await asyncio.gather(self._watch_task, return_exceptions=True)
            # Reported, not dropped. gather *retrieves* the exception, which also
            # suppresses asyncio's "never retrieved" warning — so a watch loop
            # that died of something real would vanish silently at shutdown.
            # CancelledError is a BaseException, so this is a real crash only.
            if isinstance(outcome, Exception):
                logger.error("[Supervisor] watch loop ended in error: %s", outcome)
            self._watch_task = None
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
