"""wactorz.core.persistence.migrations — Schema & State Migration Framework
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Handles version upgrades when users update wactorz (e.g. 0.3 → 0.4).

Two migration layers:
  1. SQLite schema migrations — add columns, tables, indices
  2. Persistent state migrations — upgrade stored data structures

Called automatically at startup from init_persistence(). Safe to run
multiple times — each migration checks preconditions before applying.

ADDING A NEW MIGRATION
──────────────────────
1. Increment FRAMEWORK_VERSION at the top of this file
2. Add a function: def migrate_sql_N(conn): ...
   and/or:         def migrate_state_N(db, pickle_store): ...
3. Register it in _SQL_MIGRATIONS and/or _STATE_MIGRATIONS dicts
4. That's it — run_migrations() picks it up automatically

ROLLBACK
────────
SQLite migrations wrap each step in a transaction. If a migration fails,
the transaction is rolled back and the version stays at the previous value.
The system continues running on the old schema — nothing is corrupted.

State migrations are best-effort: if upgrading a single agent's state fails,
it's logged and skipped. Other agents are not affected.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Current version ────────────────────────────────────────────────────────
# Increment this when adding new migrations.
# The startup sequence runs all migrations between the stored version and this.
FRAMEWORK_VERSION = 4


# ══════════════════════════════════════════════════════════════════════════════
# 1. SQLITE SCHEMA MIGRATIONS
# ══════════════════════════════════════════════════════════════════════════════
# Each function receives a sqlite3.Connection and runs inside a transaction.
# Only add columns/tables/indices — never drop or rename (breaks rollback).


def migrate_sql_2(conn: sqlite3.Connection):
    """v1 → v2: Add migration tracking table and framework_version to schema_version."""
    # Add migration history table
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS migration_history (
            version     INTEGER NOT NULL,
            applied_at  REAL NOT NULL,
            description TEXT DEFAULT '',
            duration_ms INTEGER DEFAULT 0
        );
    """)

    # Ensure schema_version has a framework_version column
    # (older databases only have 'version' for the SQL schema)
    try:
        conn.execute("SELECT framework_version FROM schema_version LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE schema_version ADD COLUMN framework_version INTEGER DEFAULT 1")


def migrate_sql_3(conn: sqlite3.Connection):
    """v2 → v3: chat_log carries the attachments a turn was sent with.

    Without it the thread rebuilt after a reload shows the text of a turn and
    not the file it was about.
    """
    try:
        conn.execute("SELECT attachments FROM chat_log LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE chat_log ADD COLUMN attachments TEXT DEFAULT ''")


#: The columns node samples gained once nodes reported how close they are to
#: running out, with their types.
NODE_HISTORY_READINGS = (
    ("swap_used_mb", "REAL"),
    ("load_1m", "REAL"),
    ("disk_free_mb", "REAL"),
    ("temp_c", "REAL"),
    ("throttled", "TEXT"),
)


def migrate_sql_4(conn: sqlite3.Connection):
    """v3 → v4: node samples keep swap, load, free disk, temperature and throttling.

    A database whose ``node_metrics_history`` has none of them would refuse
    every sample, and the agents' samples are written in the same transaction.
    """
    present = {row[1] for row in conn.execute("PRAGMA table_info(node_metrics_history)")}
    for column, kind in NODE_HISTORY_READINGS:
        if column not in present:
            conn.execute(f"ALTER TABLE node_metrics_history ADD COLUMN {column} {kind}")


# Register SQL migrations: version → function
# Each migration upgrades FROM (version-1) TO (version)
_SQL_MIGRATIONS = {
    2: migrate_sql_2,
    3: migrate_sql_3,
    4: migrate_sql_4,
    # 5: migrate_sql_5,  ← add future migrations here
}


# ══════════════════════════════════════════════════════════════════════════════
# 2. PERSISTENT STATE MIGRATIONS
# ══════════════════════════════════════════════════════════════════════════════
# Upgrade stored data structures (in SQLite kv_store or pickle).
# Each function receives (db, pickle_store) and handles its own
# error recovery per-agent.


def migrate_state_2(db, pickle_store):
    """v1 → v2: Upgrade persisted state structures.

    - Conversation history: sanitize corrupted entries
    - User facts: no changes needed (plain dict)

    An agent's own data is not upgraded here: an agent that changes the shape
    of what it stores declares a state version and upgrades it itself, when it
    starts (see `wactorz.core.state_versions`).
    """
    _upgrade_conversation_history(db, pickle_store)


