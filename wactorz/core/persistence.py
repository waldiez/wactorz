"""
wactorz.core.persistence — Unified persistence layer.

Replaces pickle-only storage with a 3-tier architecture:
  - SQLite:  durable structured data (spawn registry, rules, facts, contracts, time-series)
  - Redis:   ephemeral fast-access data (conversation history, observed samples, metrics)
  - Pickle:  arbitrary Python objects (agent.state, ML models, cv2 captures)

MQTT is NOT replaced — it remains the real-time messaging layer.

The PersistenceAPI class provides backward-compatible persist()/recall() that route
to the correct store based on key prefixes. Existing agent code works without changes.
"""

import json
import logging
import pickle
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── SQLite Schema ──────────────────────────────────────────────────────────

_SCHEMA_VERSION = 1

# Column DEFAULTs below use ((julianday('now') - 2440587.5) * 86400.0) for a
# sub-second Unix timestamp. We deliberately avoid unixepoch('subsec'), which
# requires SQLite >= 3.42 (2023): SQLite resolves a DEFAULT expression's
# functions when it compiles ANY write to the table (even when the column value
# is supplied), so an unavailable function breaks every INSERT — not just
# default-relying ones. julianday() is core since SQLite 3.0 (2004), so this
# works on every platform/version we could run on.
_SCHEMA_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- Key-value store for structured agent data (replaces most pickle usage)
-- Each agent gets its own namespace via the 'agent' column
CREATE TABLE IF NOT EXISTS kv_store (
    agent   TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,           -- JSON-encoded
    updated REAL NOT NULL DEFAULT ((julianday('now') - 2440587.5) * 86400.0),
    PRIMARY KEY (agent, key)
);

-- Spawn registry — which agents should be running and their configs
CREATE TABLE IF NOT EXISTS spawn_registry (
    name       TEXT PRIMARY KEY,
    config     TEXT NOT NULL,         -- JSON spawn config
    node       TEXT DEFAULT '',       -- remote node name (empty = local)
    created_at REAL NOT NULL DEFAULT ((julianday('now') - 2440587.5) * 86400.0),
    updated_at REAL NOT NULL DEFAULT ((julianday('now') - 2440587.5) * 86400.0)
);

-- Pipeline rules — reactive rules with their agent lists
CREATE TABLE IF NOT EXISTS pipeline_rules (
    rule_id    TEXT PRIMARY KEY,
    task       TEXT NOT NULL,          -- original user request
    agents     TEXT NOT NULL,          -- JSON array of agent names
    created_at REAL NOT NULL DEFAULT ((julianday('now') - 2440587.5) * 86400.0)
);

-- User facts — durable facts extracted from conversations
CREATE TABLE IF NOT EXISTS user_facts (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated REAL NOT NULL DEFAULT ((julianday('now') - 2440587.5) * 86400.0)
);

-- Topic contracts — TopicBus registry (survives restarts without retained MQTT)
CREATE TABLE IF NOT EXISTS topic_contracts (
    name             TEXT PRIMARY KEY,
    publishes        TEXT DEFAULT '[]',   -- JSON array
    subscribes       TEXT DEFAULT '[]',   -- JSON array
    triggers_when    TEXT DEFAULT '{}',   -- JSON dict
    produces_schema  TEXT DEFAULT '{}',   -- JSON dict
    consumes_schema  TEXT DEFAULT '{}',   -- JSON dict
    observed_samples TEXT DEFAULT '{}',   -- JSON dict
    node             TEXT DEFAULT '',
    actor_id         TEXT DEFAULT '',
    updated          REAL NOT NULL DEFAULT ((julianday('now') - 2440587.5) * 86400.0)
);

-- Notification webhook URLs
CREATE TABLE IF NOT EXISTS webhook_urls (
    service TEXT PRIMARY KEY,          -- discord, slack, telegram
    url     TEXT NOT NULL,
    updated REAL NOT NULL DEFAULT ((julianday('now') - 2440587.5) * 86400.0)
);

