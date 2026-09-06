"""Process-wide store instances, created once at startup."""

import logging
import os

from ..paths import resolve_state_dir
from .db import WactorzDB
from .legacy_pickle import migrate_from_pickle
from .migrations import run_migrations
from .pickle_store import PickleStore
from .stores import close_stores, install_stores

logger = logging.getLogger(__name__)


def init_persistence(
    db_path: str | None = None,
    state_dir: str | None = None,
    run_migration: bool = True,
) -> tuple[WactorzDB, PickleStore]:
    """Initialize the persistence layer. Call once at startup from cli.py.

    Returns (db, pickle_store) for passing to ActorSystem.
    """
    _state_dir = resolve_state_dir(state_dir)
    _db_path = db_path or os.path.join(_state_dir, "wactorz.db")
    wactorz_db = WactorzDB(_db_path)
    pickle_store = PickleStore(_state_dir)
    install_stores(wactorz_db, pickle_store)

    if run_migration:
        # 1. Migrate legacy pickle data to new stores
        migrate_from_pickle(_state_dir, wactorz_db)

        # 2. Run framework migrations (schema upgrades, state upgrades, spawn validation)
        try:
            migration_result = run_migrations(wactorz_db, pickle_store)

            # Log spawn issues as startup warnings
            for issue in migration_result.get("spawn_issues", []):
                if issue["severity"] == "error":
                    logger.warning(
                        "[Persistence] Spawn registry issue: %s — %s",
                        issue["agent"],
                        issue["message"],
                    )
        except Exception as e:
            # A failing migration must not stop startup — but a *missing* one is a
            # wiring bug, so ImportError is deliberately not caught here: swallowing
            # it is what let the wrong-module import go unnoticed.
            logger.warning("[Persistence] Migration runner failed: %s — continuing anyway", e)

    return wactorz_db, pickle_store


def close_persistence() -> None:
    """Close the stores and forget them. Idempotent.

    Only the database holds an OS resource; the others are cleared so a later
    ``init_persistence`` starts from a known state rather than half the previous
    one.
    """
    close_stores()
