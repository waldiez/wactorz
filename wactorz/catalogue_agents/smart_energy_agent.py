"""
CATALOG AGENT — smart-energy
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LLM-powered energy brain for smart plugs exposed through Home Assistant.
Brand-agnostic: works with any plug HA can see (Tapo, Shelly, Sonoff, Kasa…)
— you point it at HA entity IDs, not a vendor SDK.

WHAT IT DOES (always-on core)
─────────────────────────────
  • Polls each plug's HA power sensor → publishes live watts to MQTT and lets
    the timeseries-collector ingest it for history.
  • Tracks energy (kWh) and cost per plug for today / this week / this month,
    using a configurable per-kWh rate (default €0.138/kWh).
  • Publishes a live summary snapshot of every plug for dashboards.
  • Answers natural-language questions about usage and cost via its LLM.

WHAT IT DOES NOT DO BY DEFAULT
──────────────────────────────
  No use-case logic is baked in. Auto-off, alerts, "turn the printer off when
  it's done" — these are RULES the user requests through main, which forwards
  them as TASK messages. The agent starts with zero rules.

🔒 PLUG PROTECTION — THE HARD GUARD
───────────────────────────────────
  Every plug has a `protection` level:
    "locked"            → the agent will NEVER issue a turn-off for this plug.
                          Any code path that tries raises PlugProtectedError.
                          Use for the AC, the AI training rig, servers, NAS —
                          anything that must never lose power.
    "auto_off_on_idle"  → the ONLY level the agent may power down, and only
                          when an idle rule's condition is met. Use for a 3D
                          printer that should switch off after a print + cool-down.
    "manual"            → monitor only; no automatic actions ever. The user can
                          still toggle it themselves in HA.

  The guard is enforced in code, not config — a future rule, LLM reply, or
  refactor cannot bypass it. A "locked" plug is locked, full stop.

3D-PRINTER AUTO-OFF (an example rule, not a feature)
────────────────────────────────────────────────────
  User asks main: "turn off my printer when it's done printing".
  main sends:  {"action":"add_rule","rule":{"type":"auto_off_on_idle",
                "plug":"printer","idle_threshold_watts":20,"idle_delay_s":180}}
  Behaviour: while watts ≥ threshold the print is running and the timer resets;
  once watts stay BELOW threshold for idle_delay_s (default 180s = 3min, to let
  the hotend fans finish cooling) the agent turns the plug off and emits
  `wactorz/energy/auto_off`. If you don't know the printer's idle wattage, leave
  the threshold unset — the agent watches and logs observed min/max so you can
  tune it after one print, but won't power anything down until a threshold is set.

NOTIFICATIONS
─────────────
  Not this agent's job. It emits `wactorz/energy/auto_off` (and other events);
  the user asks main to spawn a notifier (Discord/Telegram/etc.) that subscribes
  to that topic. Keeps energy logic and delivery cleanly separated.

MQTT CONTRACT
─────────────
  Publish:  custom/sensors/energy/{plug}/power    {watts, kwh_today, ...}
            custom/sensors/energy/{plug}/cost     {cost_today, cost_week, ...}
            custom/sensors/energy/summary         {plugs:[...], total_watts, ...}
            wactorz/energy/auto_off               {plug, reason, ...}
  (timeseries-collector already subscribes to custom/sensors/# → free history)

SPAWN / TASK CONFIG
───────────────────
{
  "name": "smart-energy",
  "type": "dynamic",
  "capabilities": ["energy_monitoring","smart_plug","cost_tracking","home_assistant"],
  "input_schema": {
    "action": "str — status|report|cost|add_plug|list_plugs|remove_plug|"
              "add_rule|list_rules|remove_rule|set_rate|configure|<free text>",
    "plug":   "dict|str — plug config for add_plug, or name for remove_plug",
    "rule":   "dict — rule config for add_rule",
    "rate":   "float — €/kWh for set_rate"
  },
  "output_schema": {
    "plugs_monitored": "int",
    "active_rules":    "int",
    "total_watts":     "float"
  },
  "poll_interval": 30
}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

AGENT_CODE = r'''
import asyncio
import datetime
import json
import time


# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_RATE          = 0.138      # €/kWh — adjustable per deployment
DEFAULT_CURRENCY      = "EUR"
DEFAULT_IDLE_DELAY_S  = 180        # 3 min cool-down before printer auto-off
SUMMARY_TOPIC         = "custom/sensors/energy/summary"
AUTO_OFF_TOPIC        = "wactorz/energy/auto_off"

# Protection levels
LOCKED        = "locked"             # NEVER turned off — hard guard
AUTO_OFF      = "auto_off_on_idle"   # may be turned off by an idle rule only
MANUAL        = "manual"             # monitor only, no auto actions
VALID_PROTECTIONS = (LOCKED, AUTO_OFF, MANUAL)


class PlugProtectedError(Exception):
    """Raised when something attempts to turn off a protected (locked) plug.

    This is the teeth behind the protection guarantee: it is not a soft skip,
    it is an exception that aborts the turn-off path entirely."""
    pass


# ══════════════════════════════════════════════════════════════════════════════
# HOME ASSISTANT ACCESS
# ══════════════════════════════════════════════════════════════════════════════

def _ha_creds():
    """Resolve HA url+token from app config (supervisor token in the addon)."""
    from wactorz.config import CONFIG
    return CONFIG.ha_url, CONFIG.ha_token


async def _ha_get_states() -> dict:
    """Return {entity_id: state_dict} for all HA entities, or {} on failure."""
    url, token = _ha_creds()
    if not url or not token:
        return {}
    try:
        from wactorz.core.integrations.home_assistant.ha_helper import get_states
        states = await get_states(url, token)
        return {s.get("entity_id"): s for s in (states or []) if s.get("entity_id")}
    except Exception:
        return {}


async def _ha_turn_off(entity_id: str):
    """Issue switch.turn_off for an entity. Caller MUST have passed the guard."""
    url, token = _ha_creds()
    if not url or not token:
        raise RuntimeError("HA not configured (HA_URL/HA_TOKEN missing)")
    from wactorz.core.integrations.home_assistant.ha_helper import normalize_ha_ws_url
    from wactorz.core.integrations.home_assistant.ha_web_socket_client import HAWebSocketClient
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "switch"
    async with HAWebSocketClient(normalize_ha_ws_url(url), token) as ha:
        await ha.call_service(domain, "turn_off", entity_id)


def _read_watts(state: dict):
    """Pull a numeric wattage from an HA power sensor state dict."""
    if not state:
        return None
    raw = state.get("state")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════════════
# THE GUARD — single chokepoint for ALL turn-off attempts
# ══════════════════════════════════════════════════════════════════════════════

def _assert_can_turn_off(plug: dict):
    """The ONE place turn-off permission is decided.

    Raises PlugProtectedError unless the plug is explicitly auto_off_on_idle.
    locked and manual plugs can never be powered down by this agent."""
    prot = plug.get("protection", LOCKED)
    if prot != AUTO_OFF:
        raise PlugProtectedError(
            f"Refusing to turn off plug '{plug.get('name')}' — protection={prot}. "
            f"Only '{AUTO_OFF}' plugs may be powered down by this agent."
        )


async def _safe_turn_off(agent, plug: dict, reason: str) -> bool:
    """Turn a plug off, but only after passing the guard. Returns True if off."""
    try:
        _assert_can_turn_off(plug)
    except PlugProtectedError as e:
        await agent.log(f"🔒 {e}", level="warning")
        return False

    entity = plug.get("ha_entity_switch")
    if not entity:
        await agent.log(f"Plug '{plug.get('name')}' has no ha_entity_switch — cannot turn off",
                        level="warning")
        return False

    try:
        await _ha_turn_off(entity)
    except Exception as e:
        await agent.log(f"Turn-off failed for '{plug.get('name')}' ({entity}): {e}",
                        level="error")
        return False

    await agent.log(f"⚡ Turned OFF '{plug.get('name')}' ({entity}) — {reason}")
    await agent.publish(AUTO_OFF_TOPIC, {
        "plug":      plug.get("name"),
        "entity_id": entity,
        "reason":    reason,
        "ts":        time.time(),
    })
    return True


# ══════════════════════════════════════════════════════════════════════════════
# COST / ENERGY ACCOUNTING
# ══════════════════════════════════════════════════════════════════════════════

def _period_keys(now: float) -> dict:
    dt = datetime.datetime.fromtimestamp(now)
    iso = dt.isocalendar()
    return {
        "day":   dt.strftime("%Y-%m-%d"),
        "week":  f"{iso[0]}-W{iso[1]:02d}",
        "month": dt.strftime("%Y-%m"),
    }


def _accumulate(acc: dict, plug_name: str, watts: float, dt_h: float, now: float) -> dict:
    """Integrate watts over dt_h into per-period kWh accumulators, resetting
    a bucket when its calendar period rolls over."""
    rec = acc.setdefault(plug_name, {
        "day_kwh": 0.0, "week_kwh": 0.0, "month_kwh": 0.0, "total_kwh": 0.0,
        "day": "", "week": "", "month": "",
    })
    keys = _period_keys(now)
    for period in ("day", "week", "month"):
        if rec[period] != keys[period]:
            rec[period] = keys[period]
            rec[f"{period}_kwh"] = 0.0

    kwh = (watts / 1000.0) * dt_h
    if kwh > 0:
        rec["day_kwh"]   += kwh
        rec["week_kwh"]  += kwh
        rec["month_kwh"] += kwh
        rec["total_kwh"] += kwh
    return rec


def _cost(kwh: float, rate: float) -> float:
    return round(kwh * rate, 4)


# ══════════════════════════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════════════════════════

async def setup(agent):
    agent.state["plugs"]   = agent.recall("plugs")   or {}   # name -> plug dict
    agent.state["rules"]   = agent.recall("rules")   or {}   # rule_id -> rule dict
    agent.state["accum"]   = agent.recall("accum")   or {}   # name -> kWh accumulators
    agent.state["rate"]    = float(agent.recall("rate") or DEFAULT_RATE)
    agent.state["currency"] = agent.recall("currency") or DEFAULT_CURRENCY
    agent.state["last_watts"] = {}      # name -> last seen watts (live, not persisted)
    agent.state["last_poll"]  = {}      # name -> ts of last reading for dt integration
    agent.state["idle_since"] = {}      # name -> ts watts first dropped below threshold
    agent.state["auto_off_fired"] = {}  # name -> bool, so we don't re-fire while off
    agent.state["observed"]   = {}      # name -> {"min":..,"max":..} for calibration

    agent.declare_contract(
        publishes=[
            "custom/sensors/energy",
            SUMMARY_TOPIC,
            AUTO_OFF_TOPIC,
        ],
        subscribes=[],
    )

    n_plugs = len(agent.state["plugs"])
    n_rules = len(agent.state["rules"])
    await agent.log(
        f"Smart energy ready | plugs={n_plugs} | rules={n_rules} | "
        f"rate={agent.state['rate']} {agent.state['currency']}/kWh"
    )


# ══════════════════════════════════════════════════════════════════════════════
# PROCESS LOOP — poll, account, evaluate rules
# ══════════════════════════════════════════════════════════════════════════════

async def process(agent):
    plugs = agent.state["plugs"]
    if not plugs:
        return

    now    = time.time()
    states = await _ha_get_states()
    rate   = agent.state["rate"]
    accum  = agent.state["accum"]
    summary = []
    total_watts = 0.0

    for name, plug in plugs.items():
        power_entity = plug.get("ha_entity_power")
        watts = _read_watts(states.get(power_entity)) if power_entity else None
        if watts is None:
            # No reading this cycle — keep last known for rule continuity but skip accounting
            watts = agent.state["last_watts"].get(name)
            if watts is None:
                continue

        # ── Energy + cost accounting ──────────────────────────────────────────
        last_ts = agent.state["last_poll"].get(name)
        if last_ts:
            dt_h = (now - last_ts) / 3600.0
            if 0 < dt_h < 1.0:   # ignore absurd gaps (restart) > 1h
                _accumulate(accum, name, watts, dt_h, now)
        agent.state["last_poll"][name]  = now
        agent.state["last_watts"][name] = watts
        total_watts += watts

        rec = accum.get(name, {})
        await agent.publish(f"custom/sensors/energy/{name}/power", {
            "entity_id": power_entity,
            "watts":     round(watts, 2),
            "kwh_today": round(rec.get("day_kwh", 0.0), 4),
            "ts":        now,
        })
        await agent.publish(f"custom/sensors/energy/{name}/cost", {
            "currency":   agent.state["currency"],
            "rate":       rate,
            "cost_today": _cost(rec.get("day_kwh", 0.0), rate),
            "cost_week":  _cost(rec.get("week_kwh", 0.0), rate),
            "cost_month": _cost(rec.get("month_kwh", 0.0), rate),
            "ts":         now,
        })

        summary.append({
            "plug":       name,
            "watts":      round(watts, 2),
            "protection": plug.get("protection", LOCKED),
            "cost_today": _cost(rec.get("day_kwh", 0.0), rate),
        })

        # ── Rule evaluation ───────────────────────────────────────────────────
        await _evaluate_rules(agent, name, plug, watts, now)

    # ── Summary snapshot for dashboards ──────────────────────────────────────
    await agent.publish(SUMMARY_TOPIC, {
        "plugs":       summary,
        "total_watts": round(total_watts, 2),
        "currency":    agent.state["currency"],
        "rate":        rate,
        "ts":          now,
    })

    # Persist accumulators periodically (every cycle is fine — small dict)
    agent.persist("accum", accum)


async def _evaluate_rules(agent, plug_name: str, plug: dict, watts: float, now: float):
    """Run any rules attached to this plug. Currently: auto_off_on_idle."""
    for rule in agent.state["rules"].values():
        if rule.get("plug") != plug_name:
            continue
        if rule.get("type") == "auto_off_on_idle":
            await _eval_auto_off_on_idle(agent, plug, rule, watts, now)


async def _eval_auto_off_on_idle(agent, plug: dict, rule: dict, watts: float, now: float):
    name      = plug["name"]
    threshold = rule.get("idle_threshold_watts")
    delay     = float(rule.get("idle_delay_s", DEFAULT_IDLE_DELAY_S))

    # ── Calibration mode: no threshold yet → observe & log, never power off ──
    if threshold is None:
        obs = agent.state["observed"].setdefault(name, {"min": watts, "max": watts})
        obs["min"] = min(obs["min"], watts)
        obs["max"] = max(obs["max"], watts)
        return

    threshold = float(threshold)

    # If the plug drew power again, clear any prior auto-off latch (new print)
    if watts >= threshold:
        agent.state["idle_since"][name] = None
        agent.state["auto_off_fired"][name] = False
        return

    # Below threshold → idle. Start / continue the cool-down timer.
    if agent.state["auto_off_fired"].get(name):
        return  # already turned it off; wait for it to come back on

    idle_since = agent.state["idle_since"].get(name)
    if idle_since is None:
        agent.state["idle_since"][name] = now
        return

    if (now - idle_since) >= delay:
        ok = await _safe_turn_off(
            agent, plug,
            reason=f"idle <{threshold}W for {int(now - idle_since)}s (rule {rule.get('id')})",
        )
        if ok:
            agent.state["auto_off_fired"][name] = True
            agent.state["idle_since"][name] = None


# ══════════════════════════════════════════════════════════════════════════════
# handle_task — commands + natural language
# ══════════════════════════════════════════════════════════════════════════════

async def handle_task(agent, payload):
    # Unwrap JSON sent as text via @mention
    if isinstance(payload, dict) and not payload.get("action") and payload.get("text"):
        try:
            parsed = json.loads(payload["text"])
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            pass

    if not isinstance(payload, dict):
        payload = {"action": str(payload)}

    action = str(payload.get("action") or "").strip().lower()

    if action == "status":
        return _status(agent)
    if action in ("cost", "report"):
        return await _report(agent, payload)
    if action == "add_plug":
        return _add_plug(agent, payload.get("plug"))
    if action == "list_plugs":
        return {"result": "plugs", "plugs": list(agent.state["plugs"].values())}
    if action == "remove_plug":
        return _remove_plug(agent, payload.get("plug") or payload.get("name"))
    if action == "add_rule":
        return _add_rule(agent, payload.get("rule"))
    if action == "list_rules":
        return {"result": "rules", "rules": list(agent.state["rules"].values())}
    if action == "remove_rule":
        return _remove_rule(agent, payload.get("rule") or payload.get("id"))
    if action == "set_rate":
        return _set_rate(agent, payload.get("rate"), payload.get("currency"))

    # ── Free-text → LLM answers from live state + history ────────────────────
    text = str(payload.get("action") or payload.get("text") or "").strip()
    if text:
        return await _ask_llm(agent, text)

    return {
        "result": ("Commands: status, cost/report, add_plug, list_plugs, "
                   "remove_plug, add_rule, list_rules, remove_rule, set_rate. "
                   "Or ask a question in plain English."),
    }


def _status(agent) -> dict:
    plugs = agent.state["plugs"]
    rate  = agent.state["rate"]
    rows  = []
    total_w = 0.0
    for name, plug in plugs.items():
        w   = agent.state["last_watts"].get(name)
        rec = agent.state["accum"].get(name, {})
        total_w += (w or 0.0)
        rows.append(
            f"  {name} [{plug.get('protection', LOCKED)}]: "
            f"{w if w is not None else '—'}W  "
            f"today {_cost(rec.get('day_kwh', 0.0), rate)} {agent.state['currency']}"
        )
    text = (
        f"Smart energy — {len(plugs)} plug(s), {len(agent.state['rules'])} rule(s), "
        f"rate {rate} {agent.state['currency']}/kWh\n" + ("\n".join(rows) if rows else "  (no plugs)")
        + f"\n  total now: {round(total_w, 1)}W"
    )
    return {
        "result":          text,
        "plugs_monitored": len(plugs),
        "active_rules":    len(agent.state["rules"]),
        "total_watts":     round(total_w, 2),
    }


async def _report(agent, payload) -> dict:
    rate = agent.state["rate"]
    cur  = agent.state["currency"]
    lines = [f"Cost report (rate {rate} {cur}/kWh):"]
    grand = {"day": 0.0, "week": 0.0, "month": 0.0}
    for name, rec in agent.state["accum"].items():
        d, w, m = rec.get("day_kwh", 0), rec.get("week_kwh", 0), rec.get("month_kwh", 0)
        grand["day"] += d; grand["week"] += w; grand["month"] += m
        lines.append(
            f"  {name}: today {_cost(d, rate)}{cur} ({d:.3f}kWh) | "
            f"week {_cost(w, rate)}{cur} | month {_cost(m, rate)}{cur}"
        )
    lines.append(
        f"  TOTAL: today {_cost(grand['day'], rate)}{cur} | "
        f"week {_cost(grand['week'], rate)}{cur} | month {_cost(grand['month'], rate)}{cur}"
    )
    return {
        "result":      "\n".join(lines),
        "cost_today":  _cost(grand["day"], rate),
        "cost_week":   _cost(grand["week"], rate),
        "cost_month":  _cost(grand["month"], rate),
        "currency":    cur,
    }


def _add_plug(agent, plug) -> dict:
    if isinstance(plug, str):
        return {"result": "error", "error": "add_plug needs a plug object, not a name"}
    if not isinstance(plug, dict) or not plug.get("name"):
        return {"result": "error", "error": "plug must be a dict with at least 'name'"}

    name = plug["name"]
    prot = plug.get("protection", LOCKED)
    if prot not in VALID_PROTECTIONS:
        return {"result": "error",
                "error": f"protection must be one of {VALID_PROTECTIONS}, got '{prot}'"}

    # Default to the safest protection if unspecified
    plug.setdefault("protection", LOCKED)
    plug.setdefault("cost_per_kwh", agent.state["rate"])
    agent.state["plugs"][name] = plug
    agent.persist("plugs", agent.state["plugs"])
    return {"result": f"Added plug '{name}' (protection={plug['protection']})",
            "plug": plug}


def _remove_plug(agent, name) -> dict:
    if name in agent.state["plugs"]:
        del agent.state["plugs"][name]
        agent.persist("plugs", agent.state["plugs"])
        return {"result": f"Removed plug '{name}'"}
    return {"result": "error", "error": f"no plug named '{name}'"}


def _add_rule(agent, rule) -> dict:
    if not isinstance(rule, dict) or not rule.get("type"):
        return {"result": "error", "error": "rule must be a dict with a 'type'"}
    plug_name = rule.get("plug")
    plug = agent.state["plugs"].get(plug_name)
    if not plug:
        return {"result": "error", "error": f"rule references unknown plug '{plug_name}'"}

    # Safety: an auto_off rule on a protected plug is rejected up front so the
    # user gets a clear error instead of silent no-ops at runtime.
    if rule["type"] == "auto_off_on_idle" and plug.get("protection") != AUTO_OFF:
        return {"result": "error",
                "error": (f"plug '{plug_name}' has protection="
                          f"'{plug.get('protection')}'. An auto_off rule requires "
                          f"protection='{AUTO_OFF}'. This is intentional — locked "
                          f"plugs can never be powered down.")}

    rid = rule.get("id") or f"rule_{int(time.time())}"
    rule["id"] = rid
    agent.state["rules"][rid] = rule
    agent.persist("rules", agent.state["rules"])
    return {"result": f"Added rule '{rid}' ({rule['type']}) on plug '{plug_name}'",
            "rule": rule}


def _remove_rule(agent, rid) -> dict:
    if rid in agent.state["rules"]:
        del agent.state["rules"][rid]
        agent.persist("rules", agent.state["rules"])
        return {"result": f"Removed rule '{rid}'"}
    return {"result": "error", "error": f"no rule '{rid}'"}


def _set_rate(agent, rate, currency) -> dict:
    try:
        agent.state["rate"] = float(rate)
    except (TypeError, ValueError):
        return {"result": "error", "error": f"invalid rate '{rate}'"}
    if currency:
        agent.state["currency"] = str(currency)
    agent.persist("rate", agent.state["rate"])
    agent.persist("currency", agent.state["currency"])
    return {"result": f"Rate set to {agent.state['rate']} {agent.state['currency']}/kWh"}


async def _ask_llm(agent, question: str) -> dict:
    """Answer a free-text question using current readings + accumulators."""
    if agent.llm is None:
        return {"result": "No LLM configured — try: status, cost, list_plugs, list_rules"}

    rate = agent.state["rate"]
    snapshot = {
        "rate_per_kwh": rate,
        "currency":     agent.state["currency"],
        "plugs": {
            name: {
                "watts_now":  agent.state["last_watts"].get(name),
                "protection": p.get("protection", LOCKED),
                "kwh_today":  round(agent.state["accum"].get(name, {}).get("day_kwh", 0.0), 4),
                "kwh_month":  round(agent.state["accum"].get(name, {}).get("month_kwh", 0.0), 4),
            }
            for name, p in agent.state["plugs"].items()
        },
        "rules": list(agent.state["rules"].values()),
    }
    system = (
        "You are the smart-energy agent for a Home Assistant setup. Answer the "
        "user's question using ONLY the JSON snapshot of live plug readings and "
        "cost accumulators provided. Costs = kWh * rate_per_kwh. Be concise. "
        "Never suggest turning off a plug whose protection is 'locked'."
    )
    prompt = f"SNAPSHOT:\n{json.dumps(snapshot, indent=2)}\n\nQUESTION: {question}"
    try:
        answer = await agent.llm.chat(prompt, system=system)
    except Exception as e:
        return {"result": f"LLM error: {e}", "snapshot": snapshot}
    return {"result": answer, "snapshot": snapshot}
'''
