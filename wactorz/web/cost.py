"""Cost + message-count accounting for the monitor.

Owns the in-memory lifetime ledger (hydrated once from SQLite) and the
"best available number" resolution used by the dashboard: live MQTT state
first, then the actor's own metrics, then the persisted final value.
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..agents.llm.cost import get_global_alltime_cost
from . import db_writer, runtime

logger = logging.getLogger(__name__)

# ── Durable lifetime cost ledger ────────────────────────────────────────────
# The headline total cost must never drop when an agent goes away. The per-agent
# _final_cost rows that feed _historical_cost_usd() are purged on permanent
# delete (kv_purge_agent) and are never written on a hard kill — an addon update
# restarts the container, so an in-flight agent's on_stop() never runs. A total
# computed purely from live actors + surviving _final_cost rows therefore leaks
# spend whenever an agent is deleted or killed mid-life.
#
# Every actor publishes its cumulative cost_usd over MQTT on each heartbeat
# (~10s), so the monitor sees the spend long before the agent disappears. We bank
# each actor's highest-reported cost into a monotonic ledger keyed by the stable
# actor_id and persist it under the _system agent in SQLite. Deletion and hard
# kills cannot erase what was already banked; keying by actor_id stops a
# respawned agent from double-counting.
LIFETIME_LEDGER_KEY = "_lifetime_cost_ledger"
lifetime_cost: dict = {}
lifetime_loaded: bool = False


def _read_ledger() -> dict[str, float]:
    """The ledger as SQLite keeps it. Run on a thread at startup."""
    if runtime.db is None:
        return {}
    try:
        stored = runtime.db.kv_get("_system", LIFETIME_LEDGER_KEY)
    except Exception as exc:
        logger.debug("[cost] the lifetime ledger could not be read: %s", exc)
        return {}
    found: dict[str, float] = {}
    for k, v in (stored if isinstance(stored, dict) else {}).items():
        try:
            found[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return found


def _take_ledger(stored: Mapping[str, float]) -> None:
    global lifetime_loaded
    for k, v in stored.items():
        lifetime_cost[k] = max(v, lifetime_cost.get(k, 0.0))
    lifetime_loaded = True


async def load_lifetime() -> None:
    """Read the ledger once, on a thread, as the server starts: before the first metrics message."""
    if lifetime_loaded or runtime.db is None:
        return
    _take_ledger(await asyncio.to_thread(_read_ledger))


def ensure_lifetime_loaded() -> None:
    """Read the ledger on the spot, for a process that did not start the server.

    The server reads it at start (`load_lifetime`), so there this does nothing;
    an embedding that never did gets the read here, the once.
    """
    if lifetime_loaded or runtime.db is None:
        return
    _take_ledger(_read_ledger())


def record_lifetime_cost(agent_id: str, cost_usd) -> None:
    """Bank an agent's reported lifetime cost as a monotonic high-water mark.
    Persisted durably so deletion / hard kills never drop it from the total.

    The high-water mark is deliberately robust to out-of-order / transient-low
    heartbeats (a momentary lower reading never lowers the banked value). The
    trade-off: if a same-named agent is *deleted* (purging its _final_cost) and
    then respawned with the same actor_id, the new life's spend is absorbed into
    the existing high-water rather than added on top — i.e. it can undercount in
    that narrow case. That is preferred over inferring "a new life started" from
    a cost regression, which a stale frame would misread and double-count.
    """
    if not agent_id or cost_usd is None:
        return
    try:
        cost = float(cost_usd)
    except (TypeError, ValueError):
        return
    if cost <= 0:
        return
    ensure_lifetime_loaded()
    if cost <= lifetime_cost.get(agent_id, 0.0):
        return
    lifetime_cost[agent_id] = cost
    if runtime.db is not None:
        # A copy, taken now, written in order with the ledger's resets.
        db_writer.writer.kv_set("_system", LIFETIME_LEDGER_KEY, lifetime_cost)


def forget_lifetime_cost(agent_id: str) -> None:
    """Drop one agent's banked lifetime cost, for a reset of its metrics.

    Saved at once, as a raise is: removed only from memory, the old high-water
    comes back with the ledger on the next start, and pins the total again.
    """
    ensure_lifetime_loaded()
    if lifetime_cost.pop(agent_id, None) is None or runtime.db is None:
        return
    db_writer.writer.kv_set("_system", LIFETIME_LEDGER_KEY, lifetime_cost)


def reset_lifetime() -> None:
    """Empty the ledger, in memory now and in SQLite after every save already queued."""
    lifetime_cost.clear()
    if runtime.db is not None:
        db_writer.writer.kv_delete("_system", LIFETIME_LEDGER_KEY)


def lifetime_cost_total() -> float:
    """The ledger's total, from memory: the server read the ledger as it started."""
    return sum(lifetime_cost.values())


# ── What the database holds, for the headline totals ────────────────────────


@dataclass(frozen=True)
class StoredTotals:
    """The database's part of the headline totals, read together on a thread."""

    #: Each agent's persisted _final_cost, by the agent it is kept under.
    final_costs: Mapping[str, float] = field(default_factory=dict)
    #: The same costs by the name each row records, for agents no longer live.
    final_by_name: tuple[tuple[str, float], ...] = ()
    #: Each agent's persisted _messages_processed.
    messages: tuple[tuple[str, int], ...] = ()
    #: Spend accrued at call time, which no deletion lowers.
    alltime: float = 0.0


