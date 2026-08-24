"""Shared mutable state for the monitor server.

Single home for the state every monitor module needs: the actor registry and
persistence handles injected at boot, the live agent/node snapshot, connected
websocket clients, and the deleted-agent tombstones.

Access rule: ``from . import runtime`` then ``runtime.registry`` **at use
time**. ``from .runtime import registry`` binds a snapshot — a later injection
would not be seen. The in-place-mutated containers (``state``, ``ws_clients``)
are the only names safe to alias-import.
"""

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiomqtt

    from wactorz.core.persistence import WactorzDB
    from wactorz.core.registry import ActorRegistry

# ── Injected config (app.py overwrites these at boot from CLI/env) ───────────
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
WS_PORT = 8888
MQTT_TOPICS = [
    "agents/#",
    "system/#",
    "nodes/#",
    "homeassistant/state_changes/#",
]

IO_GATEWAY_ID = "io-gateway"

# ── Injected handles ─────────────────────────────────────────────────────────
# None  → no registry wired; the dashboard renders MQTT-reported state only
# <registry> → direct mode (Option B)
registry: "ActorRegistry | None" = None

# Used to query historical cost data for deleted agents.
db: "WactorzDB | None" = None

mqtt_client_ref: "aiomqtt.Client | None" = None

# Server↔broker link state. Shared: mqtt sets it, ws reports it to browsers, so
# it lives here rather than in either module (mqtt already depends on ws).
mqtt_connected: bool = False

# ── Live snapshot (mutated in place — never rebound) ─────────────────────────
state = {
    "agents": {},
    "nodes": {},
    "alerts": [],
    "system_health": {},
    "log_feed": [],
}

ws_clients: set = set()

# ── Deleted-agent tombstones ─────────────────────────────────────────────────
# IDs that have been explicitly deleted — block re-admission from stale heartbeats.
# Bounded so a long-running monitor doesn't leak memory across many deletions.
# Stored as a list of (agent_id, deleted_at_ts) tuples so we can re-admit on
# a NEWER status event (which is what a deliberate respawn produces) while
# still ignoring stale retained messages from the deleted instance.
deleted_agent_ids: list = []
DELETED_IDS_MAX = 1024
hard_resetting: bool = False


def mark_deleted(agent_id: str) -> None:
    """Add an agent_id to the deleted list with FIFO eviction. If already
    present, refresh its deleted-at timestamp so any in-flight retained
    messages from the previous incarnation stay blocked.
    """
    undelete(agent_id)  # remove any prior entry so the new timestamp wins
    deleted_agent_ids.append((agent_id, time.time()))
    if len(deleted_agent_ids) > DELETED_IDS_MAX:
        del deleted_agent_ids[0 : len(deleted_agent_ids) - DELETED_IDS_MAX]


def is_deleted(agent_id: str, newer_than: float = 0.0) -> bool:
    """Was this agent_id deleted? When newer_than is given, return False if
    the caller has evidence (a message timestamp) that's strictly later than
    the deletion — that means the agent was respawned and we should re-admit
    it on the next update_agent() call. The actual un-delete happens there;
    this function stays a pure query.
    """
    for aid, ts in deleted_agent_ids:
        if aid == agent_id:
            return not newer_than > ts
    return False


def undelete(agent_id: str) -> bool:
    """Remove agent_id from the deleted list. Returns True if it was there."""
    global deleted_agent_ids
    before = len(deleted_agent_ids)
    deleted_agent_ids = [(a, t) for (a, t) in deleted_agent_ids if a != agent_id]
    return len(deleted_agent_ids) < before


# ── Injection points (app.py calls these at boot) ────────────────────────────


def set_registry(value) -> None:
    """Inject the actor registry (direct mode)."""
    global registry
    registry = value


def set_db(value) -> None:
    """Inject the persistence DB handle."""
    global db
    db = value
