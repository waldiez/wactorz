"""The store instances this process uses, and the accessors that reach them.

Kept apart from the store classes themselves so those stay plain objects that a
caller can instantiate freely — a test builds its own ``WactorzDB`` without
touching anything shared. Only what is installed here is process-wide.

This module depends on the three store modules and nothing else in the package,
so it can be imported from anywhere in it without a cycle.
"""

import logging

from .db import WactorzDB
from .memory_store import MemoryStore
from .pickle_store import PickleStore

logger = logging.getLogger(__name__)


class Stores:
    """The installed stores. Attributes are set once at startup and read after.

    ``memory`` exists from the start because it needs no configuration; the
    other two are ``None`` until :func:`install_stores` runs, and every accessor
    returns ``None`` rather than raising so a caller running outside a started
    application degrades instead of failing.
    """

    db: WactorzDB | None = None
    pickle: PickleStore | None = None
    memory: MemoryStore = MemoryStore()


def install_stores(db: WactorzDB, pickle_store: PickleStore) -> None:
    """Make these the stores the rest of the process uses.

    Closes the database being replaced. Nothing else holds a reference to it
    once this returns, so leaving it open strands the handle and its
    write-ahead log until the garbage collector reaches it — and it sits in a
    reference cycle, so that is a collection pass rather than the moment the
    last name goes away.

    Re-installing the same database is not a replacement and leaves it open.
    """
    if Stores.db is not None and Stores.db is not db:
        Stores.db.close()
    Stores.db = db
    Stores.pickle = pickle_store


def close_stores() -> None:
    """Close what holds an OS resource and forget the rest. Idempotent.

    The in-memory store is emptied rather than dropped: it is valid at any time,
    and everything in it is regenerated in use.
    """
    if Stores.db is not None:
        Stores.db.close()
    Stores.db = None
    Stores.pickle = None
    Stores.memory.clear()


def get_db() -> WactorzDB | None:
    """The database in use, or ``None`` before startup has installed one."""
    return Stores.db


def get_pickle_store() -> PickleStore | None:
    """The pickle store in use, or ``None`` before startup has installed one."""
    return Stores.pickle


def get_memory_store() -> MemoryStore:
    """The shared ephemeral store. Always available."""
    return Stores.memory
