"""CATALOG AGENT — smart-energy
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LLM-powered energy brain for smart plugs exposed through Home Assistant.
Brand-agnostic: works with any plug HA can see (Tapo, Shelly, Sonoff, Kasa…)
— you point it at HA entity IDs, not a vendor SDK.

WHAT IT DOES (always-on core)
─────────────────────────────
  • Conversational onboarding for non-technical users: say "import my plugs"
    and it scans Home Assistant, shows what it found with live wattage, and
    asks in plain English which to monitor — no JSON, no entity IDs to type.
  • Polls each plug's HA power sensor → publishes live watts to MQTT and lets
    the timeseries-collector ingest it for history.
  • Tracks energy (kWh) and cost per plug for today / this week / this month,
    using a configurable per-kWh rate (default €0.138/kWh).
  • Publishes a live summary snapshot of every plug for dashboards.
  • Answers natural-language questions about usage and cost via its LLM.

CONVERSATIONAL FLOW (the primary interface)
───────────────────────────────────────────
  User: "import my plugs"
  Agent: scans HA → "I found these plugs that report power usage:
           1. AC Power — 362 W right now
         Which would you like me to monitor? (all / by number / by name)
         I'll only watch usage and cost — I will never turn these off."
  User: "all"  (or "1 and 3", or "the AC one")
  Agent: adds them ALL as 'locked' (never-off) and confirms.

  Every imported plug defaults to 'locked'. Auto-off is never part of import;
  it's a separate, explicit request the user makes later for one named plug.
  The raw JSON commands (add_plug, add_rule, …) still work for power users and
  for main's routing, but a human never has to use them.

WHAT IT DOES NOT DO BY DEFAULT
──────────────────────────────
  No use-case logic is baked in. Auto-off, alerts, "turn the printer off when
  it's done" — these are RULES the user requests through main, which forwards
  them as TASK messages. The agent starts with zero rules.

PLUG PROTECTION — THE HARD GUARD
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


# ── Defaults — read from env/config so deployers set ENERGY_RATE / ENERGY_CURRENCY
def _default_rate():
    try:
        from wactorz.config import CONFIG
        return float(CONFIG.energy_rate)
    except Exception:
        return 0.138

def _default_currency():
    try:
        from wactorz.config import CONFIG
        return str(CONFIG.energy_currency) or "EUR"
    except Exception:
        return "EUR"

DEFAULT_RATE          = _default_rate()
DEFAULT_CURRENCY      = _default_currency()
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


def _account(acc: dict, name: str, now: float, watts, dt_h,
             energy_today_kwh, energy_kind,
             energy_month_kwh=None, energy_total_kwh=None) -> dict:
    """Update a plug's per-period kWh buckets.

    Sources, by period, in priority order:
      • A dedicated device meter for that exact period (energy_month_kwh /
        energy_total_kwh): the plug's own absolute counter — e.g. Tapo's "this
        month's consumption" sensor. Mirrored straight into the bucket, just
        like 'today' below. Most accurate: it already reflects the whole
        period, including usage from before wactorz started watching.
      • The 'today' meter's per-poll delta, added to whichever periods don't
        have their own dedicated sensor above.
      • ESTIMATED (no meter at all): integrate live watts over the elapsed
        interval. Least accurate, and only counts from when we started watching.

    Buckets reset when their calendar period rolls over (a no-op for periods
    fed by their own absolute meter, since that's overwritten every poll anyway)."""
    rec = acc.setdefault(name, {
        "day_kwh": 0.0, "week_kwh": 0.0, "month_kwh": 0.0, "total_kwh": 0.0,
        "day": "", "week": "", "month": "",
        "meter_last": None, "source": "estimated",
    })
    keys = _period_keys(now)
    for period in ("day", "week", "month"):
        if rec.get(period) != keys[period]:
            rec[period] = keys[period]
            rec[f"{period}_kwh"] = 0.0

    have_meter = (energy_today_kwh is not None or energy_month_kwh is not None
                  or energy_total_kwh is not None)
    rec["source"] = "meter" if have_meter else "estimated"

    inc = 0.0
    if energy_today_kwh is not None:
        last = rec.get("meter_last")
        if last is not None:
            inc = energy_today_kwh - last
            if inc < 0:
                # Sensor reset (daily rollover or device reboot). Don't count a
                # negative; the small post-reset value is the new day's start.
                inc = 0.0
        rec["meter_last"] = energy_today_kwh

        if energy_kind == "today":
            rec["day_kwh"] = energy_today_kwh    # authoritative full-day value
        else:
            rec["day_kwh"] += inc                # 'since tracking' on day 1
        rec["week_kwh"] += inc                   # no dedicated 'week' meter exists

    rec["month_kwh"] = (energy_month_kwh if energy_month_kwh is not None
                         else rec["month_kwh"] + inc)
    rec["total_kwh"] = (energy_total_kwh if energy_total_kwh is not None
                         else rec["total_kwh"] + inc)

    if not have_meter and dt_h and 0 < dt_h < 1.0 and watts is not None:
        inc_w = (watts / 1000.0) * dt_h
        if inc_w > 0:
            rec["day_kwh"]   += inc_w
            rec["week_kwh"]  += inc_w
            rec["month_kwh"] += inc_w
            rec["total_kwh"] += inc_w
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
    agent.state["convo"]      = {}      # conversational onboarding state (stage, candidates)

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
    plugs_dirty = False

    for name, plug in plugs.items():
        power_entity = plug.get("ha_entity_power")
        watts = _read_watts(states.get(power_entity)) if power_entity else None
        if watts is not None:
            # Normalise to watts (a sensor reporting kW has power_scale 1000)
            watts *= float(plug.get("power_scale", 1.0))
        if watts is None:
            # No reading this cycle — keep last known for rule continuity but skip accounting
            watts = agent.state["last_watts"].get(name)

        # Read the device's own energy meter if it has one (kWh).
        energy_entity = plug.get("ha_entity_energy")
        energy_kwh = None
        if energy_entity:
            e_raw = _read_watts(states.get(energy_entity))   # generic float read
            if e_raw is not None:
                energy_kwh = e_raw * float(plug.get("energy_scale", 1.0))

        # Dedicated month/total meters, if this device has them. Resolved lazily
        # and cached on the plug record so a plug onboarded before this lookup
        # existed gets backfilled automatically, with no re-import needed.
        period_kwh = {}
        for kind in ("month", "total"):
            ent_key, scale_key = f"ha_entity_energy_{kind}", f"energy_scale_{kind}"
            if power_entity and plug.get(ent_key) is None and not plug.get(f"{kind}_resolved"):
                found_eid, found_scale = _find_energy_sensor(states, power_entity, kind)
                if found_eid:
                    plug[ent_key] = found_eid
                    plug[scale_key] = found_scale
                plug[f"{kind}_resolved"] = True
                plugs_dirty = True
            ent = plug.get(ent_key)
            period_kwh[kind] = None
            if ent:
                raw = _read_watts(states.get(ent))
                if raw is not None:
                    period_kwh[kind] = raw * float(plug.get(scale_key, 1.0))

        # Nothing to work with this cycle.
        if watts is None and energy_kwh is None and all(v is None for v in period_kwh.values()):
            continue

        # ── Energy + cost accounting ──────────────────────────────────────────
        last_ts = agent.state["last_poll"].get(name)
        dt_h = ((now - last_ts) / 3600.0) if last_ts else None
        _account(accum, name, now, watts, dt_h, energy_kwh, plug.get("energy_kind"),
                 energy_month_kwh=period_kwh["month"], energy_total_kwh=period_kwh["total"])
        agent.state["last_poll"][name]  = now
        if watts is not None:
            agent.state["last_watts"][name] = watts
            total_watts += watts

        rec = accum.get(name, {})
        await agent.publish(f"custom/sensors/energy/{name}/power", {
            "entity_id": power_entity,
            "watts":     round(watts, 2) if watts is not None else None,
            "kwh_today": round(rec.get("day_kwh", 0.0), 4),
            "source":    rec.get("source", "estimated"),
            "ts":        now,
        })
        await agent.publish(f"custom/sensors/energy/{name}/cost", {
            "currency":   agent.state["currency"],
            "rate":       rate,
            "cost_today": _cost(rec.get("day_kwh", 0.0), rate),
            "cost_week":  _cost(rec.get("week_kwh", 0.0), rate),
            "cost_month": _cost(rec.get("month_kwh", 0.0), rate),
            "source":     rec.get("source", "estimated"),
            "ts":         now,
        })

        summary.append({
            "plug":       name,
            "watts":      round(watts, 2) if watts is not None else None,
            "protection": plug.get("protection", LOCKED),
            "cost_today": _cost(rec.get("day_kwh", 0.0), rate),
            "source":     rec.get("source", "estimated"),
        })

        # ── Rule evaluation ───────────────────────────────────────────────────
        if watts is not None:
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
    if plugs_dirty:
        agent.persist("plugs", plugs)


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
        await _refresh_live_snapshot(agent)
        return _status(agent)
    if action in ("cost", "report"):
        await _refresh_live_snapshot(agent)
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

    # ── Free-text → conversational router ────────────────────────────────────
    text = str(payload.get("action") or payload.get("text") or "").strip()
    if text:
        return await _converse(agent, text)

    # No text at all → friendly first-contact welcome
    return {"result": _welcome(agent)}


async def _refresh_live_snapshot(agent):
    """Refresh live HA readings before answering an interactive request.

    The process loop keeps last_watts in memory, but that cache is intentionally
    not persisted. A chat request can arrive before the next poll after restart,
    so force one poll here and then answer from the same snapshot path.
    """
    if not agent.state.get("plugs"):
        return
    await process(agent)


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSATIONAL ONBOARDING — built for non-technical users
# ══════════════════════════════════════════════════════════════════════════════
#
# The whole point: a user should never have to type JSON. They say "import my
# plugs", we scan Home Assistant, show what we found with live wattage, and ask
# in plain English which to monitor. Everything is added as 'locked' — we NEVER
# turn a plug off. Auto-off is a separate, explicit conversation the user starts
# later (e.g. "turn my printer off when it's done").

def _welcome(agent) -> str:
    n = len(agent.state.get("plugs", {}))
    if n == 0:
        return (
            "Hi! I keep an eye on your smart plugs — how much power they use and "
            "what that costs. I never switch anything off on my own.\n\n"
            "Say **\"import my plugs\"** and I'll scan Home Assistant and show you "
            "what I find."
        )
    return _status(agent)["result"] + (
        "\n\nSay \"import my plugs\" to add more, or just ask me things like "
        "\"how much has the AC cost today?\""
    )


def _is_import_intent(low: str) -> bool:
    triggers = ("import", "discover", "scan", "set up", "setup", "add plug",
                "add my plug", "add a plug", "find plug", "onboard", "connect plug",
                "monitor plug", "monitor my plug", "get started", "find my plug",
                "check ha", "check home assistant", "look in ha", "search ha")
    return any(t in low for t in triggers)


# Words that mean "this message is about energy/plugs" — used to decide whether
# to proactively scan HA when nothing is set up yet. Kept broad on purpose:
# when there are zero plugs, the only useful thing to do is go find some.
_ENERGY_HINTS = (
    "plug", "power", "watt", "energy", "consum", "electric", "kwh", "draw",
    "usage", "cost", "tariff", "meter", "appliance", "device", "load", "solar",
    "home assistant", "check ha", " ha ", "fridge", "heater", "printer", "rig",
)


def _looks_energy_related(low: str) -> bool:
    return any(h in f" {low} " for h in _ENERGY_HINTS)


def _is_cancel(low: str) -> bool:
    return low.strip() in ("cancel", "never mind", "nevermind", "stop", "abort", "quit")


def _parse_rate(low: str):
    """Pull a kWh rate out of 'set rate to 0.20', 'electricity is 0.25 per kwh',
    'my tariff is 0.30'. Requires an explicit rate keyword so phrases like
    'I used 5 kwh' aren't mistaken for setting the tariff."""
    import re as _re
    if not any(k in low for k in ("rate", "tariff", "price", "per kwh", "per kw",
                                  "cost per", "charge", "/kwh")):
        return None
    m = _re.search(r"(\d+[.,]?\d*)", low)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _is_remove_intent(low: str) -> bool:
    return any(k in low for k in ("stop monitoring", "remove ", "forget ", "delete ",
                                  "unmonitor", "stop watching", "drop "))


async def _converse(agent, text: str) -> dict:
    low = text.lower().strip()
    convo = agent.state.get("convo") or {}
    has_plugs = bool(agent.state.get("plugs"))

    # ── An active selection flow takes priority ──────────────────────────────
    if convo.get("stage") == "selecting":
        if _is_cancel(low):
            agent.state["convo"] = {}
            return {"result": "No problem — stopped. Say \"import my plugs\" whenever you're ready."}
        # Let status/help mid-flow re-show the menu instead of being mistaken
        # for a plug choice.
        if low in ("status", "help", "?", "what", "huh", "list") or "what plug" in low:
            cands = convo.get("candidates", [])
            menu = "\n".join(f"  {i}. **{c['friendly']}**" for i, c in enumerate(cands, 1))
            return {"result": (
                "We're in the middle of importing. I found:\n" + menu +
                "\n\nSay \"all\", a number/name, or \"cancel\"."
            )}
        return await _handle_selection(agent, text)

    if _is_cancel(low):
        return {"result": "Nothing to cancel. Say \"import my plugs\" to begin."}

    # ── Set electricity rate (natural language) ──────────────────────────────
    rate = _parse_rate(low)
    if rate is not None:
        return _set_rate(agent, rate, None)

    # ── Stop monitoring a plug ───────────────────────────────────────────────
    if has_plugs and _is_remove_intent(low):
        return _remove_plug(agent, text)

    # ── Explicit import intent always scans ──────────────────────────────────
    if _is_import_intent(low):
        return await _start_import(agent)

    # ── Status / list (once plugs exist) ─────────────────────────────────────
    if has_plugs and any(w in low for w in ("status", "list plug", "my plug", "what plug",
                                            "which plug", "show plug", "overview", "summary")):
        await _refresh_live_snapshot(agent)
        return _status(agent)

    if "help" in low:
        return {"result": _help_text(agent)}

    # ── Proactive onboarding ─────────────────────────────────────────────────
    # If nothing is set up yet, don't make the user guess the magic words. The
    # moment they ask anything energy/plug/HA-related, just scan HA and show them.
    if not has_plugs:
        if _looks_energy_related(low):
            return await _start_import(agent)
        return {"result": _welcome(agent)}

    # Plugs exist → answer the question from live data.
    return await _ask_llm(agent, text)


def _help_text(agent) -> str:
    return (
        "Here's what I can do (just talk to me normally):\n"
        "  • **\"import my plugs\"** — scan Home Assistant and add plugs to watch\n"
        "  • **\"what's my power draw?\"** / **\"status\"** — live wattage & today's cost\n"
        "  • **\"how much has it cost today?\"** — cost breakdown (per day/week/month)\n"
        "  • **\"set rate to 0.20\"** — change your €/kWh tariff\n"
        "  • **\"stop monitoring the AC\"** — remove a plug\n"
        "\nI **only monitor** — I never switch a plug off. (Per-plug automations "
        "like auto-off are off by default and only ever set up if you specifically "
        "ask for one.)\n"
        f"Current rate: {agent.state['rate']} {agent.state['currency']}/kWh."
    )


async def _start_import(agent) -> dict:
    states = await _ha_get_states()
    if not states:
        return {"result": (
            "I couldn't reach Home Assistant to scan for plugs. Once HA is "
            "connected, say \"import my plugs\" again and I'll find them."
        )}

    candidates = _discover_candidates(states)
    # Drop plugs we're already monitoring
    existing_power = {p.get("ha_entity_power") for p in agent.state["plugs"].values()}
    candidates = [c for c in candidates if c["power_entity"] not in existing_power]

    if not candidates:
        if agent.state["plugs"]:
            return {"result": (
                "I didn't find any *new* plugs with energy monitoring. "
                + _status(agent)["result"]
            )}
        return {"result": (
            "I scanned Home Assistant but didn't find any plugs that report power "
            "usage (watts). Smart plugs like the Tapo P110 report energy; some "
            "(like a plain on/off plug) don't. If you think one should show up, "
            "check that its power sensor is enabled in Home Assistant."
        )}

    agent.state["convo"] = {"stage": "selecting", "candidates": candidates}

    lines = ["I found these plugs that report power usage:\n"]
    for i, c in enumerate(candidates, 1):
        w = c["watts"]
        wtxt = f"{w:.0f} W right now" if w is not None else "no reading yet"
        lines.append(f"  {i}. **{c['friendly']}** — {wtxt}")
    lines.append(
        "\nWhich would you like me to monitor? You can say **\"all\"**, or pick by "
        "number or name (e.g. \"1 and 3\" or \"the AC one\").\n\n"
        "_I'll only watch usage and cost — I will never turn these off._"
    )
    return {"result": "\n".join(lines)}


async def _handle_selection(agent, text: str) -> dict:
    convo = agent.state["convo"]
    candidates = convo.get("candidates", [])
    chosen = await _interpret_selection(agent, text, candidates)

    if not chosen:
        return {"result": (
            "Sorry, I didn't catch which ones. You can say \"all\", or give me "
            "numbers or names — like \"1 and 2\" or \"just the AC\". "
            "Or say \"cancel\" to stop."
        )}

    added = []
    metered = 0
    for c in chosen:
        plug = {
            "name":             c["suggested_name"],
            "friendly":         c["friendly"],
            "ha_entity_power":  c["power_entity"],
            "ha_entity_switch": c.get("switch_entity"),
            "ha_entity_energy": c.get("energy_entity"),
            "energy_scale":     c.get("energy_scale", 1.0),
            "energy_kind":      c.get("energy_kind"),
            "protection":       LOCKED,           # always safe by default
            "power_scale":      c.get("power_scale", 1.0),
            "cost_per_kwh":     agent.state["rate"],
        }
        agent.state["plugs"][plug["name"]] = plug
        added.append(c["friendly"])
        if c.get("energy_entity"):
            metered += 1

    agent.persist("plugs", agent.state["plugs"])
    agent.state["convo"] = {}   # flow complete

    names = ", ".join(added)
    # Tell the user honestly where the cost figure comes from.
    if metered == len(added):
        accuracy = ("I'll read each plug's own energy meter for cost, so "
                    "\"today\" reflects the whole day accurately.")
    elif metered == 0:
        accuracy = ("These plugs don't expose an energy meter, so I'll estimate "
                    "cost from live wattage — \"today\" counts from now, not midnight.")
    else:
        accuracy = (f"{metered} of {len(added)} expose an energy meter (accurate "
                    f"daily cost); the rest I'll estimate from live wattage.")
    return {"result": (
        f"Done! Now monitoring: **{names}**.\n\n"
        f"All set to **never turn off** — I'll only track power and cost. {accuracy} "
        f"(rate: {agent.state['rate']} {agent.state['currency']}/kWh — tell me if that's wrong).\n\n"
        f"Ask me \"how much has it cost today?\" or \"what's my power draw?\" anytime."
    )}


async def _interpret_selection(agent, text: str, candidates: list) -> list:
    """Map a free-text reply to a subset of candidates. Robust without an LLM."""
    low = text.lower().strip()
    if not candidates:
        return []

    # Fast paths
    if any(w in low for w in ("all", "every", "everything", "both", "yes please", "yeah all")):
        return list(candidates)
    if low in ("none", "no", "neither"):
        return []

    selected = []

    # Numbers: "1", "1 and 3", "2,3"
    import re as _re
    nums = [int(n) for n in _re.findall(r"\d+", low)]
    for n in nums:
        if 1 <= n <= len(candidates) and candidates[n - 1] not in selected:
            selected.append(candidates[n - 1])

    # Name/keyword substring match on friendly name words
    if not selected:
        for c in candidates:
            words = [w for w in c["friendly"].lower().replace("_", " ").split() if len(w) > 2]
            if any(w in low for w in words):
                if c not in selected:
                    selected.append(c)

    if selected:
        return selected

    # Last resort: ask the LLM to map the reply to candidate numbers
    if agent.llm is not None:
        menu = "\n".join(f"{i}. {c['friendly']}" for i, c in enumerate(candidates, 1))
        try:
            ans = await agent.llm.chat(
                f"Plugs:\n{menu}\n\nUser reply: \"{text}\"\n\n"
                f"Which plug numbers did the user choose? Reply with ONLY a JSON "
                f"array of integers, e.g. [1,3]. Use [] if unclear, or all numbers "
                f"if they meant everything.",
                system="You map a user's plain-language choice to plug numbers. Output only a JSON array.",
            )
            picks = json.loads(ans[ans.find("["): ans.rfind("]") + 1])
            for n in picks:
                if isinstance(n, int) and 1 <= n <= len(candidates):
                    if candidates[n - 1] not in selected:
                        selected.append(candidates[n - 1])
        except Exception:
            pass

    return selected


# ── HA discovery ──────────────────────────────────────────────────────────────

_POWER_SUFFIXES = (
    "_current_consumption", "_power_consumption", "_active_power", "_apparent_power",
    "_current_power", "_power", "_consumption", "_watts", "_wattage", "_load",
)

# Energy (kWh) sensor suffixes — the device's own cumulative meters. These are
# the authoritative source for cost: the plug firmware integrates continuously
# and the value survives wactorz being offline, unlike our watt-integration.
_ENERGY_SUFFIXES = (
    "_today_s_consumption", "_today_energy", "_energy_today", "_daily_energy",
    "_this_month_s_consumption", "_monthly_energy", "_month_energy",
    "_total_energy", "_energy_total", "_lifetime_energy", "_energy_kwh", "_energy",
)


def _slug(s: str) -> str:
    import re as _re
    s = _re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return s or "plug"


def _base_entity(eid: str) -> str:
    """Strip a power OR energy suffix so a device's power and energy sensors
    collapse to the same base (e.g. sensor.ac_current_consumption and
    sensor.ac_today_energy both → 'ac'), letting us pair them."""
    n = eid.split(".", 1)[1] if "." in eid else eid
    for suf in (*_ENERGY_SUFFIXES, *_POWER_SUFFIXES):
        if n.endswith(suf):
            return n[: -len(suf)]
    return n


def _energy_kind(eid: str, friendly: str) -> str:
    """Classify an energy sensor: 'today' (daily-reset), 'month', or 'total'
    (lifetime cumulative). 'today' is best for an accurate full-day figure."""
    s = (eid + " " + (friendly or "")).lower()
    if "today" in s or "daily" in s or "_day" in s or " day" in s:
        return "today"
    if "month" in s:
        return "month"
    return "total"


def _find_energy_sensor(states: dict, power_entity: str, kind: str):
    """Find a sibling energy sensor of the given kind ('month'|'total') for the
    same device as power_entity, by matching the entity's base name (the same
    pairing logic _discover_candidates uses). Lets process() backfill a plug
    that was onboarded before its dedicated month/total sensor was being read,
    with no re-import required. Returns (entity_id, scale) or (None, 1.0)."""
    base = _base_entity(power_entity)
    for eid, st in states.items():
        if not isinstance(st, dict) or not eid.startswith("sensor."):
            continue
        attrs = st.get("attributes", {}) or {}
        unit  = str(attrs.get("unit_of_measurement") or "").lower()
        dc    = str(attrs.get("device_class") or "").lower()
        if dc != "energy" and unit not in ("kwh", "wh"):
            continue
        if _base_entity(eid) != base:
            continue
        friendly = attrs.get("friendly_name") or eid
        if _energy_kind(eid, friendly) != kind:
            continue
        return eid, (0.001 if unit == "wh" else 1.0)
    return None, 1.0


def _discover_candidates(states: dict) -> list:
    """Find power-reporting plugs in HA and best-effort pair each with a switch.

    A candidate only needs a power sensor — the switch is optional (and unused
    while everything is locked). Each candidate is also paired with the device's
    own energy (kWh) sensor when one exists, so cost can come from the real
    meter rather than from integrating watts. Returns ordered candidate dicts."""
    power_sensors = []           # (entity_id, watts, scale, friendly)
    switches = {}                # base_name -> entity_id
    energy_by_base = {}          # base_name -> list of energy sensor dicts

    for eid, st in states.items():
        if not isinstance(st, dict):
            continue
        attrs = st.get("attributes", {}) or {}
        unit  = str(attrs.get("unit_of_measurement") or "").lower()
        dc    = str(attrs.get("device_class") or "").lower()

        if eid.startswith("switch."):
            switches[_base_entity(eid)] = eid
            continue

        if not eid.startswith("sensor."):
            continue

        friendly = attrs.get("friendly_name") or eid

        # Instantaneous power (W/kW)
        if dc == "power" or unit in ("w", "kw", "watt", "watts"):
            try:
                val = float(st.get("state"))
            except (TypeError, ValueError):
                val = None
            scale = 1000.0 if unit == "kw" else 1.0
            watts = val * scale if val is not None else None
            power_sensors.append((eid, watts, scale, friendly))
            continue

        # Cumulative energy (kWh/Wh) — the device's own meter
        if dc == "energy" or unit in ("kwh", "wh"):
            energy_by_base.setdefault(_base_entity(eid), []).append({
                "entity": eid,
                "kind":   _energy_kind(eid, friendly),
                "scale":  0.001 if unit == "wh" else 1.0,
            })

    def _pick_energy(base):
        """Choose the best energy sensor for a base: prefer a daily-reset
        'today' sensor (gives an accurate full-day figure directly), then a
        lifetime 'total', then 'month'."""
        cands = energy_by_base.get(base, [])
        if not cands:
            # looser prefix match
            for b, lst in energy_by_base.items():
                if b and (base.startswith(b) or b.startswith(base)):
                    cands = lst
                    break
        if not cands:
            return None
        for want in ("today", "total", "month"):
            for c in cands:
                if c["kind"] == want:
                    return c
        return cands[0]

    candidates = []
    used_names = set()
    for eid, watts, scale, friendly in power_sensors:
        base = _base_entity(eid)
        switch_eid = switches.get(base)
        if not switch_eid:
            for sb, sw in switches.items():
                if base.startswith(sb) or sb.startswith(base):
                    switch_eid = sw
                    break

        energy = _pick_energy(base)

        # Friendlier display: trim trailing "current consumption"/"power" noise.
        disp = friendly
        for noise in ("Current consumption", "Power consumption", "Power", "Consumption"):
            if disp.endswith(noise):
                disp = disp[: -len(noise)].strip(" -_") or disp
                break

        name = _slug(disp)
        n, base_name = name, name
        idx = 2
        while n in used_names:
            n = f"{base_name}_{idx}"; idx += 1
        used_names.add(n)

        candidates.append({
            "friendly":       disp,
            "suggested_name": n,
            "power_entity":   eid,
            "switch_entity":  switch_eid,
            "watts":          watts,
            "power_scale":    scale,
            "energy_entity":  energy["entity"] if energy else None,
            "energy_scale":   energy["scale"]  if energy else 1.0,
            "energy_kind":    energy["kind"]   if energy else None,
        })

    return candidates


def _src_label(rec) -> str:
    return "metered" if rec.get("source") == "meter" else "estimated"


def _status(agent) -> dict:
    plugs = agent.state["plugs"]
    rate  = agent.state["rate"]
    cur   = agent.state["currency"]
    rows  = []
    total_w = 0.0
    any_estimated = False
    for name, plug in plugs.items():
        w   = agent.state["last_watts"].get(name)
        rec = agent.state["accum"].get(name, {})
        total_w += (w or 0.0)
        src = _src_label(rec)
        if src == "estimated":
            any_estimated = True
        fr = plug.get("friendly", name)
        rows.append(
            f"  • {fr} [{plug.get('protection', LOCKED)}]: "
            f"{round(w, 1) if w is not None else '—'} W now, "
            f"today {_cost(rec.get('day_kwh', 0.0), rate)} {cur} ({src})"
        )
    text = (
        f"⚡ Smart energy — {len(plugs)} plug(s), {len(agent.state['rules'])} rule(s), "
        f"rate {rate} {cur}/kWh\n" + ("\n".join(rows) if rows else "  (no plugs yet — say \"import my plugs\")")
        + f"\n  total now: {round(total_w, 1)} W"
    )
    if any_estimated:
        text += ("\n  (estimated = no energy meter on that plug; cost is integrated "
                 "from live wattage and counts from when I started watching)")
    return {
        "result":          text,
        "plugs_monitored": len(plugs),
        "active_rules":    len(agent.state["rules"]),
        "total_watts":     round(total_w, 2),
    }


async def _report(agent, payload) -> dict:
    rate = agent.state["rate"]
    cur  = agent.state["currency"]
    plugs = agent.state["plugs"]
    lines = [f"Cost report (rate {rate} {cur}/kWh):"]
    grand = {"day": 0.0, "week": 0.0, "month": 0.0}
    any_estimated = False
    for name, rec in agent.state["accum"].items():
        d, w, m = rec.get("day_kwh", 0), rec.get("week_kwh", 0), rec.get("month_kwh", 0)
        grand["day"] += d; grand["week"] += w; grand["month"] += m
        src = _src_label(rec)
        if src == "estimated":
            any_estimated = True
        fr = plugs.get(name, {}).get("friendly", name)
        lines.append(
            f"  {fr}: today {_cost(d, rate)}{cur} ({d:.3f} kWh) | "
            f"week {_cost(w, rate)}{cur} | month {_cost(m, rate)}{cur}  [{src}]"
        )
    lines.append(
        f"  TOTAL: today {_cost(grand['day'], rate)}{cur} | "
        f"week {_cost(grand['week'], rate)}{cur} | month {_cost(grand['month'], rate)}{cur}"
    )
    if any_estimated:
        lines.append("  Note: 'estimated' plugs have no energy meter — cost is integrated "
                     "from live wattage and starts counting when monitoring began, not midnight.")
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


_REMOVE_STOPWORDS = {
    "stop", "monitoring", "monitor", "watching", "watch", "remove", "forget",
    "delete", "drop", "unmonitor", "the", "a", "an", "my", "plug", "please",
    "from", "list", "of", "for",
}


def _tokens(s: str) -> set:
    import re as _re
    return {t for t in _re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) >= 2}


