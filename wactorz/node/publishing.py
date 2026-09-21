"""Everything this node sends to the broker, and the queue it waits in.

Publishing is queued rather than awaited so a stalled broker never pushes back
into the agent code that called it — the same reasoning as ``MQTTPublisher`` on
the server, whose cap and telemetry suffixes this reuses. What it cannot reuse
is the outbox: the server can discard a queued QoS 1 message because SQLite
still holds it, and a node has no SQLite. A message dropped here is gone, so the
cap is sized for a long outage on a busy node and every drop is counted.

paho is driven directly rather than through aiomqtt. aiomqtt wraps it, but its
network loop gets no CPU time while this coroutine blocks on ``queue.get()``,
which loses messages silently; ``paho.loop_start()`` runs a background thread
that handles ACKs and keepalives whatever the event loop is doing.
"""

import asyncio
import json
import logging
import threading
from typing import Any

import paho.mqtt.client as paho_mqtt
from paho.mqtt.enums import CallbackAPIVersion

from ..config import CONFIG
from ..core.mqtt import SERVER_SESSION_EXPIRY_SECONDS, client_id, session_kwargs
from ..core.mqtt_publisher import MQTTPublisher
from ..core.mqtt_tls import client_context, tls_enabled

logger = logging.getLogger(__name__)

#: How many messages may wait in memory before telemetry starts giving way, and
#: which topics are the telemetry that gives way. Both from the server's
#: publisher, so a node and main agree on what is worth keeping.
MAX_QUEUED = MQTTPublisher.MAX_QUEUED
TELEMETRY_TOPIC_SUFFIXES = MQTTPublisher._TELEMETRY_TOPIC_SUFFIXES

#: How long to wait before dialling the broker again after it went away. Short:
#: a node that cannot publish is invisible to main, which after 90s of silence
#: gives up on it and takes its agents away.
RETRY_DELAY_S = 3.0

#: How long to wait for the broker to say whether it took this connection.
#: Generous: it is one round trip, and a node on slow wifi is the ordinary case.
CONNACK_TIMEOUT_S = 15.0

#: One queue entry: the topic, the payload as sent, whether it is retained, and
#: whether losing it would lose something the system needs.
QueueItem = tuple[str, bytes, bool, bool]


class BrokerRefusedNode(ConnectionError):
    """The broker would not take this node's publishing connection.

    Its own class rather than a message on a bare `ConnectionError`, because it
    is the one connection failure that will not fix itself: a node whose
    credentials the broker declines reconnects for ever, and the reason is the
    only thing that says which.
    """

    def __init__(self, broker: str, port: int, reason: Any) -> None:
        super().__init__(f"{broker}:{port} refused this node: {reason}")


class BrokerDidNotAnswer(ConnectionError):
    """The broker took the connection and never said whether it accepted it."""

    def __init__(self, broker: str, port: int) -> None:
        super().__init__(f"{broker}:{port} accepted the connection but did not answer")


def is_critical(topic: str) -> bool:
    """Whether losing this message would lose something the system needs.

    Everything that is not plain telemetry: results, errors, manifests, and the
    migration replies that decide where an agent lives.
    """
    return not topic.endswith(TELEMETRY_TOPIC_SUFFIXES)


def new_pub_queue() -> asyncio.Queue[QueueItem]:
    """The publish queue, bounded.

    Unbounded, a broker outage on a node that keeps publishing grows this until
    the machine runs out of memory -- and these run on Raspberry Pis. Built here
    rather than inline so the bound itself can be tested, instead of only the
    eviction that depends on it.
    """
    return asyncio.Queue(maxsize=MAX_QUEUED)


def close_mqtt_client(client: Any, what: str) -> None:
    """Stop and disconnect a paho client, whatever state it is in.

    Called on paths that are discarding the client either way, so a failure to
    close it changes nothing that follows and is recorded rather than raised.
    """
    try:
        client.loop_stop()
        client.disconnect()
    except Exception:
        logger.debug("[runner] %s did not close cleanly", what, exc_info=True)


