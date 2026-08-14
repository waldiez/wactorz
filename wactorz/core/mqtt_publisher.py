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

logger = logging.getLogger(__name__)


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
      qos=0  → in-memory only, and kept there until it can be sent
      retain → stored at broker, replayed to new subscribers

    QoS 0 is **not** dropped while disconnected — it is queued like anything
    else and delivered on reconnect; only a process exit loses it. This said
    "dropped if disconnected" for a long time, which made the in-memory queue
    look self-limiting when nothing bounds it. Bounding it is a durability
    decision (what to discard, and whether a discarded message is reported)
    and has not been made — see R-07.
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

    def __init__(self, db_path: str = "./state/mqtt_outbox.db") -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        #: A message whose publish failed, retried before the queue is read again.
        self._retry: tuple | None = None
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

    def _init_db(self) -> None:
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

    def _delete_from_db(self, row_id: int) -> None:
        """Remove a delivered message from the outbox."""
        try:
            with sqlite3.connect(self._db_path) as db:
                db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
                db.commit()
        except Exception as e:
            logger.debug("[MQTT] Outbox delete failed: %s", e)

    def _load_pending_from_db(self) -> None:
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
            await self._queue.put((topic, payload, retain, qos, row_id))
        else:
            # Best-effort: in-memory only
            await self._queue.put((topic, payload, retain, qos, -1))

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
