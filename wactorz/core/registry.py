"""ActorRegistry - Central registry and message router for all actors.
ActorSystem orchestrates startup, shutdown, and actor lifecycle.
Supervisor implements Erlang/OTP-style supervision trees.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import inspect
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
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
    max_restarts : crashes in a row before restarts slow right down (see
                   `Supervisor.SLOW_RETRY_DELAY`). Nothing is given up on.
    restart_window: how long an actor must stay up for its crash streak to end.
    restart_delay : the wait before the first restart; each crash in a row
                    doubles it, up to `Supervisor.MAX_RESTART_DELAY`.
    """

    factory: Callable[[], Actor | Awaitable[Actor]]
    strategy: SupervisorStrategy = SupervisorStrategy.ONE_FOR_ONE
    max_restarts: int = 5
    restart_window: float = 60.0
    restart_delay: float = 1.0

    # Runtime state — managed by Supervisor, not set by caller
    actor: Actor | None = field(default=None, repr=False)
    # Set when an actor is deliberately stopped. The watch loop skips retired specs.
    retired: bool = field(default=False, repr=False)
    #: Crashes in a row, the latest included. Ends once an actor stays up for
    #: ``restart_window``; it is what the restart delay grows with.
    crash_streak: int = field(default=0, repr=False)
    #: Restarts made for this entry; what the actor's ``restart_count`` reports.
    restarts: int = field(default=0, repr=False)
    #: Whether restarts have slowed down, after ``max_restarts`` crashes in a row.
    slow: bool = field(default=False, repr=False)
    #: When the actor's errors were noticed, for the storm check's window.
    _error_times: list[float] = field(default_factory=list, repr=False)
    #: The actor's error count when last looked at, to tell how many are new.
    _errors_seen: int = field(default=0, repr=False)
    #: A restart waiting out its delay or under way. The watch loop leaves the
    #: spec alone until it ends, rather than starting a second one.
    _restart_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def reset_history(self) -> None:
        """Forget the crashes and errors of an actor this spec no longer runs."""
        self.crash_streak = 0
        self.slow = False
        self._error_times.clear()
        self._errors_seen = 0

    @property
    def restarting(self) -> bool:
        """Whether a restart is waiting or under way."""
        return self._restart_task is not None and not self._restart_task.done()


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
            except Exception:
                logger.exception(
                    "[Registry] Stopping the superseded '%s' failed — its listeners may still be live",
                    superseded.name,
                )

    async def unregister(self, actor_id: str) -> None:
        """Remove an actor. Does not stop it — the caller owns that."""
        async with self._lock:
            if actor_id in self._actors:
                del self._actors[actor_id]
                logger.info("[Registry] Unregistered %s", actor_id[:8])

    async def deliver(self, target_id: str, msg: Message) -> bool:
        """Put a message in one actor's mailbox. False if no such actor, or no room in it."""
        actor = self._actors.get(target_id)
        if actor is None:
            logger.warning("[Registry] Unknown target: %s", target_id[:8])
            return False
        return await actor.receive(msg)

    async def broadcast(self, sender_id: str, msg_type: MessageType, payload: Any = None) -> None:
        """Send a message to every registered actor except the sender.

        Each actor is given the wait a single delivery gets, all at once rather
        than one after another: an actor whose mailbox is full then costs the
        broadcast that wait once, however many of them there are.
        """
        msg = Message(type=msg_type, sender_id=sender_id, payload=payload)
        others = [actor for actor_id, actor in list(self._actors.items()) if actor_id != sender_id]
        await asyncio.gather(*(actor.receive(msg) for actor in others))

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

    def __bool__(self) -> bool:
        """A registry is true whenever it exists, however many actors it holds.

        Defining ``__len__`` alone makes an empty container false, while every
        ``if self._registry:`` in the codebase means "is there one". Read the
        other way, an empty registry would count as none, and `Actor.spawn`
        would neither register its first child nor put it under supervision.
        """
        return True