class NodePublisher:
    """The node's outbound half: a bounded queue and the connection that drains it."""

    def __init__(self, broker: str, port: int, node_name: str) -> None:
        self.broker = broker
        self.port = port
        self.node_name = node_name
        #: Created in :meth:`run`, inside the event loop that will own it.
        self._queue: asyncio.Queue[QueueItem] | None = None
        #: Messages the cap discarded, for the log.
        self.dropped = 0
        #: Taken from the queue and not yet accepted by the client. Held so a
        #: broker that goes away mid-send costs a retry rather than the message.
        self._inflight: QueueItem | None = None
        self._running = False

    @property
    def queue(self) -> asyncio.Queue[QueueItem] | None:
        return self._queue

    # ── Enqueuing ─────────────────────────────────────────────────────────────

    async def publish(
        self,
        topic: str,
        payload: Any,
        retain: bool = False,
        qos: int | None = None,
        user_properties: Any = None,
    ) -> None:
        """Queue a message for the publisher loop. Never waits for room.

        Waiting would push a stalled broker back into the agent code that called
        this -- the same reasoning as `MQTTPublisher._enqueue` on the server.

        The signature takes ``qos`` and ``user_properties`` so this can stand in
        for an aiomqtt client on :attr:`Actor._mqtt_client`. Both are accepted
        and neither is used. What a node may drop is a property of the topic
        rather than of the caller, so the QoS is decided below; and the only
        user properties this system sends are the signatures main puts on node
        control messages, which a node receives and never sends.
        """
        if self._queue is None:
            return
        if user_properties:
            logger.debug("[runner] Dropping user properties on %s — a node signs nothing", topic)
        encoded = payload if isinstance(payload, (bytes, bytearray)) else None
        if encoded is None:
            encoded = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
        self._enqueue((topic, bytes(encoded), retain, is_critical(topic)))

    def _enqueue(self, item: QueueItem) -> None:
        """Add a message, making room by dropping telemetry when the cap is hit.

        Room is made from the front: the oldest telemetry goes, because the next
        heartbeat replaces it anyway and the freshest sample is the useful one.
        If nothing droppable is queued, the incoming message gives way.

        ⚠ **Unlike the server, a message dropped here is gone.** `MQTTPublisher`
        can discard a queued QoS 1 because the SQLite outbox still holds it; a
        node has no outbox. The cap is therefore sized so that only a long
        outage on a busy node reaches it, and every drop is counted and logged.
        """
        queue = self._queue
        if queue is None:
            return
        while True:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                if not self._discard_one_telemetry():
                    self._note_drop(item)
                    return
            else:
                return

    def _discard_one_telemetry(self) -> bool:
        """Drop the oldest non-critical message, if there is one. True if it did.

        Rebuilds the queue rather than reaching into it: `asyncio.Queue` offers
        no supported way to remove from the middle.
        """
        queue = self._queue
        if queue is None:
            return False
        held: list[QueueItem] = []
        dropped = False
        while not queue.empty():
            entry = queue.get_nowait()
            queue.task_done()
            if not dropped and not entry[3]:
                dropped = True
                self._note_drop(entry)
                continue
            held.append(entry)
        for entry in held:
            queue.put_nowait(entry)
        return dropped

    def _note_drop(self, item: QueueItem) -> None:
        """Count a discarded message, and say so at a rate a log can carry."""
        self.dropped += 1
        if self.dropped == 1 or self.dropped % 1000 == 0:
            logger.warning(
                "[runner] publish queue full at %d — discarded %s (%d total). The broker "
                "is not keeping up, or is not there.",
                MAX_QUEUED,
                item[0],
                self.dropped,
            )

    # ── Draining ──────────────────────────────────────────────────────────────

    def connect(self) -> Any:
        """Open a paho client for publishing, and wait to hear that it was let in.

        `connect` returns once the CONNECT packet is away; whether the broker
        accepted it arrives later, on the network loop. Without waiting for that
        this reported a connection it did not have — a node with credentials the
        broker would not take logged "Publisher connected" and then quietly
        dropped everything it published, heartbeats included, so the node was
        simply absent with nothing anywhere saying why. The subscriber, which
        does see its own refusal, was the only thing that said so.
        """
        client = paho_mqtt.Client(
            # Explicit, not defaulted: omitting it selects paho's callback API
            # version 1, which is deprecated and warns on every construction.
            # Nothing here registers a paho callback, so version 2 costs no
            # migration.
            CallbackAPIVersion.VERSION2,
            client_id=client_id("nodepub", self.node_name),
            # v5, so the kept session below can name a lifetime. v3.1.1 has no
            # expiry, and a decommissioned node would leave broker state for ever.
            protocol=paho_mqtt.MQTTv5,
        )
        if CONFIG.mqtt_username:
            client.username_pw_set(CONFIG.mqtt_username, CONFIG.mqtt_password or None)
        if tls_enabled(CONFIG.mqtt_tls):
            client.tls_set_context(
                client_context(CONFIG.mqtt_tls_ca, CONFIG.mqtt_tls_check_hostname)
            )
        # Durable, so QoS 1 messages in flight when the link drops are
        # redelivered rather than discarded with the session. The CONNECT
        # properties come from the same helper aiomqtt callers use, so a node
        # and the server ask the broker to hold a session for the same time.
        # A plain event rather than a queue: several methods here already bind
        # `queue` to this publisher's own, and a module of the same name reading
        # differently inside one of them is a trap for the next person.
        answered = threading.Event()
        outcome: dict[str, Any] = {}

        def _on_connect(_client: Any, _data: Any, _flags: Any, reason: Any, _props: Any = None):
            outcome["reason"] = reason
            answered.set()

        client.on_connect = _on_connect
        client.connect(
            self.broker,
            self.port,
            keepalive=60,
            clean_start=False,
            properties=session_kwargs(SERVER_SESSION_EXPIRY_SECONDS)["properties"],
        )
        client.loop_start()
        if not answered.wait(CONNACK_TIMEOUT_S):
            close_mqtt_client(client, "Unanswered publisher")
            raise BrokerDidNotAnswer(self.broker, self.port)
        reason = outcome.get("reason")
        if getattr(reason, "is_failure", False):
            close_mqtt_client(client, "Refused publisher")
            raise BrokerRefusedNode(self.broker, self.port, reason)
        return client

    async def publish_one_queued(self, client: Any) -> None:
        """Hand the next queued message to the client, waiting for one to arrive.

        Entries are built by :meth:`publish` alone, so the shape is fixed:
        (topic, payload, retain, critical) -- and `critical` decides the QoS as
        well as what the queue evicts first.
        """
        queue = self._queue
        if queue is None:
            return
        # A message already taken is finished first. `publish` below can fail --
        # the broker went away between queueing and sending -- and taking the
        # next one then would have left this one nowhere: off the queue, never
        # sent, and gone, on a node that has no outbox to fall back on.
        item = self._inflight or await queue.get()
        self._inflight = item
        topic, payload, retain, critical = item
        # Telemetry goes out at QoS 0, mirroring MQTTPublisher on the server.
        # Not only to save queue space: at QoS 1 the broker holds heartbeats for
        # a subscriber that is away and replays them on reconnect, and main
        # stamps a node as last seen *now* on receipt -- so a node that died
        # hours ago would read online for the whole freshness window, which is
        # exactly what gates migrating an agent onto it.
        try:
            client.publish(topic, payload, qos=1 if critical else 0, retain=retain)
        except ValueError:
            # The client refuses this message itself -- an impossible topic or a
            # payload past the protocol's size. Reconnecting would not change
            # its mind, so retrying it would stop everything behind it for ever.
            # `exception`, not `error`: what the client objected to is in the
            # traceback and nowhere else, and this is the one message that will
            # never be sent however long the node runs.
            logger.exception("[runner] Refusing to send %s: the client will not take it", topic)
            self._inflight = None
            queue.task_done()
            self.dropped += 1
            return
        self._inflight = None
        queue.task_done()

    async def run(self, ready: asyncio.Event | None = None) -> None:
        """Drain the queue for as long as the node is up, reconnecting as needed."""
        self._running = True
        self._queue = new_pub_queue()
        if ready is not None:
            ready.set()
        loop = asyncio.get_event_loop()
        client = None
        while self._running:
            try:
                if client is None:
                    client = await loop.run_in_executor(None, self.connect)
                    logger.info("[runner] Publisher connected to %s:%s", self.broker, self.port)
                await self.publish_one_queued(client)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    "[runner] Publisher error: %s. Reconnecting in %gs...", e, RETRY_DELAY_S
                )
                if client:
                    close_mqtt_client(client, "Discarded publisher")
                    client = None
                await asyncio.sleep(RETRY_DELAY_S)

        if client:
            close_mqtt_client(client, "Publisher")

    def stop(self) -> None:
        self._running = False
