"""Durable MQTT publishing for the actor system.

Callers hand messages over and carry on: delivery, reconnection and retry
happen behind them, so a broker that is slow or absent never reaches back into
an actor's own loop. Messages that have not gone out yet are held in SQLite, so
they survive the process as well as the connection.

Imports nothing else from ``wactorz`` — it talks to a broker, not to actors.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from contextlib import closing

logger = logging.getLogger(__name__)


class MQTTPublisher:
    """Reliable async MQTT publisher with:
      - Persistent in-memory outbox queue (messages survive reconnects)
      - SQLite-backed durable outbox (messages survive process crashes)
      - a kept session + fixed client_id (broker holds QoS 1 messages)
      - QoS 1 for critical messages, QoS 0 for telemetry
      - Automatic reconnection with exponential backoff
      - Never blocks callers — publish() always returns immediately

    Message priority:
      qos=1  → goes to durable SQLite outbox, guaranteed delivery
      qos=0  → in-memory only, and kept there until it can be sent
      retain → stored at broker, replayed to new subscribers

    QoS 0 is **not** dropped while disconnected — it is queued like anything
    else and delivered on reconnect; only a process exit loses it.

    **The queue is bounded, and what gives way is telemetry.** A broker that is
    absent or slower than the app publishes used to grow this without limit
    until the process died — the failure being a memory graph, not a message,
    which is what made it easy to leave. At the cap, the *oldest* queued QoS 0
    message is discarded: heartbeats, metrics, logs and status are superseded by
    the next sample, so the newest is the one worth keeping. QoS 1 is never
    discarded to make room, because it is already in the SQLite outbox — at
    worst it waits for the reconnect that reloads it.
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

    #: How many messages may wait in memory before telemetry starts giving way.
    #: Large enough that an ordinary reconnect blip queues and drains without
    #: dropping anything; small enough that an absent broker costs megabytes
    #: rather than the process.
    MAX_QUEUED = 10_000

    def __init__(self, db_path: str = "./state/mqtt_outbox.db") -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_QUEUED)
        #: How many messages the cap has discarded, for the log and for tests.
        self._dropped = 0
        #: A message whose publish failed, retried before the queue is read again.
        self._retry: tuple | None = None
        self._task: asyncio.Task | None = None
        self._available = False
        self._db_path = db_path
        #: Minted on first use, not here -- see :attr:`client_id`.
        self._client_id = ""
        self._connected = False

    @property
    def client_id(self) -> str:
        """This publisher's MQTT client id, minted on first use.

        Deliberately not computed in ``__init__``: :func:`install_id` creates
        the state directory and writes a file, and a constructor must not touch
        the disk -- an object has to be constructible in a test without any of
        that happening.

        The import is local for the same reason as the one in the connect path
        below: this module is reached through ``core/__init__``.
        """
        if not self._client_id:
            from .mqtt import client_id, install_id

            self._client_id = client_id("pub", install_id())
        return self._client_id

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
                pub.client_id,
                db_path,
            )
        except ImportError:
            logger.warning("[MQTT] aiomqtt not installed. MQTT disabled.")
        except Exception as e:
            logger.warning("[MQTT] Publisher unavailable: %s", e)
        return pub

    # ── SQLite outbox ──────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create outbox table if it doesn't exist.

        Every connection here is wrapped in `closing`. A `sqlite3` connection
        used as a context manager commits the transaction and leaves the handle
        open, so `with connect(...) as db` alone hands the outbox a new
        descriptor per publish and relies on the garbage collector to reclaim
        it. `closing(...)` closes it; the inner `db` keeps the commit.
        """
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as db, db:
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
            with closing(sqlite3.connect(self._db_path)) as db, db:
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

    def _delete_from_db(self, row_id: int) -> None:
        """Remove a delivered message from the outbox."""
        try:
            with closing(sqlite3.connect(self._db_path)) as db, db:
                db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
                db.commit()
        except Exception as e:
            logger.debug("[MQTT] Outbox delete failed: %s", e)

    def _enqueue(self, item: tuple) -> None:
        """Queue `item`, making room by discarding telemetry if the cap is reached.

        Never blocks the caller. `publish()` promises to return immediately, so
        waiting for space here would push a slow broker back into the actor loop
        that called it — the thing this class exists to prevent.

        Room is made from the front: the oldest QoS 0 message goes, because the
        next heartbeat or metric replaces it anyway and the freshest sample is
        the useful one. If the queue holds nothing droppable, the *incoming*
        message gives way instead — a QoS 1 in that position is already in the
        outbox and will be reloaded on the next connect, so what is lost is the
        wait, not the message.
        """
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                if not self._discard_one_telemetry():
                    self._note_drop(item)
                    return

    def _discard_one_telemetry(self) -> bool:
        """Drop the oldest QoS 0 message, if the queue has one. True if it did.

        Rebuilds the queue rather than reaching into it: `asyncio.Queue` has no
        supported way to remove from the middle, and its internal deque is not
        ours to mutate.
        """
        held = []
        dropped = False
        while not self._queue.empty():
            entry = self._queue.get_nowait()
            self._queue.task_done()
            if not dropped and entry[3] < 1:
                dropped = True
                self._note_drop(entry)
                continue
            held.append(entry)
        for entry in held:
            self._queue.put_nowait(entry)
        return dropped

    def _note_drop(self, item: tuple) -> None:
        """Count a discarded message, and say so at a rate a log can carry."""
        self._dropped += 1
        if self._dropped == 1 or self._dropped % 1000 == 0:
            logger.warning(
                "[MQTT] outbox full at %d — discarded %s (%d total). The broker is not "
                "keeping up, or is not there.",
                self.MAX_QUEUED,
                item[0],
                self._dropped,
            )

    def _load_pending_from_db(self) -> None:
        """On startup, reload undelivered QoS 1 messages into the in-memory queue."""
        try:
            with closing(sqlite3.connect(self._db_path)) as db, db:
                rows = db.execute(
                    "SELECT id, topic, payload, retain, qos FROM outbox ORDER BY id"
                ).fetchall()
            if rows:
                logger.info("[MQTT] Replaying %s undelivered message(s) from outbox", len(rows))
            for row_id, topic, payload, retain, qos in rows:
                self._enqueue((topic, payload, bool(retain), qos, row_id))
        except Exception as e:
            logger.debug("[MQTT] Outbox load failed: %s", e)

    # ── Public API ─────────────────────────────────────────────────────────

    async def publish(self, topic: str, payload, retain: bool = False, qos: int = 0) -> None:
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
            self._enqueue((topic, payload, retain, qos, row_id))
        else:
            # Best-effort: in-memory only
            self._enqueue((topic, payload, retain, qos, -1))

    async def disconnect(self) -> None:
        """Stop the drain loop and close the connection."""
        if self._task:
            self._task.cancel()
            # gather rather than a bare await: the drain loop's own
            # CancelledError comes back as a value, so ignoring it cannot also
            # swallow a cancellation aimed at the caller of disconnect().
            (outcome,) = await asyncio.gather(self._task, return_exceptions=True)
            # Reported, not dropped. gather *retrieves* the exception, which also
            # suppresses asyncio's "never retrieved" warning — so a drain loop
            # that died of something real would otherwise vanish at shutdown,
            # exactly when someone is looking for why messages stopped going out.
            # CancelledError is a BaseException, so this is a real crash only.
            if isinstance(outcome, Exception):
                logger.warning("[MQTT] Publisher drain loop ended in error: %s", outcome)

    @property
    def connected(self) -> bool:
        """Whether the broker connection is currently up."""
        return self._connected

    @property
    def queue_depth(self) -> int:
        """How many messages are waiting to be sent."""
        return self._queue.qsize()

    # ── Background drain loop ──────────────────────────────────────────────

    async def _run(self, broker: str, port: int) -> None:
        """Background loop: maintain persistent MQTT connection and drain the outbox.
        - a kept session: the broker holds subscriptions and QoS 1 messages across
          reconnects, and forgets them once the expiry passes
        - Fixed client_id: same session resumed after reconnect
        - Messages are NOT dequeued until successfully published (no loss on disconnect)
        """
        # local: avoids core/__init__ import cycle
        from .mqtt import SERVER_SESSION_EXPIRY_SECONDS, mqtt_client, session_kwargs

        backoff = 1.0
        _last_exc_str: str | None = None

        while True:
            try:
                async with mqtt_client(
                    broker,
                    port,
                    identifier=self.client_id,
                    **session_kwargs(SERVER_SESSION_EXPIRY_SECONDS),
                    keepalive=30,
                ) as client:
                    self._connected = True
                    logger.info("[MQTT] Publisher connected | client_id=%s", self.client_id)

                    while True:
                        # A message whose publish failed is retried before
                        # anything queued behind it. Held here rather than put
                        # back on the queue: `asyncio.Queue.put` appends to the
                        # *tail*, so the old "put back at front" comment
                        # described the opposite of what happened — a failed
                        # message came back out after every message produced
                        # during the outage, and an agent's ordered updates were
                        # delivered out of order.
                        if self._retry is not None:
                            item, self._retry = self._retry, None
                            from_queue = False
                        else:
                            item = await self._queue.get()
                            from_queue = True
                        topic, payload, retain, qos, row_id = item

                        try:
                            await client.publish(topic, payload, retain=retain, qos=qos)
                            # Only remove from queue AFTER successful publish.
                            # `task_done` belongs to a `get`, so it is skipped
                            # for a retry that never went back on the queue.
                            if from_queue:
                                self._queue.task_done()
                            # Remove from SQLite outbox if it was persisted
                            if row_id >= 0:
                                self._delete_from_db(row_id)
                            # Reset backoff and error dedup only after a successful publish
                            backoff = 1.0
                            _last_exc_str = None
                        except Exception as pub_err:
                            logger.warning("[MQTT] Publish failed: %s — retrying it first", pub_err)
                            self._retry = item
                            if from_queue:
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
