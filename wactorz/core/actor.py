"""Base Actor - the foundation of the Actor Model framework.
Every agent IS an actor. Actors communicate via message passing only.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import pickle
import sys
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from .atomic_io import quarantine_unreadable, write_pickle
from .cancellation import cancel_all_until_done
from .paths import agent_state_dir, resolve_state_dir
from .subscriptions import SubscriptionHub, is_durable_actor
from .topic_bus import StreamWindow, get_topic_bus

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

#: Objects the per-heartbeat size walk may visit before it stops descending.
SIZE_WALK_BUDGET = 2000


def forbidden(command: str, *, protected: bool, essential: bool) -> bool:
    """Whether policy refuses this command, regardless of the actor's state.

    Two different questions. ``protected`` marks an agent defined in code rather
    than spawned, so a delete drops a registry entry nothing can recreate.
    ``essential`` marks one whose stop would take away the means of undoing it.
    A refusal for either reason is a rule about the agent, not about the moment,
    which is what separates it from a command declined because the state is
    wrong.

    Shared rather than reimplemented per entry point: the transports each had
    their own copy, and a copy is only ever as current as the last person to
    remember it.
    """
    return (protected and command == "delete") or (essential and command == "stop")


def derive_actor_id(name: str) -> str:
    """The actor id a named actor gets, derived from its name and nothing else.

    Deterministic on purpose: the same agent must come back as the same id
    across restarts, because the broker keys a held session on it and the
    registry keys everything else on it.

    Exported because several places need to *recognise* a derived id rather than
    mint one -- deciding whether an actor can hold a broker session, addressing
    an agent by name. Spelled once so those cannot drift apart: if the formula
    changed under a copy, nothing would raise, sessions would simply stop being
    resumed and durability would quietly become clean.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"wactorz.actor.{name}"))


def has_derived_id(name: str, actor_id: str) -> bool:
    """Whether this identity comes from the name, and so survives a restart.

    A named actor reconnects as the same client and resumes whatever the broker
    held for it. An anonymous one is given a fresh id every incarnation, so a
    session kept under the old id is unreachable -- keeping one is not harmful,
    it is pointless, and it costs the broker state until it expires.
    """
    return derive_actor_id(name) == str(actor_id)


