"""wactorz.core.persistence — the storage layer behind persist()/recall().

Three stores, chosen per key by :class:`PersistenceAPI`:

  - **SQLite** — durable structured data: spawn registry, rules, facts,
    contracts, chat log, time-series.
  - **Process memory** — high-frequency values that normal operation
    regenerates: observed samples, agent metrics, heartbeat state.
  - **Pickle** — arbitrary Python objects belonging to one agent:
    ``agent.state``, ML models, capture handles.

Agent code addresses keys, not stores; the routing tables are ``SQLITE_KEYS``
and ``EPHEMERAL_KEYS`` in :mod:`.api`. Real-time messaging is MQTT's job, not
this layer's.

Split by store, so a change to one does not mean reading the others. Everything
public is re-exported here, so ``from wactorz.core.persistence import WactorzDB``
resolves without knowing the internal layout.
"""

from .api import EPHEMERAL_KEYS, SQLITE_KEYS, PersistenceAPI
from .db import WactorzDB
from .legacy_pickle import migrate_from_pickle
from .lifecycle import close_persistence, init_persistence
from .memory_store import MemoryStore
from .migrations import run_migrations
from .pickle_store import PickleStore
from .schema import SCHEMA_SQL, SCHEMA_VERSION
from .stores import Stores, get_db, get_memory_store, get_pickle_store

__all__ = [
    "EPHEMERAL_KEYS",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "SQLITE_KEYS",
    "MemoryStore",
    "PersistenceAPI",
    "PickleStore",
    "Stores",
    "WactorzDB",
    "close_persistence",
    "get_db",
    "get_memory_store",
    "get_pickle_store",
    "init_persistence",
    "migrate_from_pickle",
    "run_migrations",
]
