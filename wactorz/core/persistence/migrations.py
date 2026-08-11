"""wactorz.core.persistence.migrations — Schema & State Migration Framework
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Handles version upgrades when users update wactorz (e.g. 0.3 → 0.4).

Three migration layers:
  1. SQLite schema migrations — add columns, tables, indices
  2. Persistent state migrations — upgrade stored data structures
  3. Spawn registry validation — flag/fix stale agent configs

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
import pickle
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Current version ────────────────────────────────────────────────────────
# Increment this when adding new migrations.
# The startup sequence runs all migrations between the stored version and this.
FRAMEWORK_VERSION = 3


# ══════════════════════════════════════════════════════════════════════════════
# 1. SQLITE SCHEMA MIGRATIONS
# ══════════════════════════════════════════════════════════════════════════════
# Each function receives a sqlite3.Connection and runs inside a transaction.
# Only add columns/tables/indices — never drop or rename (breaks rollback).


def migrate_sql_2(conn: sqlite3.Connection):
    """v1 → v2: Add migration tracking table and framework_version to schema_version.
    Also adds any missing columns/tables that may not exist in older databases.
    """
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

    # Ensure spawn_registry has a 'trusted' column
    try:
        conn.execute("SELECT trusted FROM spawn_registry LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE spawn_registry ADD COLUMN trusted INTEGER DEFAULT 0")

    # Ensure spawn_registry has a 'framework_version' column
    # so we know which version the config was saved under
    try:
        conn.execute("SELECT framework_version FROM spawn_registry LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE spawn_registry ADD COLUMN framework_version INTEGER DEFAULT 1")


def migrate_sql_3(conn: sqlite3.Connection):
    """v2 → v3: chat_log carries the attachments a turn was sent with.

    Without it the thread rebuilt after a reload shows the text of a turn and
    not the file it was about.
    """
    try:
        conn.execute("SELECT attachments FROM chat_log LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE chat_log ADD COLUMN attachments TEXT DEFAULT ''")


# Register SQL migrations: version → function
# Each migration upgrades FROM (version-1) TO (version)
_SQL_MIGRATIONS = {
    2: migrate_sql_2,
    3: migrate_sql_3,
    # 3: migrate_sql_3,  ← add future migrations here
}


# ══════════════════════════════════════════════════════════════════════════════
# 2. PERSISTENT STATE MIGRATIONS
# ══════════════════════════════════════════════════════════════════════════════
# Upgrade stored data structures (in SQLite kv_store or pickle).
# Each function receives (db, pickle_store) and handles its own
# error recovery per-agent.


def migrate_state_2(db, pickle_store):
    """v1 → v2: Upgrade persisted state structures.

    - EntityBaseline: add missing fields (is_binary, transition_freq, ready)
    - TopicContract observed_samples: ensure proper dict structure
    - Conversation history: sanitize corrupted entries
    - User facts: no changes needed (plain dict)
    """
    _upgrade_baselines(db, pickle_store)
    _upgrade_conversation_history(db, pickle_store)
    _upgrade_topic_contracts(db)
    _stamp_spawn_registry(db)


def _upgrade_baselines(db, pickle_store):
    """Add missing fields to persisted EntityBaseline dicts."""
    BASELINE_DEFAULTS = {
        "is_binary": False,
        "transition_freq": 0.0,
        "ready": False,
        "hourly_count": [0] * 24,
        "max_rate": 0.0,
        "mean_interval": 0.0,
        "p1": 0.0,
        "p99": 0.0,
    }

    # Check SQLite kv_store
    try:
        rows = db.conn.execute(
            "SELECT agent, key, value FROM kv_store WHERE key = 'baselines'"
        ).fetchall()
        for row in rows:
            agent_name = row[0]
            try:
                baselines = json.loads(row[2])
                if not isinstance(baselines, dict):
                    continue
                upgraded = False
                for _key, baseline in baselines.items():
                    if not isinstance(baseline, dict):
                        continue
                    for field, default in BASELINE_DEFAULTS.items():
                        if field not in baseline:
                            baseline[field] = default
                            upgraded = True
                if upgraded:
                    db.conn.execute(
                        "UPDATE kv_store SET value=?, updated=? WHERE agent=? AND key='baselines'",
                        (json.dumps(baselines), time.time(), agent_name),
                    )
                    logger.info(f"[Migration] Upgraded baselines for '{agent_name}'")
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"[Migration] Could not upgrade baselines for '{agent_name}': {e}")
        db.conn.commit()
    except Exception as e:
        logger.warning(f"[Migration] Baseline upgrade failed: {e}")

    # Also check pickle files
    base = Path(pickle_store._base)
    for agent_dir in base.iterdir():
        if not agent_dir.is_dir():
            continue
        pkl_path = agent_dir / "state.pkl"
        if not pkl_path.exists():
            continue
        try:
            with open(pkl_path, "rb") as f:
                state = pickle.load(f)
            if not isinstance(state, dict):
                continue
            baselines = state.get("baselines")
            if not isinstance(baselines, dict):
                continue
            upgraded = False
            for _key, baseline in baselines.items():
                if not isinstance(baseline, dict):
                    continue
                for field, default in BASELINE_DEFAULTS.items():
                    if field not in baseline:
                        baseline[field] = default
                        upgraded = True
            if upgraded:
                with open(pkl_path, "wb") as f:
                    pickle.dump(state, f)
                logger.info(f"[Migration] Upgraded pickle baselines for '{agent_dir.name}'")
        except Exception as exc:
            # One agent's unreadable pickle must not abort the whole migration —
            # but it is logged rather than dropped. A silent pass here is how a
            # migration appears to succeed while having done nothing.
            logger.warning(f"[Migration] Skipped pickle baselines for '{agent_dir.name}': {exc}")


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
                        f"[Migration] Sanitized conversation history for '{agent_name}': "
                        f"removed {removed} corrupted entries"
                    )
            except (json.JSONDecodeError, TypeError):
                pass
        db.conn.commit()
    except Exception as e:
        logger.warning(f"[Migration] Conversation history upgrade failed: {e}")


def _upgrade_topic_contracts(db):
    """Ensure topic_contracts table entries have all required fields."""
    try:
        rows = db.conn.execute("SELECT name, observed_samples FROM topic_contracts").fetchall()
        for row in rows:
            name = row[0]
            try:
                samples = json.loads(row[1]) if row[1] else {}
                if not isinstance(samples, dict):
                    db.conn.execute(
                        "UPDATE topic_contracts SET observed_samples='{}', updated=? WHERE name=?",
                        (time.time(), name),
                    )
            except (json.JSONDecodeError, TypeError):
                db.conn.execute(
                    "UPDATE topic_contracts SET observed_samples='{}', updated=? WHERE name=?",
                    (time.time(), name),
                )
        db.conn.commit()
    except sqlite3.OperationalError:
        pass  # table doesn't exist yet — will be created by schema init


def _stamp_spawn_registry(db):
    """Mark all existing spawn registry entries with the current framework version.
    This lets us detect stale configs on future upgrades.
    """
    try:
        db.conn.execute(
            "UPDATE spawn_registry SET framework_version=? WHERE framework_version IS NULL OR framework_version < ?",
            (FRAMEWORK_VERSION, FRAMEWORK_VERSION),
        )
        db.conn.commit()
    except sqlite3.OperationalError:
        pass  # column doesn't exist yet — migrate_sql_2 will add it


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
# 3. SPAWN REGISTRY VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

# API changes between versions — methods that were renamed, removed, or
# had their signatures changed. Used to detect stale agent code.
_API_CHANGES = {
    2: {
        "removed_methods": [],
        "renamed_methods": {},  # old_name → new_name
        "new_methods": ["query_ts", "query_detections", "query_ha_states", "ts_stats"],
        "signature_changes": {},
        "description": "Added time-series query API, persistence layer, trusted flag",
    },
    # 3: {
    #     "removed_methods": ["old_method"],
    #     "renamed_methods": {"old_name": "new_name"},
    #     "new_methods": ["new_method"],
    #     "signature_changes": {"method": "new signature info"},
    #     "description": "...",
    # },
}


def validate_spawn_registry(db) -> list[dict]:
    """Check all spawn registry entries for compatibility with the current version.

    Returns a list of issues found:
      [{"agent": "name", "severity": "warning|error", "message": "...", "action": "..."}]

    Severity levels:
      - "warning": agent will probably work but may not use new features
      - "error": agent code references removed/renamed methods — will crash
    """
    issues = []

    try:
        rows = db.conn.execute(
            "SELECT name, config, framework_version FROM spawn_registry"
        ).fetchall()
    except sqlite3.OperationalError:
        return issues  # table doesn't exist or missing column

    for row in rows:
        name = row[0]
        try:
            config = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            issues.append(
                {
                    "agent": name,
                    "severity": "error",
                    "message": "Spawn config is corrupted (invalid JSON)",
                    "action": "delete_and_respawn",
                }
            )
            continue

        saved_version = row[2] if len(row) > 2 else 1
        if saved_version is None:
            saved_version = 1

        agent_type = config.get("type", "dynamic")
        code = config.get("code", "")

        # Skip non-dynamic agents (llm, ha_actuator, manual — no code to check)
        if agent_type != "dynamic" or not code:
            continue

        # Trusted (catalog) agents are maintained by the developer — skip
        if config.get("trusted"):
            continue

        # Check for API incompatibilities across version gap
        for version in range(saved_version + 1, FRAMEWORK_VERSION + 1):
            changes = _API_CHANGES.get(version, {})

            # Check for removed methods in agent code
            for method in changes.get("removed_methods", []):
                if f"agent.{method}(" in code or f"agent.{method} " in code:
                    issues.append(
                        {
                            "agent": name,
                            "severity": "error",
                            "message": (
                                f"Code uses agent.{method}() which was removed in v{version}. "
                                f"This agent will crash on startup."
                            ),
                            "action": "needs_respawn",
                            "version_gap": f"v{saved_version} → v{FRAMEWORK_VERSION}",
                        }
                    )

            # Check for renamed methods
            for old_name, new_name in changes.get("renamed_methods", {}).items():
                if f"agent.{old_name}(" in code:
                    issues.append(
                        {
                            "agent": name,
                            "severity": "error",
                            "message": (
                                f"Code uses agent.{old_name}() which was renamed to "
                                f"agent.{new_name}() in v{version}."
                            ),
                            "action": "needs_respawn",
                            "version_gap": f"v{saved_version} → v{FRAMEWORK_VERSION}",
                        }
                    )

        # Version gap warning (even if no specific issues found)
        if saved_version < FRAMEWORK_VERSION:
            issues.append(
                {
                    "agent": name,
                    "severity": "warning",
                    "message": (
                        f"Agent was spawned under framework v{saved_version} "
                        f"(current: v{FRAMEWORK_VERSION}). Code may not use newer API features."
                    ),
                    "action": "consider_respawn",
                    "version_gap": f"v{saved_version} → v{FRAMEWORK_VERSION}",
                }
            )

    return issues


def auto_fix_spawn_registry(db) -> list[str]:
    """Attempt to auto-fix known issues in spawn registry configs.
    Returns list of fixes applied.

    Currently handles:
      - Renamed methods: search-and-replace in code strings
      - Missing 'trusted' field: defaults to False
      - Missing 'framework_version': stamps current version
    """
    fixes = []

    try:
        rows = db.conn.execute(
            "SELECT name, config, framework_version FROM spawn_registry"
        ).fetchall()
    except sqlite3.OperationalError:
        return fixes

    for row in rows:
        name = row[0]
        try:
            config = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            continue

        saved_version = row[2] if len(row) > 2 else 1
        if saved_version is None:
            saved_version = 1

        code = config.get("code", "")
        if not code or config.get("type") != "dynamic":
            continue
        if config.get("trusted"):
            continue

        original_code = code
        applied = []

        # Apply renames across version gap
        for version in range(saved_version + 1, FRAMEWORK_VERSION + 1):
            changes = _API_CHANGES.get(version, {})
            for old_name, new_name in changes.get("renamed_methods", {}).items():
                if f"agent.{old_name}(" in code:
                    code = code.replace(f"agent.{old_name}(", f"agent.{new_name}(")
                    applied.append(f"renamed agent.{old_name} → agent.{new_name}")

        if code != original_code:
            config["code"] = code
            config["framework_version"] = FRAMEWORK_VERSION
            db.conn.execute(
                "UPDATE spawn_registry SET config=?, framework_version=?, updated_at=? WHERE name=?",
                (json.dumps(config), FRAMEWORK_VERSION, time.time(), name),
            )
            fixes.append(f"'{name}': {', '.join(applied)}")

    if fixes:
        db.conn.commit()

    return fixes


# ══════════════════════════════════════════════════════════════════════════════
# MIGRATION RUNNER
# ══════════════════════════════════════════════════════════════════════════════


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
        "spawn_fixes": [...],
        "spawn_issues": [...],
        "errors": [...],
      }
    """
    current = get_current_version(db)
    result = {
        "from_version": current,
        "to_version": FRAMEWORK_VERSION,
        "sql_migrations": 0,
        "state_migrations": 0,
        "spawn_fixes": [],
        "spawn_issues": [],
        "errors": [],
    }

    pending_state = _pending_state_versions(db)

    if current >= FRAMEWORK_VERSION and not pending_state:
        logger.info(f"[Migration] Framework v{FRAMEWORK_VERSION} — no migrations needed")
        # Still validate spawn registry even if no version change
        result["spawn_issues"] = validate_spawn_registry(db)
        return result

    if current < FRAMEWORK_VERSION:
        logger.info(
            f"[Migration] Upgrading framework v{current} → v{FRAMEWORK_VERSION} "
            f"({FRAMEWORK_VERSION - current} migration(s) to apply)"
        )
    else:
        logger.info(
            f"[Migration] Framework v{current} — retrying state migration(s) {pending_state}"
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
            logger.info(f"[Migration] SQL v{version} applied ({(time.time() - t0) * 1000:.0f}ms)")

        except Exception as e:
            error_msg = f"SQL migration v{version} failed: {e}"
            logger.error(f"[Migration] {error_msg}")
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
            logger.info(f"[Migration] State v{version} applied ({(time.time() - t0) * 1000:.0f}ms)")

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
            logger.warning(f"[Migration] {error_msg} — continuing anyway")
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

    # ── Spawn registry validation & auto-fix ───────────────────────────────
    try:
        fixes = auto_fix_spawn_registry(db)
        result["spawn_fixes"] = fixes
        if fixes:
            logger.info(f"[Migration] Auto-fixed {len(fixes)} spawn config(s): {fixes}")
    except Exception as e:
        logger.warning(f"[Migration] Spawn auto-fix failed: {e}")

    issues = validate_spawn_registry(db)
    result["spawn_issues"] = issues

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    if errors:
        logger.warning(
            f"[Migration] {len(errors)} spawn registry error(s) — "
            f"these agents may crash: {[i['agent'] for i in errors]}"
        )
    if warnings:
        logger.info(
            f"[Migration] {len(warnings)} spawn registry warning(s) — "
            f"consider respawning: {[i['agent'] for i in warnings]}"
        )

    logger.info(
        f"[Migration] Complete: v{current} → v{FRAMEWORK_VERSION} | "
        f"SQL={result['sql_migrations']} State={result['state_migrations']} "
        f"Fixes={len(result['spawn_fixes'])} Issues={len(issues)} "
        f"Errors={len(result['errors'])}"
    )

    return result
