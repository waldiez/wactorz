"""Cost + message-count accounting for the monitor.

Owns the in-memory lifetime ledger (hydrated once from SQLite) and the
"best available number" resolution used by the dashboard: live MQTT state
first, then the actor's own metrics, then the persisted final value.
"""

import logging
from collections.abc import Mapping

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
    except Exception as exc:
        logger.debug("[cost] the lifetime ledger could not be read: %s", exc)
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
        except Exception as exc:
            logger.debug("[cost] the lifetime ledger could not be saved: %s", exc)


def forget_lifetime_cost(agent_id: str) -> None:
    """Drop one agent's banked lifetime cost, for a reset of its metrics.

    Saved at once, as a raise is: removed only from memory, the old high-water
    comes back with the ledger on the next start, and pins the total again.
    """
    ensure_lifetime_loaded()
    if lifetime_cost.pop(agent_id, None) is None or runtime.db is None:
        return
    try:
        runtime.db.kv_set("_system", LIFETIME_LEDGER_KEY, lifetime_cost)
    except Exception as exc:
        logger.debug("[cost] the lifetime ledger could not be saved: %s", exc)


def lifetime_cost_total() -> float:
    """Get the total lifetime cost."""
    ensure_lifetime_loaded()
    return sum(lifetime_cost.values())


def historical_cost_usd(live_names: set) -> float:
    """Sum _final_cost for agents not in live_names."""
    if runtime.db is None:
        return 0.0
    try:
        entries = runtime.db.kv_values("_final_cost")
    except Exception:
        return 0.0
    total = 0.0
    for _agent, entry in entries:
        try:
            if entry.get("name") not in live_names:
                total += entry.get("cost_usd", 0.0)
        except Exception as exc:
            logger.debug("[cost] a deleted agent's cost row was skipped: %s", exc)
    return total


def historical_messages(live_names: set) -> int:
    """Sum _messages_processed for agents not in live_names."""
    if runtime.db is None:
        return 0
    try:
        entries = runtime.db.kv_values("_messages_processed")
    except Exception:
        return 0
    total = 0
    for agent_name, entry in entries:
        if agent_name not in live_names:
            try:
                total += entry.get("count", 0)
            except Exception as exc:
                logger.debug("[cost] a deleted agent's call-count row was skipped: %s", exc)
    return total


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