def _upgrade_conversation_history(db, pickle_store):
    """Sanitize corrupted conversation history entries."""
    try:
        rows = db.conn.execute(
            "SELECT agent, value FROM kv_store WHERE key = 'conversation_history'"
        ).fetchall()
        for row in rows:
            agent_name = row[0]
            try:
                history = json.loads(row[1])
                if not isinstance(history, list):
                    continue
                clean = []
                for m in history:
                    if not isinstance(m, dict):
                        continue
                    role = m.get("role", "")
                    content = m.get("content", "")
                    if role not in ("user", "assistant"):
                        continue
                    if not isinstance(content, str):
                        content = str(content)
                    if content.strip():
                        entry: dict[str, Any] = {"role": role, "content": content}
                        if "ts" in m and isinstance(m["ts"], (int, float)):
                            entry["ts"] = m["ts"]
                        clean.append(entry)
                if len(clean) != len(history):
                    db.conn.execute(
                        "UPDATE kv_store SET value=?, updated=? WHERE agent=? AND key='conversation_history'",
                        (json.dumps(clean), time.time(), agent_name),
                    )
                    removed = len(history) - len(clean)
                    logger.info(
                        "[Migration] Sanitized conversation history for '%s': removed %s corrupted entries",
                        agent_name,
                        removed,
                    )
            except (json.JSONDecodeError, TypeError):
                pass
        db.conn.commit()
    except Exception as e:
        logger.warning("[Migration] Conversation history upgrade failed: %s", e)


# Register state migrations
_STATE_MIGRATIONS = {
    2: migrate_state_2,
    # 3: migrate_state_3,
}

# migration_history rows are only ever written on success, which makes the table
# the record of what is actually done. These prefixes are how a row's kind is
# recognised when reading it back, so they are written in exactly one place.
_SQL_HISTORY_PREFIX = "SQL schema migration v"
_STATE_HISTORY_PREFIX = "State data migration v"


# ══════════════════════════════════════════════════════════════════════════════
# MIGRATION RUNNER
# ══════════════════════════════════════════════════════════════════════════════


def stamp_new_database(conn: sqlite3.Connection) -> None:
    """Record a database made from today's schema as already current.

    The schema creates everything the SQL migrations add, and a new database
    holds no data a state migration could change, so there is nothing to apply.
    Without this it read as the first release and was walked through every
    migration on empty tables, logging an upgrade that never was.
    """
    conn.execute(
        "INSERT INTO schema_version (version, framework_version) VALUES (1, ?)",
        (FRAMEWORK_VERSION,),
    )
    now = time.time()
    conn.executemany(
        "INSERT INTO migration_history (version, applied_at, description) VALUES (?, ?, ?)",
        [
            (version, now, f"{_STATE_HISTORY_PREFIX}{version} (new database: nothing to migrate)")
            for version in sorted(_STATE_MIGRATIONS)
        ],
    )


def get_current_version(db) -> int:
    """Get the current framework version from the database."""
    try:
        row = db.conn.execute("SELECT framework_version FROM schema_version LIMIT 1").fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except sqlite3.OperationalError:
        pass

    # Fallback: check if schema_version table exists with just 'version'
    try:
        row = db.conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row:
            return 1  # original schema, no framework_version column yet
    except sqlite3.OperationalError:
        pass

    return 0  # fresh database


def _pending_state_versions(db) -> list[int]:
    """Registered state migrations with no success recorded in the history table.

    State work is tracked here rather than derived from ``framework_version``
    because the two can legitimately disagree. A SQL migration is not idempotent,
    so its version must be stamped once applied; its paired state migration may
    still be owed. One number meaning both is what let a failed state migration
    be stamped as done and never retried.
    """
    done: set[int] = set()
    try:
        rows = db.conn.execute(
            "SELECT version FROM migration_history WHERE description LIKE ?",
            (f"{_STATE_HISTORY_PREFIX}%",),
        ).fetchall()
        done = {int(row[0]) for row in rows}
    except sqlite3.OperationalError:
        # No history table yet (a v1 database) — nothing can have been recorded.
        pass
    return sorted(v for v in _STATE_MIGRATIONS if v <= FRAMEWORK_VERSION and v not in done)