def _resolve_plug_name(agent, query) -> str:
    """Find a plug key from free text by exact name/slug, else by best token
    overlap between the (de-noised) query and each plug's name+friendly."""
    if not query:
        return None
    plugs = agent.state["plugs"]
    q = str(query).strip().lower()
    if q in plugs:
        return q
    if _slug(q) in plugs:
        return _slug(q)

    q_tokens = _tokens(q) - _REMOVE_STOPWORDS
    if not q_tokens:
        return None

    best, best_score = None, 0
    for name, p in plugs.items():
        plug_tokens = (_tokens(name) | _tokens(p.get("friendly", ""))) - _REMOVE_STOPWORDS
        score = len(q_tokens & plug_tokens)
        if score > best_score:
            best, best_score = name, score
    return best


def _remove_plug(agent, name) -> dict:
    key = name if name in agent.state["plugs"] else _resolve_plug_name(agent, name)
    if key and key in agent.state["plugs"]:
        fr = agent.state["plugs"][key].get("friendly", key)
        del agent.state["plugs"][key]
        agent.state["accum"].pop(key, None)
        agent.persist("plugs", agent.state["plugs"])
        agent.persist("accum", agent.state["accum"])
        return {"result": f"Stopped monitoring **{fr}**."}
    return {"result": "error", "error": f"I'm not monitoring a plug called '{name}'."}


