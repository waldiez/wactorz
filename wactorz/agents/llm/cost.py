"""Global LLM spend accounting and the configured cap.

One counter per period plus an all-time total, both in the key-value store, so
spend survives a restart and a cap set mid-period still sees what came before.

These functions resolve ``get_db`` and ``datetime`` in *this* module. Tests
that stub either must patch them here — the re-export in ``llm_agent`` is a
separate binding and patching it does not reach this code.
"""

import json
import logging
from datetime import datetime

from ...core.persistence import get_db

logger = logging.getLogger(__name__)


# ── Global cost limit ────────────────────────────────────────────────────────


def _period_key(period: str) -> str:
    now = datetime.now()
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    if period == "weekly":
        # ISO week (%G-W%V): weeks run Mon–Sun and never collapse into a
        # partial "W00" at the start of January the way %Y-W%W does.
        return now.strftime("%G-W%V")
    return now.strftime("%Y-%m")


def _global_cost_kv_key(period: str) -> str:
    return f"_global_cost_{_period_key(period)}"


_GLOBAL_COST_BOOTSTRAP_KEY = "_global_cost_bootstrap_v2"

# Durable, never-rolls-over counter of every LLM call's cost. Unlike the per-agent
# _final_cost rows and the heartbeat-fed lifetime ledger, this is accrued at call
# time and is never reduced by a single agent's deletion or per-agent metrics
# reset — so it is the delete-proof record of total spend and the floor for the
# dashboard's headline total (guaranteeing "this period" can never exceed it).
_GLOBAL_COST_ALLTIME_KEY = "_global_cost_alltime"
# One-shot guard so existing installs seed the all-time counter from whatever
# durable totals they already have, instead of starting the headline floor at 0.
_ALLTIME_SEED_KEY = "_global_cost_alltime_seeded"


def _known_persisted_cost_total(db) -> float:
    """Best available durable lifetime cost, used only for one-time migration."""
    total = 0.0
    try:
        rows = db.conn.execute("SELECT value FROM kv_store WHERE key = '_final_cost'").fetchall()
        for row in rows:
            try:
                value = row[0]
                if isinstance(value, str):
                    value = json.loads(value)
                if isinstance(value, dict):
                    total += float(value.get("cost_usd") or 0.0)
            except Exception:
                pass
    except Exception:
        pass
    try:
        ledger = db.kv_get("_system", "_lifetime_cost_ledger")
        if isinstance(ledger, dict):
            ledger_total = sum(float(v) for v in ledger.values())
            total = max(total, ledger_total)
    except Exception:
        pass
    return round(total, 6)


def _bootstrap_active_global_cost(db, period: str) -> None:
    """Top up the active period counter once when upgrading stored cost data.

    Older builds can have durable _final_cost / lifetime-ledger rows but no
    matching period spend, or can create a too-low period key on the first new
    call after an add-on update. After restart, LLMAgent restores lifetime totals
    as its persisted baseline, so future calls only add deltas and the cap
    counter appears to reset. On first run after this fix, raise the active cap
    period to the known durable total if it is lower. A deliberate reset after
    this migration writes explicit zero keys and is not undone.
    """
    try:
        if db.kv_get("_system", _GLOBAL_COST_BOOTSTRAP_KEY):
            return
    except Exception:
        return
    total = _known_persisted_cost_total(db)
    key = _global_cost_kv_key(period)
    try:
        current = float(db.kv_get("_system", key) or 0.0)
        if total > current:
            db.kv_set("_system", key, total)
        db.kv_set("_system", _GLOBAL_COST_BOOTSTRAP_KEY, True)
    except Exception as exc:
        logger.debug("[cost-limit] bootstrap failed (%s): %s", period, exc)


def _seed_alltime_cost(db) -> None:
    """Seed the all-time counter once from existing durable totals.

    Installs that predate this counter have _final_cost / lifetime-ledger data but
    a zero all-time key. Raise it (never lower it) to the best known total on first
    run so the headline floor is correct from the start. A deliberate reset zeroes
    the key and is not undone — the seed guard stays set.
    """
    try:
        if db.kv_get("_system", _ALLTIME_SEED_KEY):
            return
    except Exception:
        return
    try:
        known = _known_persisted_cost_total(db)
        current = float(db.kv_get("_system", _GLOBAL_COST_ALLTIME_KEY) or 0.0)
        if known > current:
            db.kv_set("_system", _GLOBAL_COST_ALLTIME_KEY, known)
        db.kv_set("_system", _ALLTIME_SEED_KEY, True)
    except Exception as exc:
        logger.debug("[cost-limit] alltime seed failed: %s", exc)


