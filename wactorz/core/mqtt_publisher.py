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
import threading
import time
from pathlib import Path

from .topics import publish_topic_error

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

    #: How often the WAL is folded back into the database, off the event loop.
    #: Frequent enough that it almost always runs while nothing is being
    #: published, which is the point — a checkpoint that coincides with a write
    #: makes the writer wait for it.
    CHECKPOINT_INTERVAL_S = 60.0

    #: Days an undelivered message is stored before it expires; 0 keeps it until
    #: delivered. The server passes its `WACTORZ_RETENTION_OUTBOX_DAYS` setting;
    #: this is the same default, for a publisher built without one.
    DEAD_LETTER_DAYS = 7.0

    #: How many times in a row one message may fail to publish on a live
    #: connection before it is dropped as one the broker will never take — too
    #: large, malformed. A flaky link can also fail just after connecting, so
    #: this is generous: with the 10s publish timeout and the reconnect backoff,
    #: five attempts hold the queue for about a minute.
    POISON_AFTER = 5

    #: How large the WAL may get before SQLite checkpoints it *inline*, on
    #: whichever commit trips the threshold. Raised well above the 1000-page
    #: default so the scheduled checkpoint normally gets there first.
    #:
    #: Deliberately not 0. Disabling the inline checkpoint entirely would make
    #: the background thread the only one, and a thread that dies or wedges then
    #: means the WAL grows without bound on an SD card — trading a latency spike
    #: for a full disk. A high threshold degrades to rare-and-spiky instead,
    #: which is what the default already was.
    WAL_AUTOCHECKPOINT_PAGES = 4000

    def __init__(
        self,
        db_path: str | os.PathLike[str] = "./state/mqtt_outbox.db",
        dead_letter_days: float = DEAD_LETTER_DAYS,
    ) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_QUEUED)
        #: How many messages the cap has discarded, for the log and for tests.
        self._dropped = 0
        #: A message whose publish failed, retried before the queue is read again.
        self._retry: tuple | None = None
        #: How many times in a row `_retry` has failed. It counts that one
        #: message: a fresh one starts again, so failures never add up across
        #: messages and turn a flaky link into a false verdict.
        self._retry_failures = 0
        #: Topics already warned about as unsendable, so a caller in a loop
        #: warns once rather than on every call.
        self._refused_topics: set[str] = set()
        self._task: asyncio.Task | None = None
        self._available = False
        self._db_path = db_path
        self._dead_letter_days = dead_letter_days
        #: The outbox handle, opened on first use and kept -- see :meth:`_connect`.
        #: Not opened here: a constructor must not touch the disk.
        self._db: sqlite3.Connection | None = None
        #: Guards the handle. The checkpoint runs on a worker thread, so the
        #: loop's writes and that thread must not reach SQLite at once. Mirrors
        #: how `WactorzDB` serialises its own shared connection — reentrant for
        #: the same reason: a failed statement drops the handle from inside the
        #: `except` of a method already holding this.
        self._db_lock = threading.RLock()
        self._checkpoint_task: asyncio.Task | None = None
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
        cls,
        broker: str,
        port: int,
        db_path: str | os.PathLike[str] = "./state/mqtt_outbox.db",
        dead_letter_days: float = DEAD_LETTER_DAYS,
    ) -> MQTTPublisher:
        """Build a publisher and connect it, or return one that quietly no-ops."""
        pub = cls(db_path=db_path, dead_letter_days=dead_letter_days)
        try:
            import aiomqtt  # noqa: F401  # pylint: disable=unused-import

            pub._init_db()
            # Before the replay: an expired message is dropped, not retried once more.
            pub._expire()
            pub._load_pending_from_db()
            pub._task = asyncio.create_task(pub._run(broker, port))
            pub._checkpoint_task = asyncio.create_task(pub._checkpoint_loop())
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

    def _connect(self) -> sqlite3.Connection:
        """The outbox handle, opened once and kept for the life of the publisher.

        A QoS-1 message costs two statements — one to enqueue, one when delivery
        succeeds — and each used to open, write, commit and close its own
        connection. On the SD card a Raspberry Pi runs from, that cycle measures
        ~16ms, and it is paid on the control plane rather than on telemetry:
        `nodes/` and `agents/by-name/` are forced up to QoS 1 in `publish`, so
        every spawn, stop and migration ack stops every actor in the process for
        that long, MQTT keepalive included.

        Keeping the handle is half the fix and the pragmas are the other half,
        and **neither works alone**. `synchronous=NORMAL` stops the fsync on
        every commit, but a connection that does not outlive the statement
        cannot amortise anything; and WAL set per-connection is *slower* than
        the rollback journal it replaces, because it pays `-wal`/`-shm` setup on
        every connect and a checkpoint when the last connection closes. Measured
        on the same SD card: reconnecting at WAL/NORMAL is 0.8x — a regression —
        a kept handle at the defaults is 1.2x, and the two together are 316x.

        Opened lazily rather than in `_init_db`, because the outbox is also read
        and written by callers that never ran it — a publisher rebuilt after a
        restart replays through `_load_pending_from_db` alone.

        Dropped again by `_close_db` whenever a statement fails: **a connection
        per statement healed itself, and a kept one has to be told to.** A handle
        that has gone bad — the file replaced underneath it, a disk error — would
        otherwise fail every write for the life of the process, turning a
        transient fault into a permanent one.
        """
        if self._db is None:
            db = sqlite3.connect(self._db_path, check_same_thread=False)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            # Only bites when a second process has the same file open, which is
            # also the case WAL makes survivable rather than an instant "database
            # is locked".
            db.execute("PRAGMA busy_timeout=5000")
            db.execute(f"PRAGMA wal_autocheckpoint={self.WAL_AUTOCHECKPOINT_PAGES}")
            self._db = db
        return self._db

    def _checkpoint(self) -> None:
        """Fold the WAL back into the database. **Runs on a worker thread.**

        SQLite-specific, and worth deleting rather than porting if the outbox
        ever moves to a pooled engine or a server-based store: nothing else here
        needs a client-side checkpointer.

        This is the part that must not happen on the event loop. Left to SQLite,
        a checkpoint fires inline on whichever commit crosses the threshold, and
        on an SD card that measures ~62ms with everything else in the process
        stopped for it — worse than the per-statement fsync this class stopped
        paying, just rarer. Measured on a Pi: with the inline checkpoint the
        worst write is 61.7ms against a 0.032ms median; with it deferred the
        worst is 0.074ms.

        TRUNCATE rather than PASSIVE: it resets the WAL to nothing, which is what
        keeps the file bounded. It needs the writers out of the way, which is
        what the lock is for, and it is why this runs on a timer while the
        publisher is usually idle rather than after a write.
        """
        with self._db_lock:
            if self._db is None:
                return
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as e:
                logger.debug("[MQTT] Outbox checkpoint failed: %s", e)

    def _expire(self) -> None:
        """Remove stored messages still undelivered after `dead_letter_days`, by topic.

        Without this a message the broker never accepts is kept for ever and
        replayed on every start. Expiring one can lose it, so the age is long and
        each expiry is a warning. Runs on a worker thread, and once before the
        replay at startup.

        Only the stored copy goes. A copy already in memory is still retried while
        this process runs, and delivered if the broker comes back; what expiry ends
        is the retry after a restart. So the warning can come before a delivery.
        """
        if self._dead_letter_days <= 0:
            return
        cutoff = time.time() - self._dead_letter_days * 86400
        expired: list[tuple[str, int]] = []
        try:
            with self._db_lock:
                db = self._connect()
                expired = db.execute(
                    "SELECT topic, COUNT(*) FROM outbox WHERE ts < ? GROUP BY topic", (cutoff,)
                ).fetchall()
                if expired:
                    db.execute("DELETE FROM outbox WHERE ts < ?", (cutoff,))
                    db.commit()
        except Exception as e:
            logger.debug("[MQTT] Outbox expiry failed: %s", e)
            self._close_db()
            return
        for topic, count in expired:
            logger.warning(
                "[MQTT] outbox: expired %d message(s) for %s, undelivered after %g days;"
                " not retried after a restart",
                count,
                topic,
                self._dead_letter_days,
            )

    async def _checkpoint_loop(self) -> None:
        """Expire dead letters, then checkpoint, on a timer and off the loop, until cancelled.

        Expiry first, so the checkpoint folds its deletes in the same pass.
        """
        while True:
            await asyncio.sleep(self.CHECKPOINT_INTERVAL_S)
            await asyncio.to_thread(self._expire)
            await asyncio.to_thread(self._checkpoint)

    def _close_db(self) -> None:
        """Drop the outbox handle, so the next use opens a fresh one."""
        with self._db_lock:
            if self._db is None:
                return
            try:
                self._db.close()
            except Exception as e:
                logger.debug("[MQTT] Outbox close failed: %s", e)
            self._db = None

    def _init_db(self) -> None:
        """Create the outbox table if it doesn't exist."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._db_lock:
            db = self._connect()
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
            with self._db_lock:
                db = self._connect()
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
        except Exception as e:
            logger.debug("[MQTT] Outbox write failed: %s", e)
            self._close_db()
            return -1
        else:
            return cur.lastrowid or 0

    def _delete_from_db(self, row_id: int) -> None:
        """Remove a delivered message from the outbox."""
        try:
            with self._db_lock:
                db = self._connect()
                db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
                db.commit()
        except Exception as e:
            logger.debug("[MQTT] Outbox delete failed: %s", e)
            self._close_db()

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

    def _discard(self, item: tuple, from_queue: bool, reason: str) -> None:
        """Give up on a message that can never be sent, so the ones behind it can be.

        Its stored row goes too; left in the outbox it would be replayed after a
        restart and stall the queue again.
        """
        topic, _payload, _retain, _qos, row_id = item
        if from_queue:
            self._queue.task_done()
        if row_id >= 0:
            self._delete_from_db(row_id)
        logger.warning("[MQTT] outbox: dropped a message for %r — %s", topic, reason)

    def _hold_for_retry(self, item: tuple, from_queue: bool, error: Exception) -> None:
        """Keep a failed message to send first after reconnecting, unless it keeps failing.

        A publish that fails on a live connection is usually the link, and the
        message deserves another try ahead of the rest. One that fails
        `POISON_AFTER` times in a row is the message — a broker that drops the
        connection every time it arrives — and holding it would hold everything.
        """
        if from_queue:
            self._queue.task_done()
        failures = 1 if from_queue else self._retry_failures + 1
        if failures >= self.POISON_AFTER:
            self._discard(item, False, f"its publish failed {failures} times in a row: {error}")
            self._retry, self._retry_failures = None, 0
            return
        logger.warning("[MQTT] Publish failed: %s — retrying it first", error)
        self._retry, self._retry_failures = item, failures

    def _load_pending_from_db(self) -> None:
        """On startup, reload undelivered QoS 1 messages into the in-memory queue."""
        try:
            with self._db_lock:
                rows = (
                    self._connect()
                    .execute("SELECT id, topic, payload, retain, qos FROM outbox ORDER BY id")
                    .fetchall()
                )
            if rows:
                logger.info("[MQTT] Replaying %s undelivered message(s) from outbox", len(rows))
            for row_id, topic, payload, retain, qos in rows:
                self._enqueue((topic, payload, bool(retain), qos, row_id))
        except Exception as e:
            logger.debug("[MQTT] Outbox load failed: %s", e)
            self._close_db()

    # ── Public API ─────────────────────────────────────────────────────────

    async def publish(self, topic: str, payload, retain: bool = False, qos: int = 0) -> None:
        """Queue a message for delivery. Returns without waiting for the broker."""
        if not self._available:
            return

        problem = publish_topic_error(topic)
        if problem:
            # Refused here rather than queued: the queue sends in order and
            # retries a failure first, so a topic that can never be sent would
            # hold up every message behind it, and a stored one would do so
            # again after a restart.
            if topic not in self._refused_topics and len(self._refused_topics) < 256:
                self._refused_topics.add(topic)
                logger.warning("[MQTT] refused to publish to %r: %s", topic, problem)
            else:
                logger.debug("[MQTT] refused to publish to %r: %s", topic, problem)
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
        if self._checkpoint_task:
            self._checkpoint_task.cancel()
            await asyncio.gather(self._checkpoint_task, return_exceptions=True)
            self._checkpoint_task = None
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
        # Closing checkpoints the WAL back into the database, so a shutdown does
        # not leave a `-wal` beside it for the next start to recover from.
        self._close_db()

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
                        except (ValueError, TypeError) as refused:
                            # paho refuses the message itself before sending it — a
                            # wildcard or empty topic, one over 65535 bytes, a payload
                            # it cannot encode — and would refuse it again on every
                            # retry. It goes now; the connection, which is fine, stays.
                            self._discard(item, from_queue, f"refused before sending: {refused}")
                            continue
                        except Exception as pub_err:
                            self._hold_for_retry(item, from_queue, pub_err)
                            raise  # trigger reconnect
                        # Only remove from queue AFTER successful publish.
                        # `task_done` belongs to a `get`, so it is skipped
                        # for a retry that never went back on the queue.
                        if from_queue:
                            self._queue.task_done()
                        # Remove from SQLite outbox if it was persisted
                        if row_id >= 0:
                            self._delete_from_db(row_id)
                        self._retry_failures = 0
                        # Reset backoff and error dedup only after a successful publish
                        backoff = 1.0
                        _last_exc_str = None

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