#: How many reports for main the supervisor keeps while main has no room for them.
HELD_REPORTS = 100


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
        #: Reports main's mailbox had no room for, oldest first, handed over
        #: again at each check. Bounded: a main that never reads must not be a
        #: way to grow this without limit, and the newest reports are the ones
        #: worth keeping.
        self._held_reports: deque[Message] = deque(maxlen=HELD_REPORTS)

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
        """Register an actor to be supervised. Call before start().

        A name already supervised is replaced in place: it keeps its position in
        ``_order``, which is what REST_FOR_ONE restarts by, and appears there once.
        """
        spec = SupervisedSpec(
            factory=factory,
            strategy=strategy,
            max_restarts=max_restarts,
            restart_window=restart_window,
            restart_delay=restart_delay,
        )
        # A restart still waiting on the entry being replaced would find it gone
        # and do nothing, but only after its delay -- and once the entry is
        # replaced, stop() cannot reach it to cancel it.
        replaced = self._specs.get(name)
        if replaced is not None:
            self._cancel_own_restart(replaced)
        self._specs[name] = spec
        if name not in self._order:
            self._order.append(name)
        return self  # fluent

    def adopt(
        self,
        name: str,
        factory: Callable[[], Actor | Awaitable[Actor]],
        actor: Actor,
        strategy: SupervisorStrategy = SupervisorStrategy.ONE_FOR_ONE,
        max_restarts: int = 5,
        restart_window: float = 60.0,
        restart_delay: float = 1.0,
    ) -> None:
        """Supervise ``actor``, which is already running, rebuilding it with ``factory``.

        For a child an actor has just spawned. A name seen before is re-armed
        rather than skipped: a spec retired by :meth:`release` -- an agent
        stopped, replaced with new code, migrated home, or deleted and spawned
        again -- would otherwise stay retired, and the new actor would crash and
        stay down with nobody told. The new factory replaces the old, whose
        closure rebuilt the actor as it used to be, and the restart history is
        the old actor's, not this one's.
        """
        self.supervise(name, factory, strategy, max_restarts, restart_window, restart_delay)
        spec = self._specs[name]
        spec.actor = actor
        actor.supervisor_id = str(id(self))

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
        # started after a rough patch starts out slow.
        spec.reset_history()
        logger.info("[Supervisor] '%s' is supervised again.", name)

    async def start_supervised(
        self,
        name: str,
        factory: Callable[[], Actor | Awaitable[Actor]],
        strategy: SupervisorStrategy = SupervisorStrategy.ONE_FOR_ONE,
        max_restarts: int = 5,
        restart_window: float = 60.0,
        restart_delay: float = 1.0,
    ) -> Actor:
        """Register an actor and start it now, rather than at :meth:`start`.

        For a caller whose actors arrive while it is already running — a node
        told to spawn an agent, rather than an app assembling a fixed tree at
        boot. A name that is already supervised is replaced: the spec keeps its
        place in ``_order``, so REST_FOR_ONE still restarts the right siblings.

        Raises whatever the factory raises, so the caller can report a spawn
        that did not start as a failure rather than finding it absent later.
        """
        self.supervise(name, factory, strategy, max_restarts, restart_window, restart_delay)
        spec = self._specs[name]
        # Held back from the watch loop while it starts. A spec with no actor
        # reads as "should be running and is not", and starting is not
        # instantaneous — a generated program's `on_start` compiles it and may
        # ask an LLM to repair it, which outlasts the poll interval easily. The
        # loop would spawn a second actor alongside the one still starting, and
        # spend restart budget doing it.
        spec.retired = True
        try:
            actor = await self._spawn_actor(name, spec)
        except BaseException:
            # The spec never held an actor, so there is nothing to stop and
            # nothing for the watch loop to act on. Dropped rather than left
            # retired, so the name is free for another attempt.
            self.drop_supervised(name)
            raise
        spec.actor = actor
        spec.retired = False
        return actor

    async def stop_supervised(self, name: str) -> None:
        """Stop an actor and forget it entirely — the undo of :meth:`start_supervised`.

        Stronger than :meth:`release`, which retires a spec but keeps it, so a
        later actor of the same name inherits its restart history. A node whose
        agents come and go on request needs the name free again.
        """
        spec = self._specs.get(name)
        self.drop_supervised(name)
        if spec is not None:
            await self._stop_actor(name, spec)

    def drop_supervised(self, name: str) -> None:
        """Forget a spec without stopping its actor.

        For an actor that has ended for good -- deleted, or ended itself:
        stopping it again is not wrong so much as misleading, and leaving the
        spec behind means the watch loop reads "should be running and is not"
        and starts it back up. Unlike :meth:`release`, nothing is kept: a
        retired spec holds its factory, and the factory's closure holds whatever
        the actor was built from, for as long as the process runs.
        """
        spec = self._specs.pop(name, None)
        if spec is None:
            return
        if name in self._order:
            self._order.remove(name)
        if spec.actor is not None:
            # Or it goes on reporting supervised=True, as with release().
            spec.actor.supervisor_id = None
        # A restart waiting out its delay would find the spec gone and do
        # nothing, but only after the delay -- and once the spec is out of
        # _specs, stop() cannot reach it to cancel it.
        self._cancel_own_restart(spec)
        logger.info("[Supervisor] Forgot '%s'.", name)

    def _cancel_own_restart(self, spec: SupervisedSpec) -> None:
        """Cancel a restart waiting on ``spec``, unless entries still here share it.

        A group strategy (ONE_FOR_ALL, REST_FOR_ONE) restarts its members in one
        task; cancelling it for one of them would abort the others' restarts as
        well. A shared one is left to run, and skips this entry when it reaches
        it, since the entry is no longer the live one for its name.
        """
        task = spec._restart_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        if any(other is not spec and other._restart_task is task for other in self._specs.values()):
            return
        task.cancel()

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self):
        """Spawn all supervised actors and start the watch loop."""
        for name, spec in self._entries():
            actor = await self._spawn_actor(name, spec)
            spec.actor = actor

        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info("[Supervisor] Started. Supervising: %s", list(self._specs))

    # ── Watchdog thresholds ───────────────────────────────────────────────────
    # An actor is considered "silent" if its heartbeat is older than this.
    # (Actor heartbeats every 10s by default; allow 3× grace period.)
    HEARTBEAT_TIMEOUT = 35.0  # seconds without a heartbeat → treat as crashed
    # This many errors within ERROR_STORM_WINDOW is a storm — restart the actor.
    # Over a window rather than a lifetime: an agent with the odd flaky poll
    # would otherwise be restarted, sooner or later, for errors it handled itself.
    ERROR_STORM_THRESHOLD = 10
    ERROR_STORM_WINDOW = 60.0  # seconds
    # The longest wait between restarts while an actor is still expected back.
    MAX_RESTART_DELAY = 60.0  # seconds
    # After max_restarts crashes in a row, restarts slow to this, doubling to
    # MAX_SLOW_RETRY_DELAY. Never stopping: an outage of what an agent depends on
    # ends, and a retired agent would stay down until someone noticed. Slowing:
    # a restart is not free -- a generated agent may ask the LLM to repair itself.
    SLOW_RETRY_DELAY = 300.0  # seconds
    MAX_SLOW_RETRY_DELAY = 3600.0  # seconds

    async def _watch_loop(self):
        """Poll supervised actors for failure and start their restarts.

        Detection runs under the lock; restarts run as tasks of their own. A
        restart waits out its delay -- up to an hour for an actor that keeps
        crashing -- and awaiting it here would leave every other actor
        unwatched for as long.
        """
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                self._hand_over_held_reports()
                failures, recovered = await self._detect_failures()
                for name in recovered:
                    await self._notify_main(
                        f"✅ **{name}** has stayed up since its last restart; it is back to "
                        "normal supervision.",
                        severity="info",
                    )
                for name, spec in failures:
                    self._schedule_restart(name, spec)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[Supervisor] watch_loop error")

    def _schedule_restart(self, name: str, spec: SupervisedSpec) -> None:
        """Handle one detected failure in a task of its own."""
        spec._restart_task = asyncio.create_task(
            self._supervise_one(name, spec), name=f"supervise-{name}"
        )

    def slow_retrying(self) -> list[str]:
        """Names of the actors whose restarts have slowed down after repeated crashes."""
        return [name for name, spec in self._entries() if spec.slow and not spec.retired]

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

        if len(spec._error_times) >= self.ERROR_STORM_THRESHOLD:
            return (
                f"{len(spec._error_times)} errors in {self.ERROR_STORM_WINDOW:.0f}s "
                f"(threshold {self.ERROR_STORM_THRESHOLD}) — error storm"
            )

        return None

    def _note_health(self, spec: SupervisedSpec, now: float) -> bool:
        """Update a spec's error window and crash streak. True if it just recovered.

        The bookkeeping `_failure_reason` reads, kept apart from it so that one
        stays a question that can be asked twice.
        """
        actor = spec.actor
        if actor is None or spec.retired or spec.restarting:
            return False
        errors = actor.metrics.errors
        # Lower than last time means the count was reset: all of it is new.
        new = errors - spec._errors_seen if errors >= spec._errors_seen else errors
        spec._errors_seen = errors
        if new > 0:
            spec._error_times.extend([now] * min(new, self.ERROR_STORM_THRESHOLD))
        cutoff = now - self.ERROR_STORM_WINDOW
        spec._error_times = [t for t in spec._error_times if t > cutoff]

        if spec.crash_streak and actor.metrics.uptime >= self._recovery_time(spec):
            was_slow = spec.slow
            spec.crash_streak = 0
            spec.slow = False
            return was_slow
        return False

    async def _detect_failures(self) -> tuple[list[tuple[str, SupervisedSpec]], list[str]]:
        """Specs needing a restart this cycle, and the names that just recovered.

        Brief, and the only locked part. A spec with a restart already pending
        is left to it.
        """
        now = time.time()
        async with self._lock:
            recovered = [
                name for name, spec in list(self._specs.items()) if self._note_health(spec, now)
            ]
            failures = [
                (name, spec)
                for name, spec in list(self._specs.items())
                if not spec.restarting and self._failure_reason(spec) is not None
            ]
            return failures, recovered

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
        """Restart the actors the strategy calls for.

        Skipped: retired specs -- deliberately stopped or deleted, and restarting
        one because a *different* actor crashed would undo a decision someone
        made on purpose -- and siblings with a restart of their own already
        pending, which that restart will see to. Only the actor that crashed
        counts it as a crash; its siblings are restarted without adding to
        their streaks.
        """
        if crashed_spec.strategy == SupervisorStrategy.ONE_FOR_ONE:
            names = [crashed_name]
        elif crashed_spec.strategy == SupervisorStrategy.ONE_FOR_ALL:
            logger.info("[Supervisor] ONE_FOR_ALL — restarting all supervised actors.")
            names = list(self._order)
        else:  # REST_FOR_ONE
            if crashed_name not in self._order:
                # Forgotten between detection and now: nothing left to restart.
                return
            names = self._order[self._order.index(crashed_name) :]
            logger.info("[Supervisor] REST_FOR_ONE — restarting: %s", names)

        group = [
            (name, spec)
            for name, spec in self._entries(names)
            if not spec.retired and (name == crashed_name or not spec.restarting)
        ]
        # Claimed for as long as this runs. A sibling sits stopped, with no
        # actor, until its turn comes, and the watch loop would otherwise read
        # that as a crash and start a second restart of it.
        this = asyncio.current_task()
        for _, spec in group:
            spec._restart_task = this
        try:
            # Stop the others first, in reverse order, then restart in order.
            for name, spec in reversed(group):
                if spec.actor and name != crashed_name:
                    await self._stop_actor(name, spec)
            for name, spec in group:
                if spec.retired:
                    continue
                await self._restart_one(name, spec, crashed=name == crashed_name)
        finally:
            for _, spec in group:
                if spec._restart_task is this:
                    spec._restart_task = None

    def _entries(self, names: list[str] | None = None) -> list[tuple[str, SupervisedSpec]]:
        """``names`` (every supervised name by default) with their specs, in order.

        Read with ``.get()``: a spec can be dropped while a strategy or a
        shutdown is part-way through the list, which is awaiting all the time,
        and a name without one is skipped rather than raising halfway through.
        """
        return [
            (name, spec)
            for name in list(self._order if names is None else names)
            if (spec := self._specs.get(name)) is not None
        ]

    # ── Individual restart ────────────────────────────────────────────────────

    def _recovery_time(self, spec: SupervisedSpec) -> float:
        """How long an actor must stay up for its crash streak to end.

        ``restart_window``, or once restarts have slowed, as long as its last
        restart waited: an agent that runs a few minutes between crashes
        would otherwise leave slow retry and enter it again, each time with a
        notice, instead of settling at a pace.
        """
        if spec.slow:
            return max(spec.restart_window, self._restart_delay(spec, crashed=True))
        return spec.restart_window

    def _restart_delay(self, spec: SupervisedSpec, crashed: bool) -> float:
        """How long to wait before this restart.

        Doubles with each crash in a row, from ``restart_delay`` up to
        ``MAX_RESTART_DELAY``; once restarts have slowed, from
        ``SLOW_RETRY_DELAY`` up to ``MAX_SLOW_RETRY_DELAY``. A sibling restarted
        because another actor crashed waits only its ``restart_delay``.
        """
        if not crashed:
            return spec.restart_delay
        if spec.slow:
            slow_attempt = spec.crash_streak - spec.max_restarts
            return min(
                self.SLOW_RETRY_DELAY * 2 ** max(slow_attempt - 1, 0), self.MAX_SLOW_RETRY_DELAY
            )
        return min(spec.restart_delay * 2 ** max(spec.crash_streak - 1, 0), self.MAX_RESTART_DELAY)

    async def _restart_one(self, name: str, spec: SupervisedSpec, crashed: bool = True):
        """Restart one actor, after a delay that grows while it keeps crashing.

        The old actor is stopped before the wait, not after it. A FAILED actor
        has ended its message loop, but its subscriptions, stream windows and
        command listener run on until it is stopped, and a delay in slow retry
        is long enough for them to go on acting on whatever arrives.

        A failed respawn leaves the spec without an actor, which the watch loop
        reads as a crash on its next poll: it waits out the next, longer delay
        rather than retrying at the poll's pace.
        """
        if crashed:
            spec.crash_streak += 1
            if not spec.slow and spec.crash_streak > spec.max_restarts:
                spec.slow = True
                await self._notify_main(
                    f"🚨 **{name}** has crashed {spec.crash_streak} times in a row. It will keep "
                    f"being restarted, but less often: in {self.SLOW_RETRY_DELAY / 60:.0f} minutes, "
                    f"then with the wait doubling up to {self.MAX_SLOW_RETRY_DELAY / 3600:.0f} "
                    "hour. Fix its code, or delete it, if it is not going to recover on its own.",
                    severity="critical",
                )

        # Stopped before the wait, so that nothing of it keeps running through
        # the delay; the spec holds the restart task, which is what tells the
        # watch loop that an entry with no actor is being seen to.
        if spec.actor:
            await self._stop_actor(name, spec)

        delay = self._restart_delay(spec, crashed)
        if delay > 0:
            logger.info("[Supervisor] Restarting '%s' in %.0fs.", name, delay)
            await asyncio.sleep(delay)

        # The lock was released before the strategy ran, and the delay above is
        # the widest part of that window: a delete, a reset or a deliberate stop
        # landing in it must not be undone by bringing the actor back.
        if not self._still_supervised(name, spec):
            logger.info("[Supervisor] Not restarting '%s': it left supervision meanwhile.", name)
            return
        if spec.actor is not None:
            # The old actor was stopped before the wait, so this one was started
            # by someone else meanwhile -- a start from the dashboard or a
            # command puts it back through resupervise(). Spawning another would
            # stop it, since both answer to the same actor id.
            logger.info("[Supervisor] Not restarting '%s': it was started again meanwhile.", name)
            return

        logger.info("[Supervisor] Restarting '%s' (crash %s in a row).", name, spec.crash_streak)

        # Spawn a fresh one. A failure leaves the spec with no actor, which the
        # watch loop reads as another crash and retries after a longer delay.
        try:
            new_actor = await self._spawn_actor(name, spec)
        except Exception:
            spec.actor = None
            logger.exception("[Supervisor] Respawn of '%s' failed", name)
            return
        spec.actor = new_actor
        if not self._still_supervised(name, spec):
            # Left during the spawn itself. Nothing would supervise or own the
            # actor just started, so it is stopped rather than left running.
            logger.info("[Supervisor] '%s' left supervision while restarting; stopping it.", name)
            await self._stop_actor(name, spec)
            return
        spec.restarts += 1
        new_actor.metrics.restart_count = spec.restarts
        # A fresh actor starts a fresh error count, and the window follows it.
        new_actor.metrics.errors = 0
        spec._errors_seen = 0
        spec._error_times.clear()

        logger.info("[Supervisor] '%s' restarted successfully.", name)
        if crashed and not spec.slow:
            streak = f" ({spec.crash_streak} crashes in a row)" if spec.crash_streak > 1 else ""
            await self._notify_main(
                f"♻️ **{name}** crashed and was automatically restarted{streak}. "
                "It is running again.",
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
        try:
            await actor.start()
        except BaseException:
            # Cancellation included: registered but never handed back, the actor
            # is held by nothing that would stop it.
            with contextlib.suppress(Exception):
                await actor.stop()
            with contextlib.suppress(Exception):
                await self._registry.unregister(actor.actor_id)
            raise
        logger.debug("[Supervisor] Spawned '%s' (%s).", name, actor.actor_id[:8])
        return actor

    def _still_supervised(self, name: str, spec: SupervisedSpec) -> bool:
        """Whether ``spec`` is still the live entry for ``name``.

        False once it has been forgotten, replaced by a spawn under the same name,
        or released by a deliberate stop.
        """
        return self._specs.get(name) is spec and not spec.retired

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
        except Exception:  # noqa: S110  # the stop failure above is already logged
            pass
        spec.actor = None

    def _hold_report(self, msg: Message) -> None:
        """Keep a report for a later check, saying so when an older one is pushed out."""
        if len(self._held_reports) == self._held_reports.maxlen:
            logger.warning(
                "[Supervisor] main has taken no report for a while; dropping the oldest of %d held",
                len(self._held_reports),
            )
        self._held_reports.append(msg)

    def _hand_over_held_reports(self) -> None:
        """Give main the reports it had no room for, in the order they were made."""
        if not self._held_reports or not self._registry:
            return
        main = self._registry.find_by_name("main")
        if main is None:
            return
        while self._held_reports and main.offer(self._held_reports[0]):
            self._held_reports.popleft()

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
                # Offered, never waited on: this runs between a failure and its
                # restart. A main too busy to take the report gets it at a
                # later check, with the time it was made.
                if self._held_reports or not main.offer(msg):
                    self._hold_report(msg)
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

    @property
    def running(self) -> bool:
        """Whether the supervised actors have started and are being watched.

        False before :meth:`start` has finished and from the moment :meth:`stop`
        begins, which is what a readiness probe needs to know.
        """
        return self._watch_task is not None and not self._watch_task.done()

    def status(self) -> list[dict]:
        """Return a snapshot of all supervised actors for dashboard/CLI."""
        result = []
        for name, spec in self._entries():
            actor = spec.actor
            result.append(
                {
                    "name": name,
                    "strategy": spec.strategy.value,
                    "max_restarts": spec.max_restarts,
                    "restarts_used": spec.restarts,
                    "crash_streak": spec.crash_streak,
                    "slow_retry": spec.slow,
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
        # Pending restarts too: one waiting out its delay would otherwise start
        # an actor after everything else has stopped. A set, because the
        # siblings a strategy restarts share its task.
        pending = {
            task
            for spec in self._specs.values()
            if (task := spec._restart_task) is not None
            and not task.done()
            and task is not asyncio.current_task()
        }
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        async with self._lock:
            # A missing entry must not end the loop: every actor after it would
            # be left running, and its state never flushed.
            for name, spec in reversed(self._entries()):
                await self._stop_actor(name, spec)
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
        #: Set as shutdown begins and never cleared: a system is not started twice.
        self.stopping = False
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

        state_dir = Path(self._state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        self._mqtt_client = await MQTTPublisher.create(
            self._mqtt_broker, self._mqtt_port, db_path=state_dir / "mqtt_outbox.db"
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
        self.stopping = True
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
        """Block until the system is stopped or interrupted.

        A cancellation stops everything and is then raised on: the caller
        asked for it, and a host wrapping this in a timeout or a task group
        has to see the cancellation to tell a stop from a normal finish.
        """
        try:
            while self._running:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("[ActorSystem] Shutdown signal received.")
            await self.stop_all()
            raise
