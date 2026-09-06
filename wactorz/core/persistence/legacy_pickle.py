"""One-time migration of pre-SQLite pickle state."""

import logging
import pickle
from pathlib import Path

from .api import EPHEMERAL_KEYS, SQLITE_KEYS
from .db import WactorzDB
from .stores import get_memory_store

logger = logging.getLogger(__name__)

# ── Migration helper ───────────────────────────────────────────────────────


def migrate_from_pickle(state_dir: str, db: WactorzDB) -> None:
    """One-time migration: read existing .pkl files and write to SQLite or process memory.

    Only migrates keys that do NOT already exist in either — this makes the
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
            logger.warning("[Migration] Failed to read %s: %s", pkl_path, e)
            continue

        if not isinstance(state, dict):
            continue

        for key, value in state.items():
            if key in SQLITE_KEYS:
                # Skip if SQLite already has this key — SQLite wins over stale pickle
                if db.kv_get(agent_name, key) is not None:
                    continue
                db.kv_set(agent_name, key, value)
                migrated += 1
            elif key in EPHEMERAL_KEYS:
                get_memory_store().set(f"{agent_name}:{key}", value)
                migrated += 1
            # Pickle keys stay in .pkl — no migration needed

    if migrated:
        logger.info("[Migration] Migrated %s key(s) from pickle", migrated)
