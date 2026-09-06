"""Every subscription an actor's program holds, carried on one MQTT connection.

Each ``agent.subscribe()`` used to open its own connection, so an actor with
twenty subscriptions held twenty authenticated sessions. Agent code is
model-authored, so a generated loop could exhaust a broker's connection limit
with nothing reviewing it first; one connection per actor makes that cost
constant however the program is written.

The callback error budget is kept on the actor rather than in this scope
because a reconnect rebuilds the loop, and a clean `process()` run must not
clear errors that `subscribe` recorded.
"""

import asyncio
import json
import logging
import time
import traceback
from typing import Any

from ...core.mqtt import AGENT_SESSION_EXPIRY_SECONDS, client_id, mqtt_client
from ...core.topic_bus import topic_matches

logger = logging.getLogger(__name__)

#: Escalations without recovery before the listener stops and the actor fails.
CB_MAX_ESCALATIONS = 5
#: Seconds between repeat reports of the same failing callback.
CB_ERROR_REPORT_INTERVAL = 30.0


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


class _Binding:
    """One topic filter, its callback, and the queue that serialises it.

    A queue with a single worker rather than a task per message: the connection
    this replaces awaited each callback, so messages on a topic were handled one
    at a time in arrival order. Agent code is model-authored and stateful --
    counters, calibration values, persist/recall -- and none of it is written to
    be re-entrant, so running two messages from the same topic concurrently
    would interleave at every `await` inside the callback. Cross-topic
    concurrency is the point of sharing a connection; same-topic reordering was
    never asked for.
    """

    def __init__(self, topic: str, callback: Any, maxsize: int) -> None:
        self.topic = topic
        self.callback = callback
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.worker: asyncio.Task | None = None
        #: Messages discarded because the callback fell behind, for the log.
        self.dropped = 0

    def offer(self, payload: Any) -> bool:
        """Queue a payload, discarding the oldest when the callback is behind.

        Bounded because the serialising queue is otherwise exactly the unbounded
        backlog the old design avoided by blocking: a callback slower than its
        topic's arrival rate would grow it without limit. Oldest goes first --
        for the sensor streams these subscriptions carry, the freshest reading
        is the useful one.
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
    """One connection per actor, carrying all of its program's subscriptions.

    Bindings are topic *filters*, so a message is delivered to every binding it
    matches -- the same fan-out the broker would have done across the separate
    connections this replaces.
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
        self._bindings: list[_Binding] = []
        self._client: Any = None
        self._task: asyncio.Task | None = None
        self._warned = [False]

    def bind(self, topic: str, callback: Any) -> asyncio.Task | None:
        """Register a subscription, returning the hub task if this call started it.

        The caller tracks that task on the actor so stopping the actor stops the
        connection. It must **not** be tracked as a program task: a repair
        cancels those, and cancelling this one would take down the subscriptions
        of every other binding with it. Repair calls :meth:`clear` instead.
        """
        binding = _Binding(topic, callback, self.QUEUE_MAX)
        self._bindings.append(binding)
        binding.worker = asyncio.create_task(self._drain(binding))
        if self._client is not None:
            asyncio.create_task(self._subscribe_now(topic))
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
            return self._task
        return None

    async def clear(self) -> None:
        """Drop every binding — what a code repair calls instead of cancelling.

        With a connection per subscription, cancelling the listener task was
        enough to stop the old program's callbacks arriving. The connection now
        outlives any single subscription, so the bindings have to be removed
        explicitly or messages keep being dispatched into a namespace that has
        been replaced.
        """
        bindings = list(self._bindings)
        self._bindings.clear()
        for binding in bindings:
            self._stop_worker(binding)
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

    def _stop_worker(self, binding: _Binding) -> None:
        if binding.worker is not None and not binding.worker.done():
            binding.worker.cancel()
        if binding.dropped:
            logger.warning(
                "[%s] %s discarded %d message(s) while its callback was behind",
                self._actor.name,
                binding.topic,
                binding.dropped,
            )

    def _ensure_workers(self) -> None:
        """Give every binding a live worker, reviving any that were cancelled."""
        for binding in self._bindings:
            if binding.worker is None or binding.worker.done():
                binding.worker = asyncio.create_task(self._drain(binding))

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
        import aiomqtt
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties

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
            import aiomqtt  # noqa: F401
        except ImportError:
            logger.exception("[%s] aiomqtt not installed", self._actor.name)
            return
        while True:
            try:
                # Workers are cancelled when this task is, so a hub that is
                # restarted -- `bind` revives it once the task has ended -- would
                # otherwise re-subscribe and queue into queues nobody drains.
                self._ensure_workers()
                async with mqtt_client(
                    self._actor._mqtt_broker,
                    self._actor._mqtt_port,
                    identifier=client_id("agent", str(self._actor.actor_id)),
                    **self._session_kwargs(),
                ) as client:
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
            except asyncio.CancelledError:
                self._client = None
                # The workers belong to this connection: stopping the actor
                # cancels the hub task, and nothing else would reach them.
                for binding in list(self._bindings):
                    self._stop_worker(binding)
                break
            except Exception as e:
                self._client = None
                logger.warning(
                    "[%s] MQTT subscribe error: %s — retrying in %ss",
                    self._actor.name,
                    e,
                    self.RECONNECT_DELAY,
                )
                await asyncio.sleep(self.RECONNECT_DELAY)

    def _dispatch(self, message: Any) -> None:
        """Queue one message for every binding whose filter matches it.

        Handing off to per-binding queues rather than awaiting here: one
        connection means one message loop, so a slow callback awaited inline
        would stall every other subscription sharing it.
        """
        topic = str(message.topic)
        try:
            payload = json.loads(message.payload.decode())
        except Exception:
            payload = {"raw": message.payload.decode()}
        for binding in list(self._bindings):
            if topic_matches(binding.topic, topic):
                binding.offer(payload)

    async def _drain(self, binding: _Binding) -> None:
        """Run one binding's callbacks, strictly one message at a time."""
        while True:
            payload = await binding.queue.get()
            try:
                await self._invoke(binding, payload)
            finally:
                binding.queue.task_done()

    async def _invoke(self, binding: _Binding, payload: Any) -> None:
        """Run one callback, keeping that topic's error budget on the actor."""
        actor = self._actor
        try:
            await safe_invoke(binding.callback, payload, actor, self._warned)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._record_failure(binding, e)
            return
        # Successful invocation — reset this topic's error budget
        actor._cb_error_count.pop(binding.topic, None)
        actor._cb_error_last.pop(binding.topic, None)

    async def _record_failure(self, binding: _Binding, error: Exception) -> None:
        """Escalate a failing callback, at most once per reporting interval."""
        actor = self._actor
        topic = binding.topic
        now = time.time()
        last = actor._cb_error_last.get(topic, 0)
        escalations = actor._cb_error_count.get(topic, 0)

        logger.exception(
            "[%s] subscribe callback error (escalation #%s/%s, topic=%s)",
            actor.name,
            escalations + 1,
            CB_MAX_ESCALATIONS,
            topic,
        )
        if (now - last) < CB_ERROR_REPORT_INTERVAL:
            return

        escalations += 1
        actor._cb_error_count[topic] = escalations
        actor._cb_error_last[topic] = now
        fatal = escalations >= CB_MAX_ESCALATIONS
        await actor._publish_error(
            phase="subscribe_callback",
            error=error,
            traceback_str=traceback.format_exc(),
            fatal=fatal,
        )
        if not fatal:
            return

        # Budget exhausted. Drop this binding rather than the connection: the
        # actor is being marked FAILED for the Supervisor to restart, and the
        # other subscriptions must not keep firing into a program on its way out.
        logger.critical(
            "[%s] subscribe callback on '%s' failed %sx — marking FAILED for Supervisor.",
            actor.name,
            topic,
            escalations,
        )
        from ...core.actor import ActorState

        self._bindings = [b for b in self._bindings if b is not binding]
        # Cancels the worker this is running in; it takes effect at the next
        # await, which is the queue read `_drain` returns to.
        self._stop_worker(binding)
        actor.state = ActorState.FAILED


def is_durable_actor(actor: Any) -> bool:
    """Whether this actor's id survives a restart, and so can hold a session.

    A named actor derives its id from its name, so the same agent reconnects as
    the same client and resumes what the broker held. An anonymous one gets a
    fresh id every incarnation, so a session kept under the old id is
    unreachable -- durability there is not harmful, it is meaningless.

    ⚠ **This asks whether the id is name-derived, not whether it is stable.** An
    actor constructed with an explicit `actor_id` that happens to be stable is
    classified as not durable. That is the safe direction -- a clean session and
    no broker state -- but it does mean such an actor forgoes durability it
    could in principle have had.
    """
    from ...core.actor import has_derived_id

    name = getattr(actor, "name", "") or ""
    return has_derived_id(name, str(getattr(actor, "actor_id", "")))


def hub_for(actor: Any) -> SubscriptionHub:
    """The actor's subscription hub, created on first subscribe."""
    hub = getattr(actor, "_sub_hub", None)
    if hub is None:
        hub = SubscriptionHub(actor, durable=is_durable_actor(actor))
        actor._sub_hub = hub
    return hub
