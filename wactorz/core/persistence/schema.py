"""SQLite schema and its version.

Kept apart from the connection manager so a schema change reads as a schema
change: the DDL is long, and burying it in the same file as the query methods
made both harder to review.
"""

# ── SQLite Schema ──────────────────────────────────────────────────────────

SCHEMA_VERSION = 1

# Column DEFAULTs below use ((julianday('now') - 2440587.5) * 86400.0) for a
# sub-second Unix timestamp. We deliberately avoid unixepoch('subsec'), which
# requires SQLite >= 3.42 (2023): SQLite resolves a DEFAULT expression's
# functions when it compiles ANY write to the table (even when the column value
# is supplied), so an unavailable function breaks every INSERT — not just
# default-relying ones. julianday() is core since SQLite 3.0 (2004), so this
# works on every platform/version we could run on.
SCHEMA_SQL = """
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
    session_id TEXT    DEFAULT '',           -- optional grouping (actor_id or custom)
    attachments TEXT   DEFAULT ''            -- JSON array of {id,name,mime,size}
);

CREATE INDEX IF NOT EXISTS idx_chatlog_ts          ON chat_log (ts);
CREATE INDEX IF NOT EXISTS idx_chatlog_agent_ts    ON chat_log (agent_name, ts);
"""