class ActorState(str, Enum):
    """Where an actor is in its lifecycle."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class MessageType(str, Enum):
    """What a message is asking of its recipient."""

    # Lifecycle
    START = "start"
    STOP = "stop"
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


#: How long a sender waits for room in a full mailbox before its message is
#: refused. Long enough to outlast a burst the recipient is working through,
#: and bounded so that one actor which has stopped reading cannot hold every
#: actor that writes to it.
MAILBOX_WAIT_S = 30.0

#: How long a listener waits before it tries the broker again.
RECONNECT_DELAY_S = 5.0

#: How often a waiting sender looks again for room.
_MAILBOX_POLL_S = 0.05

#: Message types that only report: losing one loses nothing a later one will
#: not say again.
_REPORT_TYPES = frozenset(
    {
        MessageType.HEARTBEAT,
        MessageType.TICK,
        MessageType.STATUS_REQUEST,
        MessageType.STATUS_RESPONSE,
    }
)


@dataclass
class Message:
    """One unit of communication between actors."""

    type: MessageType
    sender_id: str
    payload: Any = None
    reply_to: str | None = None
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)

    @property
    def is_notification(self) -> bool:
        """Whether this only reports something, and asks nothing of its recipient.

        A heartbeat, a status exchange, or an alert for main's notification
        list. A full mailbox drops these at once: the sender is a supervisor or
        a monitor, which must not be held up by the actor it is reporting to.
        """
        if self.type in _REPORT_TYPES:
            return True
        return (
            self.type == MessageType.TASK
            and isinstance(self.payload, dict)
            and bool(self.payload.get("_monitor_notification"))
        )

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
    """Running counters for one actor, reported in heartbeats.

    `messages_received` and `heartbeats` count the current process only; unlike
    `messages_processed` they are not restored after a restart.
    """

    messages_received: int = 0
    messages_processed: int = 0
    errors: int = 0
    start_time: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    tasks_completed: int = 0
    tasks_failed: int = 0
    restart_count: int = 0  # incremented by Supervisor on each restart
    heartbeats: int = 0
    #: Messages this actor's mailbox had no room for and did not take.
    messages_refused: int = 0

    @property
    def uptime(self) -> float:
        """Seconds since the actor started."""
        return time.time() - self.start_time


def _as_coroutine_callback(callback: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """``callback`` as something the hub can await, whatever kind it was given.

    A coroutine function is used as it is. A plain function runs on a worker
    thread, so a callback that blocks -- a model's `predict`, a file read --
    does not hold the event loop every other actor in the process shares.
    """
    if inspect.iscoroutinefunction(callback):
        return callback

    async def _on_thread(payload: Any) -> Any:
        return await asyncio.to_thread(callback, payload)

    return _on_thread


class Actor(ABC):
    """Base Actor class. All agents inherit from this.
    Actors are fully async and communicate only through messages.
    """

    def __init__(
        self,
        actor_id: str | None = None,
        name: str | None = None,
        persistence_dir: str | None = None,
        mailbox_size: int = 1000,
    ):
        if actor_id:
            self.actor_id = actor_id
        elif name:
            # Deterministic UUID from name — same name always gets same ID across restarts
            self.actor_id = derive_actor_id(name)
        else:
            self.actor_id = str(uuid.uuid4())
        self.name = name or f"actor-{self.actor_id[:8]}"
        self.state = ActorState.IDLE
        self.metrics = ActorMetrics()

        # Async mailbox (inbox)
        self._mailbox: asyncio.Queue = asyncio.Queue(maxsize=mailbox_size)
        #: When the message being handled was taken up, on the monotonic clock;
        #: None between messages.
        self._handling_since: float | None = None
        self._outbox: dict[str, asyncio.Queue] = {}  # actor_id -> queue ref

        # Registry reference (set by ActorSystem)
        self._registry: ActorRegistry | None = None
        self._mqtt_client: Any | None = None
        self._mqtt_broker: str = "localhost"
        self._mqtt_port: int = 1883

        #: The node this actor runs on, empty when it runs on main. Set by the
        #: node runner on the agents it starts; every heartbeat carries it, so
        #: the dashboard can place an agent without having to ask which node
        #: claimed it. The empty string is what the rest of the framework means
        #: by local — see main's `is_target_local`.
        self._node: str = ""

        #: The one broker connection this actor's subscriptions share, made on
        #: the first `subscribe`. None until then, so an actor that never
        #: subscribes never connects for it.
        self._sub_hub: SubscriptionHub | None = None
        #: Rolling windows by topic, from `window`. One per topic: a second call
        #: for the same topic returns the window that has been filling.
        self._windows: dict[str, StreamWindow] = {}

        # Persistence
        # Use name as persistence folder so it survives restarts with same name
        # Falls back to actor_id for anonymous actors

        # `resolve_state_dir` rather than a literal: where durable state lives is
        # one question with one answer (explicit argument, else
        # `WACTORZ_STATE_DIR`, else `./state`). The old `./actor_state` default
        # was a second opinion that disagreed with the resolver, so a caller who
        # omitted the argument wrote somewhere nothing else would look.
        # Through `agent_state_dir`, so a name like `..` is refused rather than
        # walking out of the state directory it was given.
        self._persistence_dir = agent_state_dir(persistence_dir or resolve_state_dir(), self.name)
        self._persistence_dir.mkdir(parents=True, exist_ok=True)
        self._persistent_state: dict = {}

        # Unified persistence API — set by ActorSystem if available,
        # otherwise falls back to legacy pickle behavior
        self._persistence_api: PersistenceAPI | None = None

        # Protection — if True, delete is refused. These are agents defined in
        # code rather than spawned, so deleting one drops a spawn-registry entry
        # that nothing can recreate from the interface.
        self.protected: bool = False

        # Essential — if True, stop is refused as well. Reserved for an agent the
        # user would be stopping their own way of undoing it: everything else can
        # be stopped and started again from its card.
        self.essential: bool = False

        # Supervisor reference — set by Supervisor when this actor is registered under it
        self.supervisor_id: str | None = None

        # Handlers
        self._handlers: dict[MessageType, Callable] = {}
        #: Correlation id → the future waiting on that request's RESULT.
        #: Populated by whoever sends a TASK carrying `_task_id`; drained by
        #: `_resolve_pending_result` before the message reaches a handler.
        self._result_futures: dict[str, asyncio.Future] = {}
        self._setup_default_handlers()

        # Background tasks
        self._tasks: list[asyncio.Task] = []
        #: Resolved once this run's stop has finished; see stop(). Created by the
        #: first stop, not here, and cleared by start().
        self._stopped: asyncio.Future[None] | None = None

        # Cached process handle for heartbeat metrics — one per actor so each
        # has an independent cpu_percent baseline (interval=None, non-blocking).
        self._proc: Any | None = None
        try:
            self._proc = psutil.Process()
            self._proc.cpu_percent(interval=None)  # prime the baseline
        except Exception:  # noqa: S110  # psutil is optional; the actor runs without it
            pass

        logger.info("[%s] Actor created with id=%s", self.name, self.actor_id)

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        """Start the actor's event loop."""
        self._stopped = None
        self.state = ActorState.RUNNING
        self.metrics.start_time = time.time()
        await self._load_persistent_state()
        # Restore the message count from a previous run — but only into a fresh
        # instance. The supervisor restarts by building a new actor, whose count
        # is zero; the start command restarts *this* object, whose count is
        # already the live total. Adding the persisted value there counts every
        # message twice, and again on each subsequent stop/start.
        if self.metrics.messages_processed == 0:
            saved_msgs = self.recall("_messages_processed", {})
            if isinstance(saved_msgs, dict) and saved_msgs.get("count"):
                self.metrics.messages_processed = int(saved_msgs["count"])
        await self.on_start()
        self._tasks.append(asyncio.create_task(self._message_loop()))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._tasks.append(asyncio.create_task(self._command_listener()))
        await self._publish_status()
        logger.info("[%s] Actor started.", self.name)

    async def stop(self):
        """Gracefully stop the actor, once per run however many ask.

        A second stop -- a replace or a migration that reaches an actor the
        supervisor also stops, or shutdown meeting a delete -- would run
        ``on_stop`` and the state saves again. It waits for the first to finish
        instead, and returns: a caller that goes on to act on the stopped actor,
        such as a delete purging its state, then acts after the stop and not
        during it. ``start()`` begins a new run.
        """
        if self._stopped is not None:
            await asyncio.shield(self._stopped)
            return
        self._stopped = asyncio.get_running_loop().create_future()
        try:
            await self._stop_once()
        finally:
            if not self._stopped.done():
                self._stopped.set_result(None)

    async def _stop_once(self):
        """What stopping does: wind down, clean up, save, and say so."""
        self.state = ActorState.STOPPED
        await self._wind_down_tasks()
        # Each window holds a broker connection of its own, and a restart builds
        # a new actor with windows of its own.
        self._close_windows()
        # Shield cleanup from CancelledError — chat tasks run as fire-and-forget
        # asyncio tasks outside actor._tasks and get cancelled by asyncio.run()
        # cleanup BEFORE these awaits if we don't shield them.
        #
        # A cancellation arriving here belongs to whoever is *calling* stop(),
        # not to this cleanup: `shield` keeps the inner coroutine running and
        # raises in the awaiting task. Discarding it told that caller its
        # cancellation had been honoured when it had not — the supervisor's watch
        # loop resumed polling, and `Supervisor.stop()` waited forever on a task
        # already marked cancelling. So it is remembered, both steps still run,
        # and it is re-raised once cleanup is done.
        cancelled = False
        # A failure in either is said and the stop goes on: the actor still has
        # to come off the broker and out of the registry, and an agent's own
        # `on_stop` is code nobody here wrote.
        try:
            await asyncio.shield(self.on_stop())
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            logger.exception("[%s] on_stop failed; stopping anyway", self.name)
        try:
            await asyncio.shield(self._save_persistent_state())
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            logger.exception(
                "[%s] Could not save state while stopping; what it last persisted may be lost",
                self.name,
            )

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
        except Exception:  # noqa: S110  # a last telemetry frame, sent on the way out
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
        except Exception:  # noqa: S110  # TopicBus is optional; not being registered is not fatal
            pass  # TopicBus not initialised or unavailable — not fatal
        logger.info("[%s] Actor stopped.", self.name)
        # Deferred to here rather than raised where it arrived: the shield exists
        # so cleanup completes, and stopping half way through would defeat it.
        # The caller still learns its cancellation was real.
        if cancelled:
            raise asyncio.CancelledError

    #: How long stop() waits for a cancelled task before giving up on it.
    TASK_SHUTDOWN_TIMEOUT = 5.0

    async def _discard_child(self, child: Actor) -> None:
        """Stop and unregister a child whose start did not complete."""
        with contextlib.suppress(Exception):
            await child.stop()
        if self._registry:
            with contextlib.suppress(Exception):
                await self._registry.unregister(child.actor_id)

    def run_detached(
        self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
    ) -> asyncio.Task[Any]:
        """Run ``coro`` alongside the actor, as a task the actor owns.

        For work the caller does not wait for. A bare ``asyncio.create_task``
        keeps no reference, so the task can be garbage-collected part-way
        through, and nothing cancels it when the actor stops: it goes on
        running against an actor, or a system, that has already shut down.
        This one is held in ``_tasks`` until it ends, so ``stop()`` cancels and
        waits for it like the actor's own loops, and a failure is logged rather
        than surfacing as "exception never retrieved" whenever it is collected.
        """
        task = asyncio.create_task(coro, name=name)
        if self.state == ActorState.STOPPED:
            # Too late to own it: stop() sets this before winding tasks down,
            # so one added now would be cleared without being cancelled.
            task.cancel()
            logger.debug("[%s] Not starting %s: the actor has stopped", self.name, task.get_name())
            return task
        self._tasks.append(task)
        task.add_done_callback(self._forget_detached)
        return task

    def _forget_detached(self, task: asyncio.Task[Any]) -> None:
        """Drop a finished detached task, reporting how it failed if it did."""
        # Already gone when stop() cleared the list while winding down.
        if task in self._tasks:
            self._tasks.remove(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "[%s] Background task %s failed", self.name, task.get_name(), exc_info=error
            )

    async def _wind_down_tasks(self) -> None:
        """Cancel the actor's own tasks and wait for them to actually finish.

        ``cancel()`` only requests cancellation. Returning without awaiting left
        a task that was part-way through a message to unwind after ``stop()``
        had already reported the actor stopped — and since the task list was
        never cleared, starting the actor again ran a second message loop, a
        second heartbeat and a second command listener alongside the ones still
        winding down, so every command was handled twice.

        The task this runs in is left alone: a stop command arrives on the
        command listener, and a task cannot wait for itself. Each loop is
        written to exit once the state is STOPPED, which is already set.
        """
        current = asyncio.current_task()
        others = [task for task in self._tasks if task is not current]
        try:
            if others:
                # Bounded: a task that will not unwind must not hold up shutdown.
                # Each is asked again while it keeps running: on Python 3.10 and
                # 3.11 a cancellation that lands inside a wait_for is discarded, and
                # the broker and Home Assistant clients both wait that way, so one
                # request can leave stop() waiting out the whole timeout.
                # cancel_all_until_done never uses wait_for itself, for the same
                # reason: a caller cancelled while waiting here still learns it was.
                still_running = await cancel_all_until_done(
                    others, timeout=self.TASK_SHUTDOWN_TIMEOUT
                )
                if still_running:
                    logger.warning(
                        "[%s] %d task(s) did not stop within %gs.",
                        self.name,
                        len(still_running),
                        self.TASK_SHUTDOWN_TIMEOUT,
                    )
        # CancelledError is deliberately not caught. Swallowing it consumes the
        # caller's own cancellation: the supervisor's watch loop was cancelled
        # here while stopping an actor, never saw the error, resumed its poll
        # loop, and left Supervisor.stop() awaiting a task that could no longer
        # finish. Clearing the task list still happens either way.
        finally:
            self._tasks.clear()

    # ─── Message Loop ─────────────────────────────────────────────────────────

    async def _message_loop(self):
        """Main message processing loop."""
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                msg = await asyncio.wait_for(self._mailbox.get(), timeout=1.0)
                self.metrics.messages_received += 1
                # Only count meaningful messages — not heartbeats, status pings, lifecycle
                _noise = {
                    MessageType.HEARTBEAT,
                    MessageType.STATUS_REQUEST,
                    MessageType.STATUS_RESPONSE,
                    MessageType.STOP,
                }
                if msg.type not in _noise:
                    self.metrics.messages_processed += 1
                self._handling_since = time.monotonic()
                try:
                    await self._dispatch(msg)
                finally:
                    self._handling_since = None
                self._mailbox.task_done()

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                self.metrics.errors += 1
                logger.exception("[%s] Error in message loop", self.name)

    def _resolve_pending_result(self, msg: Message) -> bool:
        """Settle a waiting future from a RESULT's correlation id.

        Request/reply is a convention rather than a framework feature: the
        sender tags a TASK with `_task_id` and blocks on a future keyed by it,
        and the recipient echoes that id back.

        It belongs here, ahead of every handler, so that any actor can receive a
        reply without opting in — a per-agent implementation makes the ability
        depend on which agent is receiving. Returns True when the message was a
        reply someone was waiting for, in which case there is nothing left to
        dispatch: the caller already has it.
        """
        if msg.type != MessageType.RESULT or not isinstance(msg.payload, dict):
            return False
        # "task" is the older spelling and still on the wire from some agents.
        fid = msg.payload.get("_task_id") or msg.payload.get("task")
        future = self._result_futures.get(fid) if fid else None
        if future is None:
            return False
        if not future.done():
            future.set_result(msg.payload)
        return True

    async def _dispatch(self, msg: Message):
        """Dispatch message to the appropriate handler."""
        if self._resolve_pending_result(msg):
            return
        handler = self._handlers.get(msg.type)
        if handler:
            await handler(msg)
        else:
            await self.handle_message(msg)

    def _setup_default_handlers(self):
        self._handlers = {
            MessageType.START: self._handle_lifecycle,
            MessageType.STOP: self._handle_lifecycle,
            MessageType.STATUS_REQUEST: self._handle_status_request,
            MessageType.HEARTBEAT: self._handle_heartbeat_msg,
        }

    async def _handle_lifecycle(self, msg: Message):
        """Apply a lifecycle message through the one implementation of it.

        Message-passing is another way to ask for the same thing, not another
        set of rules: START/STOP all route through `apply_command`,
        so an essential actor refuses a STOP message exactly as it refuses the
        REST and dashboard routes, and the supervision release happens once
        rather than per entry point.
        """
        await self.apply_command(msg.type.value)

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
        self.metrics.heartbeats += 1
        await self._mqtt_publish(f"agents/{self.actor_id}/heartbeat", self._build_heartbeat())
        await self._mqtt_publish(f"agents/{self.actor_id}/metrics", self._build_metrics())
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                await asyncio.sleep(interval)
                hb = self._build_heartbeat()
                self.metrics.heartbeats += 1
                self.metrics.last_heartbeat = time.time()
                await self._mqtt_publish(f"agents/{self.actor_id}/heartbeat", hb)
                await self._mqtt_publish(f"agents/{self.actor_id}/metrics", self._build_metrics())
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[%s] Heartbeat error: %s", self.name, e)

    def _estimate_memory_mb(self) -> float:
        """Bounded deep-size walk over this actor's own data structures.

        Bounded in breadth as well as depth: the walk runs on the event loop on
        every heartbeat, so it stops after ``SIZE_WALK_BUDGET`` objects and its
        cost stays flat however far the actor's state has grown. Past that point
        the result is a lower bound rather than a full accounting.
        Uses only sys.getsizeof — no new deps, works on Windows.
        """
        seen: set[int] = set()

        def _deep(obj: object, depth: int) -> int:
            if depth > 5 or len(seen) >= SIZE_WALK_BUDGET:
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
        except Exception:  # noqa: S110  # a heartbeat reports 0.0 rather than not arriving
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
            "essential": self.essential,
            "node": self._node,
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

    LIFECYCLE_COMMANDS = ("start", "stop", "delete")

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

    def _leave_supervision(self) -> None:
        """Tell the supervisor this actor is gone for good: deleted, or ended itself.

        Stronger than :meth:`_release_from_supervision`, which keeps the entry so
        a stopped actor can be supervised again when it starts. An actor that
        will not come back needs no entry, and a kept one holds what the actor
        was built from until the process exits.
        """
        if self._registry and hasattr(self._registry, "_supervisor_ref"):
            sup = self._registry._supervisor_ref
            if sup is not None:
                sup.drop_supervised(self.name)

    def _resume_supervision(self) -> None:
        """Put this actor back under supervision after a deliberate stop.

        The mirror of :meth:`_release_from_supervision`. Without it a restarted
        actor runs unwatched — it would crash and stay down, which is exactly
        what supervision exists to prevent.
        """
        if self._registry and hasattr(self._registry, "_supervisor_ref"):
            sup = self._registry._supervisor_ref
            if sup is not None:
                sup.resupervise(self.name, self)

    async def apply_command(self, command: str) -> bool:
        """Run a lifecycle command on this actor. Returns whether it took effect.

        These are not a dispatch table, which is why they live here rather than
        in the transport below: ``stop`` must release from supervision first, and
        ``delete`` also clears the spawn registry and unregisters. A caller that
        holds the actor and simply awaits ``stop()`` gets it restarted half a
        minute later by the watchdog.

        Every entry point routes through this — the broker listener, and the web
        layer when the actor is local — so the two cannot drift apart.

        Returns ``False`` when the command was refused -- ``delete`` on a
        protected actor, ``stop`` on an essential one -- or unknown, so a caller
        can report that rather than claim success.
        """
        if forbidden(command, protected=self.protected, essential=self.essential):
            logger.warning("[%s] Ignoring %r — refused by policy.", self.name, command)
            return False

        if command == "start":
            if self.state != ActorState.STOPPED:
                logger.warning("[%s] Ignoring 'start' — actor is %s.", self.name, self.state.value)
                return False
            await self.start()
            self._resume_supervision()
        elif command == "stop":
            self._release_from_supervision()
            await self.stop()
        elif command == "delete":
            self._leave_supervision()
            await self.delete_own_traces()
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

    async def delete_own_traces(self) -> None:
        """Run `on_delete`, never letting a failure in it stop the deletion."""
        try:
            await self.on_delete()
        except Exception:
            logger.exception("[%s] on_delete failed; deleting anyway", self.name)

    async def _command_listener(self):
        """Carry commands from agents/{id}/commands to :meth:`apply_command`.

        Transport only. Needed for actors a caller cannot reach in process —
        after the direct-dispatch change that means agents on remote nodes.
        """
        # local: avoids core/__init__ import cycle
        from .mqtt import (
            AGENT_SESSION_EXPIRY_SECONDS,
            client_id,
            mqtt_client,
            reconnect_wait,
            session_kwargs,
        )

        topic = f"agents/{self.actor_id}/commands"
        # Same rule the subscription hub follows: only an identity that
        # survives a restart can resume a session, so an anonymous actor
        # connects clean rather than leaving one behind per incarnation.
        durable = has_derived_id(self.name, self.actor_id)
        session = session_kwargs(AGENT_SESSION_EXPIRY_SECONDS) if durable else {}
        # A `commands` detail, not the bare actor id: SubscriptionHub already
        # connects as `wactorz-agent-<actor id>`, and two connections sharing an
        # id kick each other off the broker for ever.
        identifier = client_id("agent", str(self.actor_id), "commands")
        # Whether the connection is known to be down, so it is said when it
        # goes and when it comes back, not at every attempt in between.
        down = False
        while self.state not in (ActorState.STOPPED, ActorState.FAILED):
            try:
                async with mqtt_client(
                    self._mqtt_broker,
                    self._mqtt_port,
                    identifier=identifier,
                    # Control, not telemetry: a dropped stop leaves an agent
                    # running while the dashboard reports it stopped.
                    **session,
                ) as client:
                    await client.subscribe(topic, qos=1 if durable else 0)
                    logger.debug("[%s] Subscribed to %s", self.name, topic)
                    if down:
                        down = False
                        logger.info("[%s] Listening for commands again.", self.name)
                    async for message in client.messages:
                        try:
                            data = json.loads(message.payload.decode())
                            command = data.get("command", "")
                            logger.info("[%s] Received command: %s", self.name, command)
                            applied = await self.apply_command(command)
                            # Only stop listening if it actually took effect: a
                            # an actor that refuses a command by policy must keep
                            # receiving commands afterwards.
                            if applied and command in ("stop", "delete"):
                                return
                        except Exception:
                            logger.exception("[%s] Command parse error", self.name)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self.state not in (ActorState.STOPPED, ActorState.FAILED):
                    if not down:
                        down = True
                        logger.warning(
                            "[%s] Lost the broker connection it takes commands on (%s). A stop "
                            "or delete sent over the broker will not reach it until it is "
                            "back; trying again every %gs or so.",
                            self.name,
                            exc,
                            RECONNECT_DELAY_S,
                        )
                    await asyncio.sleep(reconnect_wait(RECONNECT_DELAY_S))

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

    @property
    def handling_seconds(self) -> float:
        """How long this actor has been on the message it is handling; 0 when idle.

        The heartbeat is a task of its own and carries on whatever the message
        loop is doing, so an actor waiting for ever on one message still looks
        alive. This is what tells the two apart.
        """
        since = self._handling_since
        return 0.0 if since is None else time.monotonic() - since

    async def receive(self, msg: Message) -> bool:
        """Put a message in this actor's mailbox. False if there was no room for it.

        A mailbox with room takes the message at once. A full one means this
        actor is not keeping up, and the sender is usually another actor in the
        middle of handling a message of its own, so how long it may be held is
        bounded: a notification is dropped on the spot, and anything else waits
        ``MAILBOX_WAIT_S`` for room and is then refused. Either way the sender
        is told, and can report that rather than hang.
        """
        if self.offer(msg):
            return True
        if msg.is_notification:
            self._note_refused(msg, "dropped a notification")
            return False
        deadline = time.monotonic() + MAILBOX_WAIT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(_MAILBOX_POLL_S)
            if self.offer(msg):
                return True
        self._note_refused(msg, f"refused a message after waiting {MAILBOX_WAIT_S:g}s")
        return False

    def offer(self, msg: Message) -> bool:
        """Put a message in the mailbox if it has room, without waiting. True if it did."""
        try:
            self._mailbox.put_nowait(msg)
        except asyncio.QueueFull:
            return False
        return True

    def _note_refused(self, msg: Message, what: str) -> None:
        """Count a message the mailbox had no room for, and say so at a rate a log can carry."""
        self.metrics.messages_refused += 1
        refused = self.metrics.messages_refused
        if refused == 1 or refused % 100 == 0:
            logger.warning(
                "[%s] Mailbox full at %d: %s (%s from %s; %d refused so far). The actor is "
                "not keeping up with what it is sent, or is stuck on one message.",
                self.name,
                self._mailbox.maxsize,
                what,
                msg.type.value,
                msg.sender_id[:8],
                refused,
            )

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

        # Start the child. Registered already, so a start that fails or is
        # cancelled must take it back out: nothing else holds it to stop, and
        # supervision has not adopted it yet.
        try:
            await child.start()
        except BaseException:
            # Shielded, so a second cancellation cannot cut the cleanup short;
            # it is still passed on once the cleanup is done.
            try:
                await asyncio.shield(self._discard_child(child))
            except asyncio.CancelledError:
                raise asyncio.CancelledError from None
            raise

        # ── Erlang/OTP: register child under Supervisor so it's never an orphan ──
        # We reach into the registry to find the ActorSystem's supervisor.
        # If no supervisor is available this is a safe no-op.
        try:
            if self._registry and hasattr(self._registry, "_supervisor_ref"):
                supervisor = self._registry._supervisor_ref
                if supervisor is not None:
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

                    # Adopted as it runs, so the watch loop monitors it at once
                    # without a redundant restart -- and re-armed if the name
                    # was supervised before and released, or it would not be.
                    supervisor.adopt(
                        child.name,
                        _child_factory,
                        child,
                        strategy=SupervisorStrategy.ONE_FOR_ONE,
                        max_restarts=5,
                        restart_window=60.0,
                        restart_delay=2.0,
                    )
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
            # State is kept per key as persist() is called, and its file is
            # written a moment later. A stop does not leave that to the moment:
            # once it returns, the file holds what the agent last persisted.
            self._persistence_api.flush()
            return
        # Legacy pickle path
        try:
            write_pickle(self._persistence_dir / "state.pkl", self._persistent_state)
        except Exception:
            logger.exception("[%s] Failed to save state", self.name)

    async def _load_persistent_state(self):
        """Load state from disk. Called on start() before on_start()."""
        if self._persistence_api is not None:
            # New path: state is loaded per-key via recall(), nothing to batch-load.
            # But load legacy pickle for backward compat if it exists.
            path = self._persistence_dir / "state.pkl"
            if path.exists():
                try:
                    with open(path, "rb") as f:
                        self._persistent_state = pickle.load(  # noqa: S301  # our own state file, under the state dir
                            f
                        )  # our own state file, under the state dir
                    logger.info(
                        "[%s] Loaded legacy persistent state (will migrate on first persist).",
                        self.name,
                    )
                except Exception as e:
                    self._keep_unreadable_state(path, e)
            return
        # Legacy pickle path
        path = self._persistence_dir / "state.pkl"
        if path.exists():
            try:
                with open(path, "rb") as f:
                    self._persistent_state = pickle.load(  # noqa: S301  # our own state file, under the state dir
                        f
                    )  # our own state file, under the state dir
                logger.info("[%s] Loaded persistent state.", self.name)
            except Exception as e:
                self._keep_unreadable_state(path, e)

    def _keep_unreadable_state(self, path: Path, exc: Exception) -> None:
        """Move a state file we could not read out of the next save's way.

        Starting empty is the right call — the agent must come up — but the very
        next `persist` would write over the file, so the only record of what was
        lost has to be taken out of that path first.
        """
        kept = quarantine_unreadable(path)
        logger.error(
            "[%s] Failed to load state: %s — %s",
            self.name,
            exc,
            f"kept at {kept}" if kept else "the file could not be preserved",
        )

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

    # ─── Subscriptions ────────────────────────────────────────────────────────

    def _make_hub(self) -> SubscriptionHub:
        """The hub that carries this actor's subscriptions.

        A method so a subclass can hand out one with its own failure policy: a
        generated program's hub asks the model to repair a failing callback.
        """
        return SubscriptionHub(self, durable=is_durable_actor(self))

    def subscribe(self, topic: str, callback: Callable[[Any], Any]) -> None:
        """Call ``callback(payload)`` for every message matching ``topic``.

        ``topic`` is an MQTT filter, so ``sensors/#`` and ``sensors/+/temp``
        work. The payload is the message decoded as JSON, or ``{"raw": text}``
        when it is not JSON. The callback may be a coroutine function or a
        plain one; messages on one topic are handled one at a time, in order,
        so a callback that keeps state between calls is never run against
        itself. Every subscription of this actor shares one broker connection,
        opened by the first call and closed when the actor stops.

        A callback that keeps raising marks the actor FAILED after a few
        failures in a row, which is the supervisor's cue to restart it.
        """
        if not callable(callback):
            raise TypeError(f"subscribe({topic!r}) needs a callable callback, got {callback!r}")
        if self._sub_hub is None:
            self._sub_hub = self._make_hub()
        task = self._sub_hub.bind(topic, _as_coroutine_callback(callback))
        if task is not None:
            self._tasks.append(task)

    def window(self, topic: str, seconds: float = 300, max_size: int = 1000) -> StreamWindow:
        """A rolling window over the last ``seconds`` of messages on ``topic``.

        Returns a :class:`~wactorz.core.topic_bus.StreamWindow`, with ``mean``,
        ``min``, ``max``, ``rising``, ``falling``, ``absent_for`` and the rest,
        fed from the broker from the moment it is made. One window per topic:
        asking again for the same topic returns the one that has been filling.
        """
        existing = self._windows.get(topic)
        if existing is not None:
            return existing
        bus = get_topic_bus()
        if bus is not None:
            made = bus.make_window(topic, seconds=seconds, max_size=max_size)
        else:
            made = StreamWindow(topic, seconds=seconds, max_size=max_size)
            made.start(self._mqtt_broker, self._mqtt_port)
        self._windows[topic] = made
        return made

    def _close_windows(self) -> None:
        """Stop every window this actor opened."""
        for topic, made in list(self._windows.items()):
            try:
                made.stop()
            except Exception as exc:
                logger.debug("[%s] Window on %s would not stop: %s", self.name, topic, exc)
        self._windows.clear()

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
                user_properties = self._publish_properties(topic, encoded)
                if user_properties:
                    await self._mqtt_client.publish(
                        topic, encoded, retain=retain, qos=qos, user_properties=user_properties
                    )
                else:
                    await self._mqtt_client.publish(topic, encoded, retain=retain, qos=qos)
            except Exception as e:
                logger.debug("[%s] MQTT publish failed: %s", self.name, e)

    def _publish_properties(self, topic: str, encoded: Any) -> list[tuple[str, str]] | None:
        """MQTT v5 user properties to send with ``encoded`` on ``topic``. None for most actors."""
        return None

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
        # QoS 1: neither critical-prefixed nor telemetry-suffixed, so the
        # publisher would leave this at 0 -- and a frame emitted while the
        # monitor is reconnecting would simply be gone. The agent said
        # something; the user never sees it.
        await self._mqtt_publish(f"agents/{self.actor_id}/chat", payload, qos=1)

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
        subscribes: list[str] | None = None,
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
            "subscribes": subscribes or [],
            "capabilities": capabilities or [],
            "input_schema": input_schema or {},
            "output_schema": output_schema or {},
            "timestamp": time.time(),
        }
        await self._mqtt_publish(f"agents/{self.actor_id}/manifest", manifest, retain=True)

    async def withdraw_manifest(self) -> None:
        """Withdraw the retained manifest, announcing that this actor is gone.

        An empty retained payload is the MQTT idiom for taking a retained message
        back. The manifest topic is keyed on the actor id whoever published it —
        the actor itself or the API of a generated agent — so one call withdraws
        it either way, and the dashboard reads the withdrawal as the actor
        ceasing to exist.

        Call it from an ending the actor decides on for itself; a stop is not
        one, because a stopped actor still exists and can be started again. At
        QoS 1 because a lost withdrawal leaves the broker replaying the manifest
        to every subscriber that connects later.
        """
        await self._mqtt_publish(f"agents/{self.actor_id}/manifest", b"", retain=True, qos=1)

    async def on_stop(self):
        """Called when actor stops. Override for cleanup."""

    async def on_delete(self):
        """Called before the stop that ends a deletion. Override to remove what stop keeps.

        A stop is expected to be undone later, so it keeps state; a delete
        promises that no trace of the agent is left. The caller purges the
        stores it knows about afterwards. This is for everything else the agent
        owns: files of its own, and retained messages outside `agents/<id>/`.

        It runs where an agent is deleted in this process: main's delete, the
        actor's own `delete` command, and a factory reset forgetting it. A
        node's runner does not call it. Its delete also drops the copy an agent
        leaves behind when it migrates, and there the agent lives on elsewhere
        under the same topics, so removing its retained messages would take
        them from the copy that is running.
        """

    @abstractmethod
    async def handle_message(self, msg: Message):
        """Handle messages not caught by default handlers."""

    def __repr__(self):
        return f"<Actor name={self.name} id={self.actor_id[:8]} state={self.state.value}>"