-- Plan cache — cached planner decompositions (with TTL)
CREATE TABLE IF NOT EXISTS plan_cache (
    cache_key  TEXT PRIMARY KEY,
    plan       TEXT NOT NULL,          -- JSON array of steps
    workers    TEXT DEFAULT '[]',      -- JSON array of worker names at cache time
    created_at REAL NOT NULL DEFAULT ((julianday('now') - 2440587.5) * 86400.0)
);

-- ══════════════════════════════════════════════════════════════════════════
-- TIME-SERIES TABLES — for device data collection and ML training
-- ══════════════════════════════════════════════════════════════════════════

-- Sensor readings — numeric values from any MQTT topic
-- Covers: temperature, humidity, energy, pressure, lux, CO2, etc.
CREATE TABLE IF NOT EXISTS sensor_readings (
    ts        REAL NOT NULL,           -- Unix timestamp (float, sub-second precision)
    topic     TEXT NOT NULL,           -- MQTT topic: sensors/data, homeassistant/state_changes/...
    entity_id TEXT DEFAULT '',         -- HA entity_id or agent-defined identifier
    field     TEXT NOT NULL,           -- field name within the payload: temp, humidity, state
    value     REAL,                    -- numeric value (NULL for non-numeric)
    value_str TEXT DEFAULT '',         -- string value for non-numeric fields (on/off, etc.)
    unit      TEXT DEFAULT '',         -- C, %, lux, W, etc.
    agent     TEXT DEFAULT '',         -- which agent published this
    node      TEXT DEFAULT ''          -- which node the agent runs on
);

CREATE INDEX IF NOT EXISTS idx_sensor_ts       ON sensor_readings (ts);
CREATE INDEX IF NOT EXISTS idx_sensor_topic_ts ON sensor_readings (topic, ts);
CREATE INDEX IF NOT EXISTS idx_sensor_entity_ts ON sensor_readings (entity_id, ts);
CREATE INDEX IF NOT EXISTS idx_sensor_field_ts ON sensor_readings (field, ts);

-- Object detections — from YOLO/camera agents
CREATE TABLE IF NOT EXISTS detections (
    ts         REAL NOT NULL,
    agent      TEXT NOT NULL,          -- camera-detect, yolo-agent, etc.
    class_name TEXT NOT NULL,          -- person, car, dog, laptop, etc.
    confidence REAL NOT NULL,          -- 0.0 to 1.0
    bbox       TEXT DEFAULT '',        -- JSON: [x1, y1, x2, y2] or empty
    frame_id   INTEGER DEFAULT 0,     -- frame counter for dedup
    metadata   TEXT DEFAULT '{}',      -- JSON: extra fields (target, objects list, etc.)
    node       TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_detect_ts       ON detections (ts);
CREATE INDEX IF NOT EXISTS idx_detect_class_ts ON detections (class_name, ts);
CREATE INDEX IF NOT EXISTS idx_detect_agent_ts ON detections (agent, ts);

-- HA state changes — every state_changed event from the bridge
CREATE TABLE IF NOT EXISTS ha_state_changes (
    ts        REAL NOT NULL,
    entity_id TEXT NOT NULL,
    old_state TEXT DEFAULT '',
    new_state TEXT NOT NULL,
    domain    TEXT DEFAULT '',         -- light, switch, sensor, climate, etc.
    attributes TEXT DEFAULT '{}',     -- JSON: brightness, color_temp, etc.
    context   TEXT DEFAULT ''          -- HA context_id for correlation
);

CREATE INDEX IF NOT EXISTS idx_ha_entity_ts ON ha_state_changes (entity_id, ts);
CREATE INDEX IF NOT EXISTS idx_ha_domain_ts ON ha_state_changes (domain, ts);

-- Actuations — every HA service call made by actuator agents
CREATE TABLE IF NOT EXISTS actuations (
    ts        REAL NOT NULL,
    agent     TEXT NOT NULL,           -- which actuator fired
    domain    TEXT NOT NULL,           -- light, switch, climate
    service   TEXT NOT NULL,           -- turn_on, turn_off, set_temperature
    entity_id TEXT NOT NULL,
    payload   TEXT DEFAULT '{}',       -- JSON: service call data
    trigger   TEXT DEFAULT '{}',       -- JSON: the MQTT payload that caused this
    rule_id   TEXT DEFAULT ''          -- pipeline rule that owns this actuator
);

CREATE INDEX IF NOT EXISTS idx_actuation_ts     ON actuations (ts);
CREATE INDEX IF NOT EXISTS idx_actuation_entity ON actuations (entity_id, ts);

-- Chat log — every user/assistant turn the monitor server sees.
-- This is what backs the UI feed across restarts. Without it, the feed
-- has to be reconstructed from kv_store conversation_history, which has
-- no real timestamps (turns are positional within the JSON blob).
CREATE TABLE IF NOT EXISTS chat_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,             -- Unix timestamp of the turn
    agent_name TEXT    NOT NULL,             -- agent that produced/received the message
    role       TEXT    NOT NULL,             -- 'user' | 'assistant'
    content    TEXT    NOT NULL,
    session_id TEXT    DEFAULT ''            -- optional grouping (actor_id or custom)
);