def _add_rule(agent, rule) -> dict:
    if not isinstance(rule, dict) or not rule.get("type"):
        return {"result": "error", "error": "rule must be a dict with a 'type'"}
    requested_plug = rule.get("plug")
    plug_name = _resolve_plug_name(agent, requested_plug)
    plug = agent.state["plugs"].get(plug_name) if plug_name else None
    if not plug:
        return {"result": "error", "error": f"rule references unknown plug '{requested_plug}'"}

    rule["plug"] = plug_name

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

    await _refresh_live_snapshot(agent)

    rate = agent.state["rate"]
    snapshot = {
        "rate_per_kwh": rate,
        "currency":     agent.state["currency"],
        "plugs": {
            name: {
                "friendly":    p.get("friendly", name),
                "watts_now":   agent.state["last_watts"].get(name),
                "protection":  p.get("protection", LOCKED),
                "kwh_today":   round(agent.state["accum"].get(name, {}).get("day_kwh", 0.0), 4),
                "kwh_month":   round(agent.state["accum"].get(name, {}).get("month_kwh", 0.0), 4),
                "cost_source": _src_label(agent.state["accum"].get(name, {})),
            }
            for name, p in agent.state["plugs"].items()
        },
        "rules": list(agent.state["rules"].values()),
    }
    system = (
        "You are the smart-energy agent for a Home Assistant setup. Answer the "
        "user's question using ONLY the JSON snapshot of live plug readings and "
        "cost accumulators provided. Costs = kWh * rate_per_kwh. Be concise and "
        "refer to plugs by their 'friendly' name. If a plug's cost_source is "
        "'estimated', note that its cost is approximate (no hardware meter) and "
        "counts only from when monitoring began, not midnight. Never suggest "
        "turning off a plug whose protection is 'locked'."
    )
    prompt = f"SNAPSHOT:\n{json.dumps(snapshot, indent=2)}\n\nQUESTION: {question}"
    try:
        answer = await agent.llm.chat(prompt, system=system)
    except Exception as e:
        return {"result": f"LLM error: {e}", "snapshot": snapshot}
    return {"result": answer, "snapshot": snapshot}
'''