_stored = StoredTotals()
#: Refreshes are numbered as they start; one finishing after a newer one is dropped.
_refreshes_started = 0
_refresh_applied = 0


def stored_totals() -> StoredTotals:
    """What the last refresh read; empty before the first."""
    return _stored


def read_stored_totals() -> StoredTotals:
    """Read the database's part of the totals. Run on a thread."""
    db = runtime.db
    if db is None:
        return StoredTotals()
    final_costs: dict[str, float] = {}
    by_name: list[tuple[str, float]] = []
    messages: list[tuple[str, int]] = []
    try:
        for agent, entry in db.kv_values("_final_cost"):
            try:
                spent = float(entry.get("cost_usd", 0.0))
            except (AttributeError, TypeError, ValueError):
                continue
            final_costs[agent] = spent
            by_name.append((str(entry.get("name", "")), spent))
        for agent, entry in db.kv_values("_messages_processed"):
            try:
                messages.append((agent, int(entry.get("count", 0))))
            except (AttributeError, TypeError, ValueError):
                continue
    except Exception as exc:
        logger.debug("[cost] the stored totals could not be read: %s", exc)
    try:
        alltime = get_global_alltime_cost()
    except Exception:
        alltime = 0.0
    return StoredTotals(final_costs, tuple(by_name), tuple(messages), alltime)


def use_stored_totals(totals: StoredTotals) -> None:
    """Make ``totals`` what the snapshot reads, now: for a caller with no loop to keep free."""
    global _stored, _refreshes_started, _refresh_applied
    _refreshes_started += 1
    _refresh_applied = _refreshes_started
    _stored = totals


async def refresh_totals() -> None:
    """Read the totals again on a thread, unless a refresh started later has already landed."""
    global _stored, _refreshes_started, _refresh_applied
    _refreshes_started += 1
    number = _refreshes_started
    totals = await asyncio.to_thread(read_stored_totals)
    if number > _refresh_applied:
        _stored, _refresh_applied = totals, number


async def settled_totals() -> None:
    """Refresh the totals once every write already queued has landed.

    For a reset or a delete, before it sends the snapshot that shows it: a
    refresh that read the database ahead of the queued delete would show the
    old totals again.
    """
    await db_writer.writer.settled()
    await refresh_totals()


def historical_cost_usd(live_names: set) -> float:
    """Sum _final_cost for agents not in live_names, from the stored totals."""
    return sum(spent for name, spent in _stored.final_by_name if name not in live_names)


def historical_messages(live_names: set) -> int:
    """Sum _messages_processed for agents not in live_names, from the stored totals."""
    return sum(count for agent, count in _stored.messages if agent not in live_names)


def final_costs() -> dict[str, float]:
    """Every agent's persisted _final_cost, by agent name: one read for a whole list of cards."""
    if runtime.db is None:
        return {}
    try:
        entries = runtime.db.kv_values("_final_cost")
    except Exception as exc:
        logger.debug("[cost] the final costs could not be read: %s", exc)
        return {}
    found: dict[str, float] = {}
    for agent, entry in entries:
        try:
            found[agent] = float(entry["cost_usd"])
        except (TypeError, ValueError, KeyError):
            continue
    return found


def _final_cost_from_db(name: str):
    """Read the persisted _final_cost cost_usd for an agent name, or None."""
    if runtime.db is None or not name:
        return None
    try:
        entry = runtime.db.kv_get(name, "_final_cost")
        if isinstance(entry, dict):
            return entry.get("cost_usd")
    except Exception as exc:
        logger.debug("[cost] the final cost of %r could not be read: %s", name, exc)
    return None


def actor_cost(actor, ag: dict, stored: Mapping[str, float] | None = None):
    """Return the most accurate cost available.

    MQTT-derived first, then live object, then SQLite: from ``stored`` when the
    caller read every final cost already (`final_costs`), else one read here.
    """
    mqtt_cost = ag.get("cost_usd")
    if mqtt_cost is not None:
        return mqtt_cost
    live_cost = getattr(actor, "total_cost_usd", None)
    if live_cost:
        return round(live_cost, 6)
    name = getattr(actor, "name", "")
    if stored is not None:
        return stored.get(name)
    return _final_cost_from_db(name)


def best_cost(ag, actor, name: str) -> float:
    """Resolve an agent's cost the SAME way the cards do (_actor_cost): MQTT
    state first, then the live actor object, then the persisted _final_cost row.
    Returns 0.0 when nothing is known so it can be summed safely. The headline
    total must use this — summing only state["cost_usd"] dropped agents whose
    cost lives on the actor object / SQLite.
    """
    if ag is not None:
        c = ag.get("cost_usd")
        if c is not None:
            try:
                return float(c)
            except (TypeError, ValueError):
                pass
    if actor is not None:
        c = getattr(actor, "total_cost_usd", None)
        if c:
            try:
                return float(c)
            except (TypeError, ValueError):
                pass
    return _stored.final_costs.get(name, 0.0)


def best_msgs(ag, actor) -> int:
    """Resolve message count: MQTT state first, then the live actor's metrics."""
    if ag is not None:
        m = ag.get("messages_processed")
        if m:
            try:
                return int(m)
            except (TypeError, ValueError):
                pass
    m = getattr(getattr(actor, "metrics", None), "messages_processed", 0)
    try:
        return int(m) if m else 0
    except (TypeError, ValueError):
        return 0
