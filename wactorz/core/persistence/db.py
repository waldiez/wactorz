"""SQLite connection management and typed accessors."""

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from types import TracebackType
from typing import Any

from .schema import SCHEMA_SQL, SCHEMA_VERSION

logger = logging.getLogger(__name__)

# ── SQLite Connection Manager ──────────────────────────────────────────────


def _serialised(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run a read method holding the connection lock.

    Reads need the lock as much as writes do: with one shared connection, a read
    taken while another caller is mid-transaction sees that caller's uncommitted
    rows. They do not need the commit, which is why they use this rather than
    :meth:`WactorzDB.transaction`.
    """

    @wraps(fn)
    def wrapper(db: "WactorzDB", *args: Any, **kwargs: Any) -> Any:
        with db._lock:
            return fn(db, *args, **kwargs)

    return wrapper


def _with_attachments(row: dict[str, Any]) -> dict[str, Any]:
    """Decode a chat_log row's attachment references.

    Absent, empty and unreadable all mean the same thing to a caller: the turn
    had no files. A row predating the column reads as NULL, and one history
    request must not fail wholesale over a single malformed value — that would
    lose every other turn with it.
    """
    raw = row.get("attachments")
    if not raw:
        row["attachments"] = []
        return row
    try:
        decoded = json.loads(raw)
        row["attachments"] = decoded if isinstance(decoded, list) else []
    except Exception:
        row["attachments"] = []
    return row


class WactorzDB:
    """SQLite connection manager. One instance per process, created at startup.

    A single connection shared by every caller, reached two ways:

    - **writes** go through :meth:`transaction`, which takes the lock, commits
      at the outermost block and rolls back on failure;
    - **reads** carry the :func:`_serialised` decorator — the same lock, without
      the commit.

    **Why every write and not only the multi-statement ones.** One connection
    means ``BEGIN``/``COMMIT`` are global, so a helper that committed on its own
    would commit *across* an open transaction and publish half-finished work.
    Partial coverage is worse than none because it reads as safe. Uniform also
    means a helper that later grows a second statement is correct without anyone
    noticing it needed to be.

    Blocks nest: a helper called inside a caller's transaction leaves the commit
    to that caller, so composing them is safe.

    Not decoration — agent code runs ``exec``'d in this process and is told to
    push blocking work onto executor threads, so concurrency arrives from the
    code least able to be reviewed.
    """

    def __init__(self, db_path: str = "./state/wactorz.db") -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._depth = 0  # open transaction() blocks; only the outermost commits
        self._connect()
        self._init_schema()

    def _connect(self) -> None:
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,  # access is serialised by self._lock
            timeout=10.0,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent reads
        self._conn.execute("PRAGMA synchronous=NORMAL")  # fast + safe enough
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA cache_size=-8000")  # 8MB cache
        self._conn.row_factory = sqlite3.Row
        logger.info("[Persistence] SQLite opened: %s", self._path)

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        # Check/set version
        row = self.conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if not row:
            self.conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        # Direct, not via transaction(): this runs from __init__, before the
        # instance is reachable by anything that could contend for the lock.
        self.conn.commit()
        logger.info("[Persistence] Schema v%s ready", SCHEMA_VERSION)

    @property
    def conn(self) -> sqlite3.Connection:
        """The live connection. Raises if the database has been closed.

        Reads and single statements may use this directly; anything writing more
        than one statement wants :meth:`transaction` instead, so the whole unit
        is serialised rather than each statement on its own.
        """
        if self._conn is None:
            raise RuntimeError("WactorzDB connection is closed")
        return self._conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, Any, None]:
        """Exclusive use of the connection for the duration of a transaction.

        Commits on success, rolls back on failure. Reentrant, so a method that
        already holds it may call another that takes it.
        """
        with self._lock:
            self._depth += 1
            try:
                yield self.conn
            except Exception:
                if self._depth == 1:
                    self.conn.rollback()
                raise
            else:
                if self._depth == 1:
                    # The one place that commits. Every write helper reaches it
                    # through this block, so the outermost one commits and any
                    # nested block leaves the decision to it.
                    self.conn.commit()
            finally:
                self._depth -= 1

    def close(self) -> None:
        """Close the connection. Idempotent, and safe to call from any thread.

        Closing checkpoints the write-ahead log, so the ``-wal``/``-shm`` files
        do not survive; skipping it also leaves the database file locked on
        Windows.
        """
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "WactorzDB":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection on the way out.

        Returns ``None`` rather than a bool: a failure inside the block
        propagates. Closing a database is not grounds for swallowing whatever
        went wrong while using it.
        """
        self.close()

    # ── KV store (general purpose) ─────────────────────────────────────────

    def kv_set(self, agent: str, key: str, value: Any) -> None:
        """Insert or replace one key for one agent, and commit.

        ``value`` is JSON-encoded, so it must be serialisable; objects that are
        not fall back to ``str``, which round-trips as text rather than failing.
        """
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_store (agent, key, value, updated) VALUES (?, ?, ?, ?)",
                (agent, key, json.dumps(value, default=str), time.time()),
            )

    @_serialised
    def kv_get(self, agent: str, key: str, default: Any = None) -> Any:
        """Retrieve a value. Returns default if not found."""
        row = self.conn.execute(
            "SELECT value FROM kv_store WHERE agent=? AND key=?",
            (agent, key),
        ).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                return row[0]
        return default

    def kv_delete(self, agent: str, key: str) -> None:
        """Remove one key for one agent. Missing keys are not an error."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM kv_store WHERE agent=? AND key=?", (agent, key))

    def kv_purge_agent(self, agent: str) -> int:
        """Hard-delete EVERY kv_store row for a given agent. Used when an agent
        is permanently deleted (not just stopped) so its persisted state does
        not survive into the next process lifetime.

        Returns the number of rows removed.
        """
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM kv_store WHERE agent=?", (agent,))
        return cur.rowcount or 0

    @_serialised
    def kv_all(self, agent: str) -> dict[str, Any]:
        """Return all key-value pairs for an agent."""
        rows = self.conn.execute(
            "SELECT key, value FROM kv_store WHERE agent=?", (agent,)
        ).fetchall()
        result = {}
        for row in rows:
            try:
                result[row[0]] = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                result[row[0]] = row[1]
        return result

    # ── Time-series writes ─────────────────────────────────────────────────

    def write_sensor(
        self,
        ts: float,
        topic: str,
        entity_id: str,
        field: str,
        value: float | None,
        value_str: str = "",
        unit: str = "",
        agent: str = "",
        node: str = "",
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO sensor_readings "
                "(ts, topic, entity_id, field, value, value_str, unit, agent, node) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, topic, entity_id, field, value, value_str, unit, agent, node),
            )

    def write_sensor_batch(self, rows: list[tuple]) -> None:
        """Batch insert sensor readings. Each tuple matches write_sensor args."""
        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO sensor_readings "
                "(ts, topic, entity_id, field, value, value_str, unit, agent, node) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def write_detection(
        self,
        ts: float,
        agent: str,
        class_name: str,
        confidence: float,
        bbox: str = "",
        frame_id: int = 0,
        metadata: str = "{}",
        node: str = "",
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO detections "
                "(ts, agent, class_name, confidence, bbox, frame_id, metadata, node) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, agent, class_name, confidence, bbox, frame_id, metadata, node),
            )

    def write_ha_state(
        self,
        ts: float,
        entity_id: str,
        old_state: str,
        new_state: str,
        domain: str = "",
        attributes: str = "{}",
        context: str = "",
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO ha_state_changes "
                "(ts, entity_id, old_state, new_state, domain, attributes, context) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, entity_id, old_state, new_state, domain, attributes, context),
            )

    def write_actuation(
        self,
        ts: float,
        agent: str,
        domain: str,
        service: str,
        entity_id: str,
        payload: str = "{}",
        trigger: str = "{}",
        rule_id: str = "",
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO actuations "
                "(ts, agent, domain, service, entity_id, payload, trigger, rule_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, agent, domain, service, entity_id, payload, trigger, rule_id),
            )

    # ── Chat log (persistent feed for the UI) ──────────────────────────────

    def write_chat_log(
        self,
        ts: float,
        agent_name: str,
        role: str,
        content: str,
        session_id: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist a single chat turn so the UI feed can be rebuilt with real
        timestamps after a restart. The schema lives in :mod:`.schema`.

        ``attachments`` are stored on the row rather than resolved from disk on
        read: the files are immutable, so there is nothing to keep in step, and a
        chip should still say what was attached even once the file behind it is
        gone.
        """
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO chat_log (ts, agent_name, role, content, session_id, attachments) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    agent_name,
                    role,
                    content,
                    session_id,
                    json.dumps(attachments) if attachments else "",
                ),
            )

    def clear_chat_log(self, agent_name: str | None = None) -> int:
        """Delete chat_log rows. Pass agent_name to limit to one agent."""
        with self.transaction() as conn:
            if agent_name:
                cur = conn.execute("DELETE FROM chat_log WHERE agent_name=?", (agent_name,))
            else:
                cur = conn.execute("DELETE FROM chat_log")
        return cur.rowcount

    def clear_spawn_registry(self, agent_name: str | None = None) -> int:
        """Delete spawn_registry rows. Pass agent_name to limit to one agent."""
        with self.transaction() as conn:
            if agent_name:
                cur = conn.execute("DELETE FROM spawn_registry WHERE name=?", (agent_name,))
            else:
                cur = conn.execute("DELETE FROM spawn_registry")
        return cur.rowcount

    @_serialised
    def query_chat_log(
        self,
        agent_name: str | None = None,
        role: str | None = None,
        since: float | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return chat_log rows newest-first as plain dicts. Used by the
        /api/chats endpoint and by feed_handler to seed the UI feed.
        """
        sql = "SELECT id, ts, agent_name, role, content, session_id, attachments FROM chat_log"
        clauses: list[str] = []
        params: list = []
        if agent_name:
            clauses.append("agent_name = ?")
            params.append(agent_name)
        if role:
            clauses.append("role = ?")
            params.append(role)
        if since is not None:
            clauses.append("ts > ?")
            params.append(float(since))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        # rows are sqlite3.Row because connect() set row_factory; coerce to dict
        return [_with_attachments(dict(r)) for r in rows]

    # ── Time-series queries (for ML agents) ────────────────────────────────

    @_serialised
    def query_sensor(
        self,
        hours: float = 24,
        topic: str | None = None,
        entity_id: str | None = None,
        field: str | None = None,
        limit: int = 100_000,
    ) -> list[dict[str, Any]]:
        """Query sensor readings by time range, topic, entity, or field.
        Returns list of dicts suitable for pandas DataFrame conversion.
        """
        since = time.time() - (hours * 3600)
        conditions = ["ts >= ?"]
        params: list = [since]

        if topic:
            conditions.append("topic = ?")
            params.append(topic)
        if entity_id:
            conditions.append("entity_id = ?")
            params.append(entity_id)
        if field:
            conditions.append("field = ?")
            params.append(field)

        where = " AND ".join(conditions)
        rows = self.conn.execute(
            f"SELECT ts, topic, entity_id, field, value, value_str, unit, agent, node "
            f"FROM sensor_readings WHERE {where} ORDER BY ts ASC LIMIT ?",
            [*params, limit],
        ).fetchall()

        return [dict(r) for r in rows]

    @_serialised
    def query_detections(
        self,
        hours: float = 24,
        agent: str | None = None,
        class_name: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 50_000,
    ) -> list[dict[str, Any]]:
        since = time.time() - (hours * 3600)
        conditions = ["ts >= ?", "confidence >= ?"]
        params: list = [since, min_confidence]

        if agent:
            conditions.append("agent = ?")
            params.append(agent)
        if class_name:
            conditions.append("class_name = ?")
            params.append(class_name)

        where = " AND ".join(conditions)
        rows = self.conn.execute(
            f"SELECT ts, agent, class_name, confidence, bbox, metadata, node "
            f"FROM detections WHERE {where} ORDER BY ts ASC LIMIT ?",
            [*params, limit],
        ).fetchall()

        return [dict(r) for r in rows]

    @_serialised
    def query_ha_states(
        self,
        hours: float = 24,
        entity_id: str | None = None,
        domain: str | None = None,
        limit: int = 50_000,
    ) -> list[dict[str, Any]]:
        since = time.time() - (hours * 3600)
        conditions = ["ts >= ?"]
        params: list = [since]

        if entity_id:
            conditions.append("entity_id = ?")
            params.append(entity_id)
        if domain:
            conditions.append("domain = ?")
            params.append(domain)

        where = " AND ".join(conditions)
        rows = self.conn.execute(
            f"SELECT ts, entity_id, old_state, new_state, domain, attributes "
            f"FROM ha_state_changes WHERE {where} ORDER BY ts ASC LIMIT ?",
            [*params, limit],
        ).fetchall()

        return [dict(r) for r in rows]

    @_serialised
    def query_actuations(
        self,
        hours: float = 24,
        entity_id: str | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        since = time.time() - (hours * 3600)
        conditions = ["ts >= ?"]
        params: list = [since]

        if entity_id:
            conditions.append("entity_id = ?")
            params.append(entity_id)

        where = " AND ".join(conditions)
        rows = self.conn.execute(
            f"SELECT ts, agent, domain, service, entity_id, payload, trigger, rule_id "
            f"FROM actuations WHERE {where} ORDER BY ts ASC LIMIT ?",
            [*params, limit],
        ).fetchall()

        return [dict(r) for r in rows]

    # ── Retention / cleanup ────────────────────────────────────────────────

    def prune_old_data(self, days: int = 30) -> int:
        """Delete time-series data older than N days. Run periodically."""
        cutoff = time.time() - (days * 86400)
        tables = ["sensor_readings", "detections", "ha_state_changes", "actuations"]
        total = 0
        with self.transaction() as conn:
            for table in tables:
                cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                total += cur.rowcount
        if total:
            logger.info("[Persistence] Pruned %s rows older than %sd", total, days)
        return total

    @_serialised
    def stats(self) -> dict[str, Any]:
        """Return row counts for all time-series tables."""
        tables = ["sensor_readings", "detections", "ha_state_changes", "actuations"]
        result = {}
        for table in tables:
            row = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            result[table] = row[0] if row else 0
        return result