def run_migrations(db, pickle_store=None) -> dict:
    """Run all pending migrations from current version to FRAMEWORK_VERSION.

    Called automatically by init_persistence(). Safe to call multiple times.

    Returns:
      {
        "from_version": int,
        "to_version": int,
        "sql_migrations": int,
        "state_migrations": int,
        "errors": [...],
      }
    """
    current = get_current_version(db)
    result = {
        "from_version": current,
        "to_version": FRAMEWORK_VERSION,
        "sql_migrations": 0,
        "state_migrations": 0,
        "errors": [],
    }

    pending_state = _pending_state_versions(db)

    if current >= FRAMEWORK_VERSION and not pending_state:
        logger.info("[Migration] Framework v%s — no migrations needed", FRAMEWORK_VERSION)
        return result

    if current < FRAMEWORK_VERSION:
        logger.info(
            "[Migration] Upgrading framework v%s → v%s (%s migration(s) to apply)",
            current,
            FRAMEWORK_VERSION,
            FRAMEWORK_VERSION - current,
        )
    else:
        logger.info(
            "[Migration] Framework v%s — retrying state migration(s) %s", current, pending_state
        )

    # ── SQL schema migrations ──────────────────────────────────────────────
    schema_ok_through = current
    for version in range(current + 1, FRAMEWORK_VERSION + 1):
        migrate_fn = _SQL_MIGRATIONS.get(version)
        if not migrate_fn:
            schema_ok_through = version  # nothing owed at this version
            continue

        t0 = time.time()
        try:
            # Run in a transaction so failures roll back cleanly
            with db.conn:
                migrate_fn(db.conn)
                # Record in migration history
                try:
                    duration_ms = int((time.time() - t0) * 1000)
                    db.conn.execute(
                        "INSERT INTO migration_history (version, applied_at, description, duration_ms) "
                        "VALUES (?, ?, ?, ?)",
                        (version, time.time(), f"{_SQL_HISTORY_PREFIX}{version}", duration_ms),
                    )
                except sqlite3.OperationalError:
                    pass  # migration_history table might not exist yet (v1→v2)

            result["sql_migrations"] += 1
            schema_ok_through = version
            logger.info("[Migration] SQL v%s applied (%.0fms)", version, (time.time() - t0) * 1000)

        except Exception as e:
            error_msg = f"SQL migration v{version} failed: {e}"
            logger.exception("[Migration] %s", error_msg)
            result["errors"].append(error_msg)
            # Stop — don't apply later migrations if an earlier one failed
            break

    # ── State data migrations ──────────────────────────────────────────────
    for version in pending_state:
        migrate_fn = _STATE_MIGRATIONS[version]

        t0 = time.time()
        try:
            migrate_fn(db, pickle_store)
            result["state_migrations"] += 1
            logger.info(
                "[Migration] State v%s applied (%.0fms)", version, (time.time() - t0) * 1000
            )

            try:
                duration_ms = int((time.time() - t0) * 1000)
                db.conn.execute(
                    "INSERT INTO migration_history (version, applied_at, description, duration_ms) "
                    "VALUES (?, ?, ?, ?)",
                    (version, time.time(), f"{_STATE_HISTORY_PREFIX}{version}", duration_ms),
                )
                db.conn.commit()
            except sqlite3.OperationalError:
                pass

        except Exception as e:
            error_msg = f"State migration v{version} failed: {e}"
            logger.warning("[Migration] %s — continuing anyway", error_msg)
            result["errors"].append(error_msg)
            # State migrations are best-effort — continue with next version

    # ── Update stored version ──────────────────────────────────────────────
    # The highest version the schema actually reached — not FRAMEWORK_VERSION,
    # which would skip a SQL migration that failed part-way through the run. A
    # state migration that failed no longer holds this back: it is retried from
    # the history table instead.
    if schema_ok_through > current:
        try:
            db.conn.execute(
                "UPDATE schema_version SET framework_version=?",
                (schema_ok_through,),
            )
            db.conn.commit()
        except sqlite3.OperationalError:
            # framework_version column might not exist if SQL migration failed
            pass

    logger.info(
        "[Migration] Complete: v%s → v%s | SQL=%s State=%s Errors=%s",
        current,
        FRAMEWORK_VERSION,
        result["sql_migrations"],
        result["state_migrations"],
        len(result["errors"]),
    )

    return result
