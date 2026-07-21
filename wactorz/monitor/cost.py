"""Cost + message-count accounting for the monitor.

Owns the in-memory lifetime ledger (hydrated once from SQLite) and the
"best available number" resolution used by the dashboard: live MQTT state
first, then the actor's own metrics, then the persisted final value.
"""

import json
import logging

from . import runtime

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


def ensure_lifetime_loaded() -> None:
    """Lazily hydrate the in-memory ledger from SQLite once db is injected."""
    global lifetime_loaded
    if lifetime_loaded or runtime.db is None:
        return
    try:
        stored = runtime.db.kv_get("_system", LIFETIME_LEDGER_KEY)
        if isinstance(stored, dict):
            for k, v in stored.items():  # pyright: ignore[reportGeneralTypeIssues]
                try:
                    lifetime_cost[k] = float(v)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    lifetime_loaded = True


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
        try:
            runtime.db.kv_set("_system", LIFETIME_LEDGER_KEY, lifetime_cost)
        except Exception:
            pass


def lifetime_cost_total() -> float:
    """Get the total lifetime cost."""
    ensure_lifetime_loaded()
    return sum(lifetime_cost.values())


def reset_actor_cost(actor) -> None:
    """Zero a live actor's cost/token counters AND its per-call accrual baseline.

    The baseline must move with total_cost_usd: _persist_cost / _accrue_usage
    accumulate (total_cost_usd - baseline) into the global period and all-time
    counters. Zeroing total_cost_usd on a reset without also zeroing the baseline
    leaves the baseline at the pre-reset total, so every subsequent call yields a
    negative delta and the period/all-time counters stop advancing until spend
    climbs back past the old total — the "limit count stopped counting after a
    wipe" bug.
    """
    if not hasattr(actor, "total_cost_usd"):
        return
    actor.total_cost_usd = 0.0
    actor.total_input_tokens = 0
    actor.total_output_tokens = 0
    if hasattr(actor, "_last_persisted_usd"):
        actor._last_persisted_usd = 0.0
    if hasattr(actor, "_last_period_cost_usd"):
        actor._last_period_cost_usd = 0.0


def historical_cost_usd(live_names: set) -> float:
    """Sum _final_cost for agents not in live_names."""
    if runtime.db is None:
        return 0.0
    try:
        rows = runtime.db.conn.execute(
            "SELECT value FROM kv_store WHERE key = '_final_cost'"
        ).fetchall()
        total = 0.0
        for row in rows:
            try:
                entry = json.loads(row[0])
                if entry.get("name") not in live_names:
                    total += entry.get("cost_usd", 0.0)
            except Exception:
                pass
        return total
    except Exception:
        return 0.0


def historical_messages(live_names: set) -> int:
    """Sum _messages_processed for agents not in live_names."""
    if runtime.db is None:
        return 0
    try:
        rows = runtime.db.conn.execute(
            "SELECT agent, value FROM kv_store WHERE key = '_messages_processed'"
        ).fetchall()
        total = 0
        for agent_name, value in rows:
            if agent_name not in live_names:
                try:
                    entry = json.loads(value)
                    total += entry.get("count", 0)
                except Exception:
                    pass
        return total
    except Exception:
        return 0


def _final_cost_from_db(name: str):
    """Read the persisted _final_cost cost_usd for an agent name, or None."""
    if runtime.db is None or not name:
        return None
    try:
        row = runtime.db.conn.execute(
            "SELECT value FROM kv_store WHERE agent=? AND key='_final_cost'",
            (name,),
        ).fetchone()
        if row:
            return json.loads(row[0]).get("cost_usd")
    except Exception:
        pass
    return None


def actor_cost(actor, ag: dict):
    """Return the most accurate cost available.

    MQTT-derived first, then live object, then SQLite.
    """
    mqtt_cost = ag.get("cost_usd")
    if mqtt_cost is not None:
        return mqtt_cost
    live_cost = getattr(actor, "total_cost_usd", None)
    if live_cost:
        return round(live_cost, 6)
    return _final_cost_from_db(getattr(actor, "name", ""))


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
    c = _final_cost_from_db(name)
    try:
        return float(c) if c is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


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
