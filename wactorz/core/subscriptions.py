"""Every subscription an actor holds, carried on one MQTT connection.

An actor that subscribes to several topics holds one authenticated session for
all of them rather than one each: the cost of a subscription is then a binding
in a list, not broker state, however many an actor takes out.

A binding is a topic *filter* and a callback. A message is queued for every
binding it matches, and each binding runs its callbacks one message at a time,
in arrival order, so a callback that keeps state between calls is never run
against itself. Cross-topic concurrency is the point of sharing a connection;
same-topic reordering was never asked for.

What happens when a callback keeps failing is the one thing a host decides for
itself: here a budget of consecutive failures marks the actor FAILED for its
supervisor to restart. A host that can repair a program in place overrides
:meth:`SubscriptionHub._record_failure` -- see the dynamic agent's listener.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from typing import Any

import aiomqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from .mqtt import AGENT_SESSION_EXPIRY_SECONDS, agent_client_id, mqtt_client
from .topic_bus import topic_matches

logger = logging.getLogger(__name__)

#: Consecutive callback failures before the binding is dropped and the actor fails.
MAX_CONSECUTIVE_FAILURES = 5


def decode_payload(raw: Any) -> Any:
    """What a message carried: JSON when it is JSON, else the text, else the bytes.

    A camera frame or an audio chunk is bytes that are neither, and must reach
    the callback as bytes rather than end the connection on a decode error.
    """
    if not isinstance(raw, (bytes, bytearray)):
        return raw
    try:
        text = bytes(raw).decode()
    except UnicodeDecodeError:
        return {"raw": bytes(raw)}
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text}


async def safe_invoke(cb: Any, payload: Any, actor: Any, warned: list[bool]) -> None:
    """Run a subscribe callback, tolerating a stray `await` on a sync call."""
    try:
        await cb(payload)
    except TypeError as e:
        if "NoneType" in str(e) and "await" in str(e):
            if not warned[0]:
                logger.warning(
                    "[%s] subscribe callback has 'await None' error (suppressed): %s",
                    actor.name,
                    e,
                )
                warned[0] = True
            # Swallow: a sync API method was awaited, harmless
        else:
            raise


class Binding:
    """One topic filter, its callback, and the queue that serialises it.

    A queue with a single worker rather than a task per message, so messages on
    a topic are handled one at a time in arrival order. A callback that keeps
    state between calls -- counters, calibration values, persist/recall -- is
    not written to be re-entrant, and running two messages from the same topic
    concurrently would interleave at every `await` inside it.

    ``concurrency`` above one asks for that many workers on the same queue, for
    a callback that waits rather than computes -- a model call, a training
    job -- and would otherwise queue its topic behind itself. Messages are then
    handled as workers free up, not in order, and the callback has to cope
    with running against itself.
    """

    def __init__(self, topic: str, callback: Any, maxsize: int, concurrency: int = 1) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {concurrency}")
        self.topic = topic
        self.callback = callback
        self.concurrency = concurrency
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.workers: list[asyncio.Task] = []
        #: Messages discarded because the callback fell behind, for the log.
        self.dropped = 0

    @property
    def worker(self) -> asyncio.Task | None:
        """The first worker, for callers that know of one."""
        return self.workers[0] if self.workers else None

    def live_workers(self) -> list[asyncio.Task]:
        return [w for w in self.workers if not w.done()]

    def offer(self, payload: Any) -> bool:
        """Queue a payload, discarding the oldest when the callback is behind.

        Bounded, because a serialising queue is otherwise an unbounded backlog:
        a callback slower than its topic's arrival rate would grow it without
        limit. Oldest goes first -- for the sensor streams these subscriptions
        carry, the freshest reading is the useful one.
        """
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped += 1
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover - drained concurrently
                pass
            self.queue.put_nowait(payload)
            return False
        return True


class SubscriptionHub:
    """One connection per actor, carrying all of its subscriptions.

    Bindings are topic *filters*, so a message is delivered to every binding it
    matches -- the same fan-out the broker would do across separate connections.
    """

    #: Seconds to wait before rebuilding a connection that dropped.
    RECONNECT_DELAY = 5.0
    #: Messages held per subscription while its callback catches up.
    QUEUE_MAX = 100
    #: Shared with the actor's own command listener, so an agent's connections
    #: age out together rather than by two separate numbers.
    SESSION_EXPIRY_SECONDS = AGENT_SESSION_EXPIRY_SECONDS

    def __init__(self, actor: Any, durable: bool = False) -> None:
        self._actor = actor
        #: Whether the broker should hold this actor's subscriptions while it is
        #: away. Only meaningful for an actor whose id survives a restart -- an
        #: anonymous one gets a fresh id per incarnation, so a session kept for
        #: the old id could never be resumed by the new one.
        self._durable = durable
        #: A list, not keyed by topic: two callbacks may watch the same
        #: filter, and keying by topic would silently drop the first.
        self._bindings: list[Binding] = []
        self._client: Any = None
        self._task: asyncio.Task | None = None
        #: Subscriptions sent on the live connection and not yet acknowledged,
        #: held so none is collected part-way through.
        self._subscribing: set[asyncio.Task] = set()
        self._warned = [False]
        #: Consecutive failures per topic, for the budget `_record_failure` keeps.
        self._failures: dict[str, int] = {}

    def bind(self, topic: str, callback: Any, *, concurrency: int = 1) -> asyncio.Task | None:
        """Register a subscription, returning the hub task if this call started it.

        The caller tracks that task on the actor so stopping the actor stops the
        connection. It must **not** be tracked as a program task: a repair
        cancels those, and cancelling this one would take down the subscriptions
        of every other binding with it. Repair calls :meth:`clear` instead.
        ``concurrency`` is the number of workers on the topic; see :class:`Binding`.
        """
        binding = Binding(topic, callback, self.QUEUE_MAX, concurrency)
        self._bindings.append(binding)
        self._start_workers(binding)
        if self._client is not None:
            task = asyncio.create_task(self._subscribe_now(topic))
            self._subscribing.add(task)
            task.add_done_callback(self._subscribing.discard)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
            return self._task
        return None

    async def clear(self) -> None:
        """Drop every binding, keeping the connection.

        For a host that replaces its program: the connection outlives any one
        subscription, so the bindings have to be removed explicitly or messages
        keep being dispatched into callbacks that have been replaced.
        """
        bindings = list(self._bindings)
        self._bindings.clear()
        for binding in bindings:
            self._stop_worker(binding)
        for task in list(self._subscribing):
            task.cancel()
        client = self._client
        if client is None:
            return
        for topic in dict.fromkeys(b.topic for b in bindings):
            try:
                await client.unsubscribe(topic)
            except Exception:
                # A dropped connection unsubscribes by itself; the binding is
                # already gone, which is what stops the callback being reached.
                logger.debug("[%s] Could not unsubscribe %s", self._actor.name, topic)

    def _stop_worker(self, binding: Binding) -> None:
        # Never cancel the task this is running in. A host clearing every
        # binding from inside a failing callback -- a repair -- does so from
        # that binding's worker; cancelling it there would cut the work off at
        # its next await. `_drain` ends such a worker once the binding is gone.
        current = asyncio.current_task()
        for worker in binding.live_workers():
            if worker is not current:
                worker.cancel()
        if binding.dropped:
            logger.warning(
                "[%s] %s discarded %d message(s) while its callback was behind",
                self._actor.name,
                binding.topic,
                binding.dropped,
            )

    def _ensure_workers(self) -> None:
        """Give every binding its workers, reviving any that were cancelled."""
        for binding in self._bindings:
            self._start_workers(binding)

    def _start_workers(self, binding: Binding) -> None:
        """Bring ``binding`` up to its number of live workers."""
        live = binding.live_workers()
        for _ in range(binding.concurrency - len(live)):
            live.append(asyncio.create_task(self._drain(binding)))
        binding.workers = live

    def _qos(self) -> int:
        """QoS 1 only where a session exists to queue into.

        Delivery is `min(publish, subscribe)`, but a QoS 1 subscription on a
        clean session buys nothing: the broker discards the session the moment
        the client goes away, so there is nowhere for a held message to wait.
        """
        return 1 if self._durable else 0

    def _session_kwargs(self) -> dict[str, Any]:
        """Connect arguments that make the broker keep this session, or not."""
        if not self._durable:
            return {}
        properties = Properties(PacketTypes.CONNECT)
        properties.SessionExpiryInterval = self.SESSION_EXPIRY_SECONDS
        return {
            # v5, not v3.1.1: a v3.1.1 durable session has no expiry, so an
            # agent that is deleted -- or whose node never comes back -- leaves
            # broker state for ever.
            "protocol": aiomqtt.ProtocolVersion.V5,
            "clean_start": False,
            "properties": properties,
        }

    def _topics(self) -> list[str]:
        """The distinct filters to subscribe, in bind order."""
        return list(dict.fromkeys(b.topic for b in self._bindings))

    def _connect(self) -> Any:
        """The broker connection this hub holds, as an async context manager.

        A method so a subclass can take the client factory from its own module,
        where a test can stand a fake in for it.
        """
        return mqtt_client(
            self._actor._mqtt_broker,
            self._actor._mqtt_port,
            identifier=agent_client_id(
                str(self._actor.actor_id), getattr(self._actor, "_node", "") or ""
            ),
            **self._session_kwargs(),
        )

    async def _subscribe_now(self, topic: str) -> None:
        """Add a topic to a connection that is already up."""
        client = self._client
        if client is None:
            return
        try:
            await client.subscribe(topic, qos=self._qos())
        except Exception:
            # The reconnect path resubscribes everything still bound, so a
            # failure here costs a delay rather than the subscription.
            logger.debug("[%s] Deferred subscribe of %s to reconnect", self._actor.name, topic)

    async def run(self) -> None:
        """Hold the connection open and dispatch what arrives, reconnecting for ever."""
        try:
            while True:
                # Workers are cancelled when this task is, so a hub that is
                # restarted -- `bind` revives it once the task has ended -- would
                # otherwise re-subscribe and queue into queues nobody drains.
                self._ensure_workers()
                try:
                    async with self._connect() as client:
                        self._client = client
                        for topic in self._topics():
                            await client.subscribe(topic, qos=self._qos())
                        logger.info(
                            "[%s] Subscribed to %d topic(s) on one connection",
                            self._actor.name,
                            len(self._topics()),
                        )
                        async for message in client.messages:
                            self._dispatch(message)
                    continue
                except Exception as e:
                    self._client = None
                    logger.warning(
                        "[%s] MQTT subscribe error: %s — retrying in %ss",
                        self._actor.name,
                        e,
                        self.RECONNECT_DELAY,
                    )
                # Outside the handler: a cancellation that lands in this wait,
                # which is where a hub with no broker to reach spends its time,
                # must reach the cleanup below like one that lands anywhere else.
                await asyncio.sleep(self.RECONNECT_DELAY)
        except asyncio.CancelledError:
            pass
        finally:
            self._client = None
            # The workers belong to this connection: stopping the actor cancels
            # the hub task, and nothing else would reach them.
            for binding in list(self._bindings):
                self._stop_worker(binding)

    def _dispatch(self, message: Any) -> None:
        """Queue one message for every binding whose filter matches it.

        Handing off to per-binding queues rather than awaiting here: one
        connection means one message loop, so a slow callback awaited inline
        would stall every other subscription sharing it.
        """
        topic = str(message.topic)
        payload = decode_payload(message.payload)
        for binding in list(self._bindings):
            if topic_matches(binding.topic, topic):
                binding.offer(payload)

    async def _drain(self, binding: Binding) -> None:
        """Run one binding's callbacks, strictly one message at a time."""
        while True:
            payload = await binding.queue.get()
            try:
                await self._invoke(binding, payload)
            finally:
                binding.queue.task_done()
            if binding not in self._bindings:
                # Unbound while its callback ran. Whatever replaced it has bound
                # its own callbacks by now.
                return

    async def _invoke(self, binding: Binding, payload: Any) -> None:
        """Run one callback, keeping that topic's error budget."""
        try:
            await safe_invoke(binding.callback, payload, self._actor, self._warned)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        # BaseException: a `SystemExit` from a callback would end the process
        # rather than this subscription.
        except BaseException as e:
            await self._record_failure(binding, e)
            return
        self._record_success(binding)

    def _record_success(self, binding: Binding) -> None:
        """A callback returned: its topic's budget starts over."""
        self._failures.pop(binding.topic, None)

    async def _record_failure(self, binding: Binding, error: BaseException) -> None:
        """Count a failing callback, and fail the actor once the budget is spent.

        Every failure counts; a callback that then succeeds starts the count
        over. At `MAX_CONSECUTIVE_FAILURES` straight failures the binding is
        dropped and the actor marked FAILED, which is the supervisor's cue to
        restart it.
        """
        actor = self._actor
        topic = binding.topic
        failures = self._failures.get(topic, 0) + 1
        self._failures[topic] = failures
        metrics = getattr(actor, "metrics", None)
        if metrics is not None:
            metrics.errors += 1
        logger.error(
            "[%s] subscribe callback error (failure #%s/%s, topic=%s)",
            actor.name,
            failures,
            MAX_CONSECUTIVE_FAILURES,
            topic,
            exc_info=error,
        )
        if failures < MAX_CONSECUTIVE_FAILURES:
            return
        self.fail_binding(binding, traceback.format_exc())

    def fail_binding(self, binding: Binding, detail: str = "") -> None:
        """Drop `binding` and mark the actor FAILED for its supervisor.

        The binding rather than the connection: the other subscriptions must
        not keep firing into an actor on its way out, and the connection is
        closed when the actor's tasks are.
        """
        logger.critical(
            "[%s] subscribe callback on '%s' failed %sx in a row — marking FAILED for Supervisor.",
            self._actor.name,
            binding.topic,
            self._failures.get(binding.topic, 0),
        )
        if detail:
            logger.debug("[%s] last failure:\n%s", self._actor.name, detail)
        # Here rather than at module scope: `core.actor` imports this module,
        # and this is the one place the state enum is needed.
        from .actor import ActorState  # circular import: core.actor imports this module

        self._bindings = [b for b in self._bindings if b is not binding]
        # Cancels the worker this is running in; it takes effect at the next
        # await, which is the queue read `_drain` returns to.
        self._stop_worker(binding)
        self._actor.state = ActorState.FAILED


def is_durable_actor(actor: Any) -> bool:
    """Whether this actor's id survives a restart, and so can hold a session.

    A named actor derives its id from its name, so the same agent reconnects as
    the same client and resumes what the broker held. An anonymous one gets a
    fresh id every incarnation, so a session kept under the old id is
    unreachable -- durability there is not harmful, it is meaningless.

    This asks whether the id is name-derived, not whether it is stable. An
    actor constructed with an explicit `actor_id` that happens to be stable is
    classified as not durable. That is the safe direction -- a clean session
    and no broker state -- but such an actor forgoes durability it could in
    principle have had.
    """
    from .actor import has_derived_id  # circular import: core.actor imports this module

    name = getattr(actor, "name", "") or ""
    return has_derived_id(name, str(getattr(actor, "actor_id", "")))