CREATE INDEX IF NOT EXISTS idx_chatlog_ts          ON chat_log (ts);
CREATE INDEX IF NOT EXISTS idx_chatlog_agent_ts    ON chat_log (agent_name, ts);
"""


# ── SQLite Connection Manager ──────────────────────────────────────────────

class WactorzDB:
    """
    Thread-safe SQLite connection manager.
    One instance per process, created at startup.
    """

    def __init__(self, db_path: str = "./state/wactorz.db"):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()
        self._init_schema()

    def _connect(self):
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,    # safe with WAL mode
            timeout=10.0,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")       # concurrent reads
        self._conn.execute("PRAGMA synchronous=NORMAL")      # fast + safe enough
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA cache_size=-8000")        # 8MB cache
        self._conn.row_factory = sqlite3.Row
        logger.info(f"[Persistence] SQLite opened: {self._path}")

    def _init_schema(self):
        self._conn.executescript(_SCHEMA_SQL)
        # Check/set version
        row = self._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if not row:
            self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
        self._conn.commit()
        logger.info(f"[Persistence] Schema v{_SCHEMA_VERSION} ready")

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── KV store (general purpose) ─────────────────────────────────────────

    def kv_set(self, agent: str, key: str, value: Any):
        """Store a JSON-serializable value."""
        self._conn.execute(
            "INSERT OR REPLACE INTO kv_store (agent, key, value, updated) "
            "VALUES (?, ?, ?, ?)",
            (agent, key, json.dumps(value, default=str), time.time()),
        )
        self._conn.commit()

    def kv_get(self, agent: str, key: str, default: Any = None) -> Any:
        """Retrieve a value. Returns default if not found."""
        row = self._conn.execute(
            "SELECT value FROM kv_store WHERE agent=? AND key=?",
            (agent, key),
        ).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                return row[0]
        return default

    def kv_delete(self, agent: str, key: str):
        self._conn.execute(
            "DELETE FROM kv_store WHERE agent=? AND key=?", (agent, key)
        )
        self._conn.commit()

    def kv_purge_agent(self, agent: str) -> int:
        """
        Hard-delete EVERY kv_store row for a given agent. Used when an agent
        is permanently deleted (not just stopped) so its persisted state does
        not survive into the next process lifetime.

        Returns the number of rows removed.
        """
        cur = self._conn.execute(
            "DELETE FROM kv_store WHERE agent=?", (agent,)
        )
        self._conn.commit()
        return cur.rowcount or 0

    def kv_all(self, agent: str) -> dict:
        """Return all key-value pairs for an agent."""
        rows = self._conn.execute(
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

    def write_sensor(self, ts: float, topic: str, entity_id: str,
                     field: str, value: Optional[float], value_str: str = "",
                     unit: str = "", agent: str = "", node: str = ""):
        self._conn.execute(
            "INSERT INTO sensor_readings "
            "(ts, topic, entity_id, field, value, value_str, unit, agent, node) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, topic, entity_id, field, value, value_str, unit, agent, node),
        )

    def write_sensor_batch(self, rows: list[tuple]):
        """Batch insert sensor readings. Each tuple matches write_sensor args."""
        self._conn.executemany(
            "INSERT INTO sensor_readings "
            "(ts, topic, entity_id, field, value, value_str, unit, agent, node) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def write_detection(self, ts: float, agent: str, class_name: str,
                        confidence: float, bbox: str = "", frame_id: int = 0,
                        metadata: str = "{}", node: str = ""):
        self._conn.execute(
            "INSERT INTO detections "
            "(ts, agent, class_name, confidence, bbox, frame_id, metadata, node) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, agent, class_name, confidence, bbox, frame_id, metadata, node),
        )
        self._conn.commit()

    def write_ha_state(self, ts: float, entity_id: str, old_state: str,
                       new_state: str, domain: str = "", attributes: str = "{}",
                       context: str = ""):
        self._conn.execute(
            "INSERT INTO ha_state_changes "
            "(ts, entity_id, old_state, new_state, domain, attributes, context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, entity_id, old_state, new_state, domain, attributes, context),
        )
        self._conn.commit()

    def write_actuation(self, ts: float, agent: str, domain: str, service: str,
                        entity_id: str, payload: str = "{}",
                        trigger: str = "{}", rule_id: str = ""):
        self._conn.execute(
            "INSERT INTO actuations "
            "(ts, agent, domain, service, entity_id, payload, trigger, rule_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, agent, domain, service, entity_id, payload, trigger, rule_id),
        )
        self._conn.commit()

    # ── Chat log (persistent feed for the UI) ──────────────────────────────

    def write_chat_log(self, ts: float, agent_name: str, role: str,
                       content: str, session_id: str = ""):
        """
        Persist a single chat turn so the UI feed can be rebuilt with real
        timestamps after a restart. The schema is created in init_state.sql.
        """
        self._conn.execute(
            "INSERT INTO chat_log (ts, agent_name, role, content, session_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, agent_name, role, content, session_id),
        )
        self._conn.commit()

    def clear_chat_log(self, agent_name: Optional[str] = None) -> int:
        """Delete chat_log rows. Pass agent_name to limit to one agent."""
        if agent_name:
            cur = self._conn.execute(
                "DELETE FROM chat_log WHERE agent_name=?", (agent_name,)
            )
        else:
            cur = self._conn.execute("DELETE FROM chat_log")
        self._conn.commit()
        return cur.rowcount

    def clear_spawn_registry(self, agent_name: Optional[str] = None) -> int:
        """Delete spawn_registry rows. Pass agent_name to limit to one agent."""
        if agent_name:
            cur = self._conn.execute(
                "DELETE FROM spawn_registry WHERE name=?", (agent_name,)
            )
        else:
            cur = self._conn.execute("DELETE FROM spawn_registry")
        self._conn.commit()
        return cur.rowcount

    def query_chat_log(self, agent_name: Optional[str] = None,
                       role: Optional[str] = None,
                       since: Optional[float] = None,
                       limit: int = 200) -> list[dict]:
        """
        Return chat_log rows newest-first as plain dicts. Used by the
        /api/chats endpoint and by feed_handler to seed the UI feed.
        """
        sql    = "SELECT id, ts, agent_name, role, content, session_id FROM chat_log"
        clauses: list[str] = []
        params:  list      = []
        if agent_name:
            clauses.append("agent_name = ?"); params.append(agent_name)
        if role:
            clauses.append("role = ?");       params.append(role)
        if since is not None:
            clauses.append("ts > ?");         params.append(float(since))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        # rows are sqlite3.Row because connect() set row_factory; coerce to dict
        return [dict(r) for r in rows]


    # ── Time-series queries (for ML agents) ────────────────────────────────

    def query_sensor(
        self,
        hours: float = 24,
        topic: Optional[str] = None,
        entity_id: Optional[str] = None,
        field: Optional[str] = None,
        limit: int = 100_000,
    ) -> list[dict]:
        """
        Query sensor readings by time range, topic, entity, or field.
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
        rows = self._conn.execute(
            f"SELECT ts, topic, entity_id, field, value, value_str, unit, agent, node "
            f"FROM sensor_readings WHERE {where} ORDER BY ts ASC LIMIT ?",
            params + [limit],
        ).fetchall()

        return [dict(r) for r in rows]

    def query_detections(
        self,
        hours: float = 24,
        agent: Optional[str] = None,
        class_name: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 50_000,
    ) -> list[dict]:
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
        rows = self._conn.execute(
            f"SELECT ts, agent, class_name, confidence, bbox, metadata, node "
            f"FROM detections WHERE {where} ORDER BY ts ASC LIMIT ?",
            params + [limit],
        ).fetchall()

        return [dict(r) for r in rows]

    def query_ha_states(
        self,
        hours: float = 24,
        entity_id: Optional[str] = None,
        domain: Optional[str] = None,
        limit: int = 50_000,
    ) -> list[dict]:
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
        rows = self._conn.execute(
            f"SELECT ts, entity_id, old_state, new_state, domain, attributes "
            f"FROM ha_state_changes WHERE {where} ORDER BY ts ASC LIMIT ?",
            params + [limit],
        ).fetchall()

        return [dict(r) for r in rows]

    def query_actuations(
        self,
        hours: float = 24,
        entity_id: Optional[str] = None,
        limit: int = 10_000,
    ) -> list[dict]:
        since = time.time() - (hours * 3600)
        conditions = ["ts >= ?"]
        params: list = [since]

        if entity_id:
            conditions.append("entity_id = ?")
            params.append(entity_id)

        where = " AND ".join(conditions)
        rows = self._conn.execute(
            f"SELECT ts, agent, domain, service, entity_id, payload, trigger, rule_id "
            f"FROM actuations WHERE {where} ORDER BY ts ASC LIMIT ?",
            params + [limit],
        ).fetchall()

        return [dict(r) for r in rows]

    # ── Retention / cleanup ────────────────────────────────────────────────

    def prune_old_data(self, days: int = 30):
        """Delete time-series data older than N days. Run periodically."""
        cutoff = time.time() - (days * 86400)
        tables = ["sensor_readings", "detections", "ha_state_changes", "actuations"]
        total = 0
        for table in tables:
            cur = self._conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            total += cur.rowcount
        self._conn.commit()
        if total:
            logger.info(f"[Persistence] Pruned {total} rows older than {days}d")
        return total

    def stats(self) -> dict:
        """Return row counts for all time-series tables."""
        tables = ["sensor_readings", "detections", "ha_state_changes", "actuations"]
        result = {}
        for table in tables:
            row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            result[table] = row[0] if row else 0
        return result