def get_global_alltime_cost() -> float:
    """Durable all-time LLM spend, accrued at call time. Survives agent deletion
    and per-agent metrics resets (unlike _final_cost / the lifetime ledger).
    """
    db = get_db()
    if db is None:
        return 0.0
    try:
        return float(db.kv_get("_system", _GLOBAL_COST_ALLTIME_KEY) or 0.0)
    except Exception:
        return 0.0


def get_global_cost_info() -> dict:
    """Return current period spend and limit. Used by GET /api/cost."""
    from ...config import CONFIG

    db = get_db()
    # Runtime override (set via POST /api/cost/limit) takes priority over env var
    limit = CONFIG.llm_cost_limit_usd
    period = CONFIG.llm_cost_limit_period
    if db is not None:
        try:
            override = db.kv_get("_system", "_cost_limit_override")
            if isinstance(override, dict):
                limit = float(override.get("limit_usd", limit))
                period = override.get("period", period)
        except Exception:
            pass
        _bootstrap_active_global_cost(db, period)
        _seed_alltime_cost(db)
    key = _global_cost_kv_key(period)
    spend = 0.0
    if db is not None:
        try:
            spend = float(db.kv_get("_system", key) or 0.0)
        except Exception:
            pass
    pct = round(spend / limit * 100, 1) if limit > 0 else None
    return {
        "period": period,
        "period_key": _period_key(period),
        "spend_usd": round(spend, 6),
        "limit_usd": limit if limit > 0 else None,
        "pct_used": pct,
        "limit_reached": limit > 0 and spend >= limit,
        "warning": limit > 0 and spend >= limit * 0.8,
    }


def set_cost_limit(limit_usd: float, period: str) -> None:
    """Persist a runtime cost limit override to SQLite."""
    if period not in ("daily", "weekly", "monthly"):
        raise ValueError(f"period must be daily, weekly, or monthly (got {period!r})")
    db = get_db()
    if db is None:
        raise RuntimeError("Database not available")
    db.kv_set("_system", "_cost_limit_override", {"limit_usd": limit_usd, "period": period})


def reset_global_cost() -> dict:
    """Clear accumulated spend for all periods. Returns new spend info."""
    db = get_db()
    if db is None:
        raise RuntimeError("Database not available")
    for period in ("daily", "weekly", "monthly"):
        db.kv_set("_system", _global_cost_kv_key(period), 0.0)
    # Zero the all-time counter too — a full reset wipes the durable cost records
    # (_final_cost / lifetime ledger) it would otherwise be reseeded from. The seed
    # guard stays set so it is not re-seeded from now-purged rows.
    db.kv_set("_system", _GLOBAL_COST_ALLTIME_KEY, 0.0)
    return get_global_cost_info()


def accumulate_global_cost(delta: float) -> None:
    if delta <= 0:
        return
    db = get_db()
    if db is None:
        return
    # Always accumulate, even when no limit is configured. Gating this on a
    # limit meant period spend stayed at $0 until a cap existed, so enabling a
    # cap mid-period gave false protection (spend already incurred was never
    # recorded) and the "Current spend (no limit set)" readout was always $0.
    for period in ("daily", "weekly", "monthly"):
        key = _global_cost_kv_key(period)
        try:
            current = float(db.kv_get("_system", key) or 0.0)
            db.kv_set("_system", key, round(current + delta, 6))
        except Exception as exc:
            logger.debug("[cost-limit] global accumulate failed (%s): %s", period, exc)
    # Same delta into the never-resetting all-time counter so deleted agents'
    # spend is retained in the headline total.
    try:
        current = float(db.kv_get("_system", _GLOBAL_COST_ALLTIME_KEY) or 0.0)
        db.kv_set("_system", _GLOBAL_COST_ALLTIME_KEY, round(current + delta, 6))
    except Exception as exc:
        logger.debug("[cost-limit] alltime accumulate failed: %s", exc)


def check_cost_limit() -> None:
    info = get_global_cost_info()
    if not info.get("limit_usd"):
        return
    if info["limit_reached"]:
        raise RuntimeError(
            f"LLM cost limit of ${info['limit_usd']:.2f} reached "
            f"for {info['period_key']}. Blocking further LLM calls."
        )
    if info["warning"]:
        logger.warning(
            "[cost-limit] %.1f%% of $%.2f %s budget used ($%.4f)",
            info["pct_used"],
            info["limit_usd"],
            info["period"],
            info["spend_usd"],
        )