# ── Redis Wrapper (optional) ───────────────────────────────────────────────

class RedisStore:
    """
    Optional Redis wrapper for ephemeral fast-access data.
    Falls back to an in-memory dict if Redis is unavailable.
    """

    def __init__(self, url: str = "redis://localhost:6379", prefix: str = "wactorz:"):
        self._prefix = prefix
        self._redis = None
        self._fallback: dict[str, Any] = {}
        self._using_fallback = False

        try:
            import redis
            self._redis = redis.Redis.from_url(url, decode_responses=True)
            self._redis.ping()
            logger.info(f"[Persistence] Redis connected: {url}")
        except Exception as e:
            logger.info(f"[Persistence] Redis unavailable ({e}), using in-memory fallback")
            self._redis = None
            self._using_fallback = True

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Store a JSON-serializable value with optional TTL (seconds)."""
        serialized = json.dumps(value, default=str)
        if self._redis:
            if ttl:
                self._redis.setex(self._key(key), ttl, serialized)
            else:
                self._redis.set(self._key(key), serialized)
        else:
            self._fallback[key] = {
                "value": serialized,
                "expires": time.time() + ttl if ttl else None,
            }

    def get(self, key: str, default: Any = None) -> Any:
        if self._redis:
            raw = self._redis.get(self._key(key))
            if raw is None:
                return default
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return raw
        else:
            entry = self._fallback.get(key)
            if entry is None:
                return default
            if entry.get("expires") and time.time() > entry["expires"]:
                del self._fallback[key]
                return default
            try:
                return json.loads(entry["value"])
            except (json.JSONDecodeError, TypeError):
                return entry["value"]

    def delete(self, key: str):
        if self._redis:
            self._redis.delete(self._key(key))
        else:
            self._fallback.pop(key, None)

    def keys(self, pattern: str = "*") -> list[str]:
        if self._redis:
            prefix_len = len(self._prefix)
            return [k[prefix_len:] for k in self._redis.keys(self._key(pattern))]
        else:
            import fnmatch
            now = time.time()
            return [
                k for k, v in self._fallback.items()
                if fnmatch.fnmatch(k, pattern)
                and (not v.get("expires") or v["expires"] > now)
            ]


# ── Pickle Store (for agent.state only) ───────────────────────────────────

class PickleStore:
    """
    Pickle-based persistence for arbitrary Python objects.
    Used ONLY for agent.state dicts (ML models, numpy arrays, cv2 captures).
    """

    def __init__(self, base_dir: str = "./state"):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, agent_name: str) -> Path:
        safe = agent_name.replace("/", "_").replace("\\", "_")
        p = self._base / safe
        p.mkdir(parents=True, exist_ok=True)
        return p / "state.pkl"

    def save(self, agent_name: str, state: dict):
        path = self._path(agent_name)
        try:
            with open(path, "wb") as f:
                pickle.dump(state, f)
        except Exception as e:
            logger.debug(f"[Persistence] Pickle save failed for {agent_name}: {e}")

    def load(self, agent_name: str) -> dict:
        path = self._path(agent_name)
        if path.exists():
            try:
                with open(path, "rb") as f:
                    return pickle.load(f)
            except Exception as e:
                logger.warning(f"[Persistence] Pickle load failed for {agent_name}: {e}")
        return {}

    def delete(self, agent_name: str):
        """
        Remove the agent's state.pkl AND its containing directory.

        Without removing the directory, a subsequent re-spawn of an agent
        with the same name would find an empty folder rather than a truly
        clean slate — harmless but easy to misread when debugging.
        """
        path = self._path(agent_name)
        if path.exists():
            try:
                path.unlink()
            except Exception as e:
                logger.warning(f"[Persistence] Pickle unlink failed for {agent_name}: {e}")
                return
        # Try to drop the parent directory too. rmdir only succeeds if empty,
        # which is what we want — never remove a folder a user populated.
        parent = path.parent
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception as e:
            logger.debug(f"[Persistence] Pickle rmdir skipped for {parent}: {e}")


# ── Unified Persistence API ───────────────────────────────────────────────
# This is the compatibility shim. It replaces the raw pickle persist/recall
# on Actor with intelligent routing based on key names.

# Keys that go to SQLite (durable, structured, queryable)
_SQLITE_KEYS = {
    "_spawned_agents",
    "_pipeline_rules",
    "_user_facts",
    "_notification_urls",
    "_topic_contracts",
    "_agent_manifests",
    "conversation_history",    # must survive restarts — durable
    "history_summary",         # must survive restarts — durable
    "_final_cost",             # lifetime LLM cost — durable, queryable for deleted agents
    "_messages_processed",     # lifetime message count — durable, survives restarts
}

# Keys that go to Redis ONLY when Redis is actually running.
# These are high-frequency, short-lived data. If Redis is down,
# they fall back to in-memory dict (lost on restart, which is fine
# for these specific keys).
_REDIS_KEYS = {
    "_observed_samples",       # rebuilt on first publish anyway
    "_agent_metrics",          # rebuilt from heartbeats
    "_heartbeat_state",        # rebuilt on agent start
}


class PersistenceAPI:
    """
    Drop-in replacement for Actor.persist() / Actor.recall().

    Routes data to the correct store based on key name:
      - Known structured keys → SQLite
      - Known ephemeral keys → Redis
      - Everything else → Pickle (agent.state, models, etc.)

    Usage in Actor.__init__:
        self._persistence = PersistenceAPI(db, redis, pickle_store, self.name)

    Then persist/recall delegate:
        def persist(self, key, value):
            self._persistence.set(key, value)

        def recall(self, key, default=None):
            return self._persistence.get(key, default)
    """

    def __init__(self, db: WactorzDB, redis: RedisStore,
                 pickle_store: PickleStore, agent_name: str):
        self.db = db
        self.redis = redis
        self.pickle = pickle_store
        self.agent = agent_name

    def set(self, key: str, value: Any):
        if key in _SQLITE_KEYS:
            self.db.kv_set(self.agent, key, value)
        elif key in _REDIS_KEYS:
            self.redis.set(f"{self.agent}:{key}", value)
        else:
            # Arbitrary Python object → pickle
            state = self.pickle.load(self.agent)
            state[key] = value
            self.pickle.save(self.agent, state)

    def get(self, key: str, default: Any = None) -> Any:
        if key in _SQLITE_KEYS:
            return self.db.kv_get(self.agent, key, default)
        elif key in _REDIS_KEYS:
            return self.redis.get(f"{self.agent}:{key}", default)
        else:
            state = self.pickle.load(self.agent)
            return state.get(key, default)

    def delete(self, key: str):
        if key in _SQLITE_KEYS:
            self.db.kv_delete(self.agent, key)
        elif key in _REDIS_KEYS:
            self.redis.delete(f"{self.agent}:{key}")
        else:
            state = self.pickle.load(self.agent)
            state.pop(key, None)
            self.pickle.save(self.agent, state)

    def all(self) -> dict:
        """Return all key-value pairs across all stores."""
        result = {}
        result.update(self.db.kv_all(self.agent))
        for key in _REDIS_KEYS:
            val = self.redis.get(f"{self.agent}:{key}")
            if val is not None:
                result[key] = val
        result.update(self.pickle.load(self.agent))
        return result

    def load_snapshot(self, snapshot: dict, *, replace: bool = True) -> dict:
        """
        Bulk-load a state snapshot (the inverse of all()).

        Used at migration time: the source node ships its complete state, and
        the destination calls this once BEFORE the agent's on_start() runs so
        recall() finds the migrated values during initialization.

        When ``replace`` is True (default), every existing value for this
        agent is wiped first — this is the correct semantics for migration,
        where the incoming snapshot must fully replace any stale local state
        from a prior incarnation of the same name. Pass replace=False for
        merge semantics (advanced; rarely the right thing).

        Returns a summary dict for logging.
        """
        if replace:
            self.purge()

        applied = {"sqlite": 0, "redis": 0, "pickle": 0}
        if not isinstance(snapshot, dict):
            return applied

        # Pickle keys are anything not in the SQLite or Redis sets — we group
        # them so one disk write covers all of them instead of N writes.
        pickle_blob: dict = {}
        for key, value in snapshot.items():
            try:
                if key in _SQLITE_KEYS:
                    self.db.kv_set(self.agent, key, value)
                    applied["sqlite"] += 1
                elif key in _REDIS_KEYS:
                    self.redis.set(f"{self.agent}:{key}", value)
                    applied["redis"] += 1
                else:
                    pickle_blob[key] = value
            except Exception as e:
                logger.warning(
                    f"[Persistence] Could not load snapshot key '{key}' for "
                    f"'{self.agent}': {e}"
                )

        if pickle_blob:
            try:
                self.pickle.save(self.agent, pickle_blob)
                applied["pickle"] = len(pickle_blob)
            except Exception as e:
                logger.warning(
                    f"[Persistence] Pickle bulk-load failed for '{self.agent}': {e}"
                )

        logger.info(
            f"[Persistence] Loaded snapshot for '{self.agent}': "
            f"{applied['sqlite']} SQLite keys, {applied['redis']} Redis keys, "
            f"{applied['pickle']} pickle keys"
        )
        return applied

    def purge(self) -> dict:
        """
        Permanently delete EVERY stored value for this agent across all
        backends (SQLite kv_store, Redis ephemeral keys, pickle state file).

        Use this only when the agent is being fully deleted — not on stop or
        restart. Returns a small summary dict, useful for logging:

            {"sqlite_rows": int, "redis_keys": int, "pickle_deleted": bool}
        """
        summary = {"sqlite_rows": 0, "redis_keys": 0, "pickle_deleted": False}

        # 1. SQLite — drop every row this agent owns in kv_store.
        try:
            summary["sqlite_rows"] = self.db.kv_purge_agent(self.agent)
        except Exception as e:
            logger.warning(f"[Persistence] SQLite purge failed for {self.agent}: {e}")

        # 2. Redis — only the known ephemeral keys live there.
        for key in _REDIS_KEYS:
            try:
                self.redis.delete(f"{self.agent}:{key}")
                summary["redis_keys"] += 1
            except Exception as e:
                logger.debug(f"[Persistence] Redis delete {key} failed: {e}")

        # 3. Pickle — remove the agent's state.pkl on disk.
        try:
            self.pickle.delete(self.agent)
            summary["pickle_deleted"] = True
        except Exception as e:
            logger.warning(f"[Persistence] Pickle delete failed for {self.agent}: {e}")

        logger.info(
            f"[Persistence] Purged agent '{self.agent}': "
            f"{summary['sqlite_rows']} SQLite rows, "
            f"{summary['redis_keys']} Redis keys, "
            f"pickle_deleted={summary['pickle_deleted']}"
        )
        return summary


# ── Migration helper ───────────────────────────────────────────────────────

def migrate_from_pickle(state_dir: str, db: WactorzDB, redis: RedisStore):
    """
    One-time migration: read existing .pkl files and write to SQLite/Redis.

    Only migrates keys that do NOT already exist in SQLite/Redis — this makes the
    function safe to call on every startup without overwriting newer SQLite data
    with stale pickle data from a previous session.
    """
    base = Path(state_dir)
    if not base.exists():
        return

    migrated = 0
    for agent_dir in base.iterdir():
        if not agent_dir.is_dir():
            continue
        pkl_path = agent_dir / "state.pkl"
        if not pkl_path.exists():
            continue

        agent_name = agent_dir.name
        try:
            with open(pkl_path, "rb") as f:
                state = pickle.load(f)
        except Exception as e:
            logger.warning(f"[Migration] Failed to read {pkl_path}: {e}")
            continue

        if not isinstance(state, dict):
            continue

        for key, value in state.items():
            if key in _SQLITE_KEYS:
                # Skip if SQLite already has this key — SQLite wins over stale pickle
                if db.kv_get(agent_name, key) is not None:
                    continue
                db.kv_set(agent_name, key, value)
                migrated += 1
            elif key in _REDIS_KEYS:
                redis.set(f"{agent_name}:{key}", value)
                migrated += 1
            # Pickle keys stay in .pkl — no migration needed

    if migrated:
        logger.info(f"[Migration] Migrated {migrated} key(s) from pickle to SQLite/Redis")


# ── Singleton access ──────────────────────────────────────────────────────

_db: Optional[WactorzDB] = None
_redis: Optional[RedisStore] = None
_pickle: Optional[PickleStore] = None


def init_persistence(
    db_path: str = "./state/wactorz.db",
    redis_url: Optional[str] = None,
    state_dir: str = "./state",
    run_migration: bool = True,
) -> tuple[WactorzDB, RedisStore, PickleStore]:
    """
    Initialize the persistence layer. Call once at startup from cli.py.

    Redis URL is resolved in this order:
      1. Explicit redis_url parameter
      2. REDIS_URL environment variable
      3. Default: redis://localhost:6379

    If Redis is not available, falls back to in-memory dict automatically.

    Returns (db, redis, pickle_store) for passing to ActorSystem.
    """
    import os
    global _db, _redis, _pickle

    if redis_url is None:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")

    _db = WactorzDB(db_path)
    _redis = RedisStore(redis_url)
    _pickle = PickleStore(state_dir)

    if run_migration:
        # 1. Migrate legacy pickle data to new stores
        migrate_from_pickle(state_dir, _db, _redis)

        # 2. Run framework migrations (schema upgrades, state upgrades, spawn validation)
        try:
            from .migrations import run_migrations
            migration_result = run_migrations(_db, _redis, _pickle)

            # Log spawn issues as startup warnings
            for issue in migration_result.get("spawn_issues", []):
                if issue["severity"] == "error":
                    logger.warning(
                        f"[Persistence] Spawn registry issue: {issue['agent']} — "
                        f"{issue['message']}"
                    )
        except ImportError:
            logger.debug("[Persistence] migrations module not available — skipping")
        except Exception as e:
            logger.warning(f"[Persistence] Migration runner failed: {e} — continuing anyway")

    return _db, _redis, _pickle


def get_db() -> Optional[WactorzDB]:
    return _db


def get_redis() -> Optional[RedisStore]:
    return _redis


def get_pickle_store() -> Optional[PickleStore]:
    return _pickle