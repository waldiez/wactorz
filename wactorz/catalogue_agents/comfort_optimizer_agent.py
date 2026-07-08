"""CATALOG AGENT — comfort-optimizer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The orchestrating brain of the active-inference comfort pipeline
(docs/design/automation-advisor.md, "scenario flow" steps 3a–3d).

NOTHING about the site is hardcoded. On an `optimize` request it:

  3a. pulls recorded history from the `timeseries-collector` over the normal
      agent task channel (send_to → query action)
  3b. DISCOVERS what exists — builds a per-entity inventory purely from the
      data (domains, device classes, cadence, value ranges) — and MINES
      generic patterns in code (event co-occurrence, hour-of-day activity,
      actuator→sensor response, power duty/energy)
      → the LLM then reads inventory + patterns and decides:
        · what each entity semantically IS
        · which OPTIMIZATION OPPORTUNITIES exist (control loops, rules, gaps)
        · the entity BINDING and C preferences for the chosen control loop
  3c. designs the AIF generative-model configuration from that reasoning:
        A (observation precision) · C (preferences) · D (initial beliefs)
        fixed by design, B (transitions) warm-start rates measured IN CODE
        and KEPT LEARNABLE — the LLM never writes probability tensors
      and spawns the `aif-agent` with it (via main's spawn path)
  3d. subscription topics are declared in the spawn config so the
      planner/TopicBus can wire them (Fuseki/SPARQL in the full scenario)

The LLM is the primary analyst. Without an LLM the agent still runs in a
clearly-flagged degraded mode (device_class heuristics, thermal-only), which
also keeps the pipeline deterministic for tests.

MQTT CONTRACT
─────────────
  Subscribe: (none — pulls via agent task channel)
  Publish:   custom/optimizer/analysis — inventory + patterns + opportunities

SPAWN CONFIG
────────────
{
  "name":        "comfort-optimizer",
  "type":        "dynamic",
  "description": "Discovers devices from collected data, finds user patterns with an LLM, proposes optimizations, and commissions a tuned active-inference controller.",
  "capabilities": ["comfort_optimization", "pattern_analysis", "model_design",
                   "active_inference", "commissioning", "device_discovery"],
  "input_schema": {
    "action":       "str — optimize|analyze|status",
    "goal":         "str — free-text user goal, e.g. 'save energy', 'comfort first'",
    "hours":        "float — history window to analyze (default 24)",
    "dry_run":      "bool — analyze + design but do NOT spawn (approval flow)",
    "opportunity":  "int|str — pick a specific proposed opportunity (index or title)",
    "comfort_when": "str — occupied|unoccupied; overrides the inferred preference",
    "comfort_band": "[lo, hi] — °C; overrides the inferred band",
    "aif_name":     "str — name for the spawned controller (default 'aif-ac')",
    "node":         "str — node to spawn the aif-agent on (default: local)",
    "tick_seconds": "int — control tick (default 900)",
    "propose_every_min": "int — proposal cadence k (default 15)"
  },
  "output_schema": {
    "inventory":     "dict — discovered entities and their data profiles",
    "patterns":      "dict — mined correlations, activity, responses, energy",
    "opportunities": "list — LLM-proposed optimizations (ranked)",
    "aif_config":    "dict — the designed spawn config (A/B/C/D + binding)",
    "spawned":       "bool",
    "llm_used":      "bool",
    "result":        "str — human-readable report"
  },
  "poll_interval": 3600
}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

AGENT_CODE = r'''
import asyncio
import json
import time

COLLECTOR_NAME      = "timeseries-collector"
DEFAULT_HOURS       = 24.0
DEFAULT_TICK_S      = 900
DEFAULT_PROPOSE_MIN = 15
ACTIVE_POWER_W      = 50.0
CO_OCCUR_LAG_S      = 120.0

# States that make an entity "event-like" for pattern mining
_BINARYISH = {"on", "off", "locked", "unlocked", "open", "closed", "home",
              "not_home", "cool", "heat", "heat_cool", "dry", "fan_only",
              "idle", "auto", "true", "false", "detected", "clear"}
# Per-domain default meaning of "occupied" (LLM can override in the binding)
_OCCUPIED_DEFAULT = {"lock": "unlocked", "binary_sensor": "on",
                     "device_tracker": "home", "person": "home"}


def _is_num(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _attr(row):
    try:
        a = row.get("attributes")
        return json.loads(a) if isinstance(a, str) else (a or {})
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# 3a — DATA FETCH
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_history(agent, hours):
    res = await agent.send_to(COLLECTOR_NAME, {
        "action": "query", "table": "ha_states", "hours": hours, "limit": 5000,
    })
    if not isinstance(res, dict) or res.get("error"):
        raise RuntimeError(f"collector query failed: {res}")
    return res.get("rows") or []


# ══════════════════════════════════════════════════════════════════════════════
# 3b.1 — INVENTORY: discover what exists, purely from the data
# ══════════════════════════════════════════════════════════════════════════════

def _inventory(rows):
    per = {}
    for r in rows:
        per.setdefault(r["entity_id"], []).append(r)
    inv = {}
    for eid, ers in per.items():
        ers.sort(key=lambda r: r["ts"])
        states = [r["new_state"] for r in ers]
        nums = [float(s) for s in states if _is_num(s)]
        dclass = unit = ""
        for r in ers[:8]:
            a = _attr(r)
            dclass = a.get("device_class") or dclass
            unit = a.get("unit_of_measurement") or unit
        gaps = [(b["ts"] - a["ts"]) / 60 for a, b in zip(ers, ers[1:])]
        low_states = {s.lower() for s in states if not _is_num(s)}
        inv[eid] = {
            "domain": ers[0].get("domain") or eid.split(".")[0],
            "device_class": dclass, "unit": unit,
            "events": len(ers),
            "median_gap_min": round(sorted(gaps)[len(gaps) // 2], 1) if gaps else None,
            "numeric": bool(nums) and len(nums) >= max(3, len(states) // 2),
            "range": [round(min(nums), 2), round(max(nums), 2)] if nums else None,
            "states": sorted(low_states)[:6] if low_states else None,
            "eventlike": bool(low_states & _BINARYISH),
        }
    return inv, per


# ══════════════════════════════════════════════════════════════════════════════
# 3b.2 — GENERIC PATTERN MINING (code; nothing device-specific)
# ══════════════════════════════════════════════════════════════════════════════

def _active_windows(per, eid, inv):
    """Time windows in which an entity is 'active': numeric>threshold for
    power-like entities, else any state change burst membership."""
    ers = sorted(per.get(eid, []), key=lambda r: r["ts"])
    info = inv.get(eid, {})
    wins, start, last = [], None, None
    if info.get("numeric"):
        thr = ACTIVE_POWER_W if (info.get("device_class") == "power"
                                 or info.get("unit") == "W") else None
        if thr is None and info.get("range"):
            lo, hi = info["range"]
            thr = lo + 0.5 * (hi - lo)
        for r in ers:
            if not _is_num(r["new_state"]):
                continue
            if float(r["new_state"]) > thr:
                if start is None:
                    start = r["ts"]
                last = r["ts"]
            elif start is not None and r["ts"] - last > 600:
                wins.append((start, last)); start = None
    else:
        on_states = {"on", "cool", "heat", "heat_cool", "dry", "fan_only",
                     "open", "unlocked", "home", "detected", "true"}
        for r in ers:
            if str(r["new_state"]).lower() in on_states:
                if start is None:
                    start = r["ts"]
                last = r["ts"]
            else:
                if start is not None:
                    wins.append((start, r["ts"])); start = None
    if start is not None:
        wins.append((start, last))
    return [(a, b) for a, b in wins if b and b > a]


def _mine_patterns(rows, inv, per):
    now = max((r["ts"] for r in rows), default=time.time())
    t0 = min((r["ts"] for r in rows), default=now)
    span_h = max((now - t0) / 3600.0, 0.01)
    out = {"span_hours": round(span_h, 1)}

    event_entities = [e for e, i in inv.items() if i["eventlike"] and i["events"] >= 3]
    numeric_entities = [e for e, i in inv.items() if i["numeric"]]

    # ── lagged co-occurrence between event-like entities ────────────────────
    pairs = []
    for a in event_entities:
        a_ts = [r["ts"] for r in per[a]]
        for b in event_entities:
            if a == b:
                continue
            b_ts = [r["ts"] for r in per[b]]
            hits = sum(1 for t in a_ts if any(0 <= u - t <= CO_OCCUR_LAG_S for u in b_ts))
            if len(a_ts) >= 5 and hits / len(a_ts) >= 0.6:
                pairs.append({"when": a, "then": b, "n": len(a_ts),
                              "confidence": round(hits / len(a_ts), 2)})
    out["co_occurrence"] = sorted(pairs, key=lambda p: -p["confidence"])[:12]

    # ── hour-of-day activity per event-like entity ──────────────────────────
    activity = {}
    for e in event_entities:
        hours = {}
        for r in per[e]:
            h = time.localtime(r["ts"]).tm_hour
            hours[h] = hours.get(h, 0) + 1
        top = sorted(hours.items(), key=lambda kv: -kv[1])[:4]
        activity[e] = [h for h, _ in top]
    out["active_hours"] = activity

    # ── actuator→sensor response: numeric rate inside vs outside windows ───
    responses = []
    actuatorish = [e for e in inv
                   if inv[e]["domain"] in ("climate", "switch", "light", "fan",
                                           "humidifier", "cover", "valve")
                   or inv[e].get("device_class") == "power"
                   or inv[e].get("unit") == "W"]
    for act in actuatorish:
        wins = _active_windows(per, act, inv)
        if not wins:
            continue
        active_s = sum(b - a for a, b in wins)
        for sensor in numeric_entities:
            if sensor == act:
                continue
            pts = [(r["ts"], float(r["new_state"])) for r in per[sensor]
                   if _is_num(r["new_state"])]
            rates_in, rates_out = [], []
            for (t1, v1), (t2, v2) in zip(pts, pts[1:]):
                dt_h = (t2 - t1) / 3600.0
                if not 0.02 < dt_h < 4.0:
                    continue
                rate = (v2 - v1) / dt_h
                inside = any(a <= t1 <= b for a, b in wins) and \
                         any(a <= t2 <= b for a, b in wins)
                (rates_in if inside else rates_out).append(rate)
            if len(rates_in) >= 3 and len(rates_out) >= 3:
                mi = sum(rates_in) / len(rates_in)
                mo = sum(rates_out) / len(rates_out)
                if abs(mi - mo) > 1e-3:
                    responses.append({
                        "actuator": act, "sensor": sensor,
                        "rate_active": round(mi, 3), "rate_inactive": round(mo, 3),
                        "n": len(rates_in),
                        "active_fraction": round(active_s / (now - t0), 3),
                    })
    out["responses"] = responses[:12]

    # ── power/energy for W-metered entities ─────────────────────────────────
    energy = {}
    for e in numeric_entities:
        if inv[e].get("device_class") == "power" or inv[e].get("unit") == "W":
            pts = [(r["ts"], float(r["new_state"])) for r in per[e]
                   if _is_num(r["new_state"])]
            kwh = sum(w * (pts[i + 1][0] - t) / 3.6e6
                      for i, (t, w) in enumerate(pts[:-1]))
            wins = _active_windows(per, e, inv)
            energy[e] = {"kwh": round(kwh, 2), "sessions": len(wins),
                         "duty": round(sum(b - a for a, b in wins) / (now - t0), 3)}
    out["energy"] = energy

    # ── state overlap: fraction of A's active time during which B is active ──
    # This is the generic "revealed preference" signal (e.g. AC runs while the
    # door is locked ⇒ cooling-while-empty), with no device knowledge baked in.
    def _win_overlap(wa, wb):
        total = sum(b - a for a, b in wa)
        if not total:
            return None
        inter = 0.0
        for a1, b1 in wa:
            for a2, b2 in wb:
                inter += max(0.0, min(b1, b2) - max(a1, a2))
        return round(inter / total, 3)

    windowed = {}
    for e in (event_entities + list(energy))[:8]:
        w = _active_windows(per, e, inv)
        if w:
            windowed[e] = w
    overlaps = []
    ents = list(windowed)
    for i, a in enumerate(ents):
        for b in ents[i + 1:]:
            ov = _win_overlap(windowed[a], windowed[b])
            if ov is not None:
                overlaps.append({"a_active": a, "b_active": b, "overlap": ov})
    out["state_overlap"] = sorted(overlaps, key=lambda o: -o["overlap"])[:12]
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 3b.3 — LLM ANALYSIS: semantics + opportunities + binding + C (one call)
# ══════════════════════════════════════════════════════════════════════════════

_LLM_SYSTEM = (
    "You are a building-automation analyst commissioning adaptive controllers. "
    "You receive a device inventory (discovered from data, not configured) and "
    "mined behavior patterns. Reply with ONLY a JSON object, no prose."
)

def _llm_prompt(inv, patterns, goal, hints):
    schema = {
        "semantics": {"<entity_id>": "short label, e.g. 'room temperature sensor'"},
        "opportunities": [{
            "title": "str", "kind": "aif_control | rule | data_gap",
            "rationale": "str, cite the patterns that justify it",
            "confidence": "0..1",
            "binding": {
                "observations": ["entity ids the controller should observe"],
                "primary_observation": "the numeric sensor the controller regulates",
                "occupancy": [{"entity": "id", "occupied_state": "state meaning occupied"}],
                "actuator": {"entity": "id", "domain": "ha domain",
                             "service": "ha service", "temp_key": "service data key or null"},
                "feedback": "power/verification entity id or null"
            },
            "C": {"comfort_when": "occupied|unoccupied", "comfort_band": [0, 0],
                  "comfort_weight": 1.0, "energy_weight": 0.5}
        }],
    }
    return (
        f"USER GOAL: {goal or 'balance comfort and energy'}\n"
        f"{hints}"
        f"\nDEVICE INVENTORY (discovered from data):\n{json.dumps(inv)}\n"
        f"\nMINED PATTERNS:\n{json.dumps(patterns)}\n"
        "\nTasks: (1) label every entity; (2) list optimization opportunities "
        "ranked by value — include control loops (kind=aif_control) where a "
        "controllable actuator influences an observed quantity, simple rules, "
        "and data gaps; (3) for each aif_control opportunity fill the binding "
        "and C preferences. Infer preferences from REVEALED behavior in the "
        "patterns (e.g. if the actuator runs mostly while the space is empty, "
        "comfort_when='unoccupied' and energy_weight should be lower). "
        f"Reply as JSON with exactly this shape:\n{json.dumps(schema)}"
    )


def _json_from(text):
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


async def _llm_analyze(agent, inv, patterns, goal, overrides):
    """LLM pass. Returns (semantics, opportunities, llm_used)."""
    if agent.llm is not None:
        hints = ""
        for k in ("comfort_when", "comfort_band"):
            if overrides.get(k) is not None:
                hints += f"USER CONSTRAINT: {k} = {overrides[k]}\n"
        try:
            raw = await agent.llm.chat(_llm_prompt(inv, patterns, goal, hints),
                                       system=_LLM_SYSTEM)
            parsed = _json_from(raw)
            opps = parsed.get("opportunities") or []
            if opps:
                return parsed.get("semantics") or {}, opps, True
        except Exception as exc:
            await agent.log(f"LLM analysis failed ({exc}) — degraded heuristics mode",
                            level="warning")

    # ── degraded fallback: thermal loop from device classes, clearly flagged ──
    sem, opp = {}, []
    temp = next((e for e, i in inv.items()
                 if i.get("device_class") == "temperature"
                 or ("temp" in e and i["numeric"])), None)
    clim = next((e for e, i in inv.items() if i["domain"] == "climate"), None)
    power = next((e for e, i in inv.items()
                  if i.get("device_class") == "power" or i.get("unit") == "W"), None)
    occ = [e for e, i in inv.items()
           if i["domain"] in ("lock", "device_tracker", "person")
           or i.get("device_class") in ("motion", "occupancy")]
    if temp and clim:
        cool_empty = None
        for r in (patterns.get("responses") or []):
            if r["sensor"] == temp:
                cool_empty = r
                break
        opp = [{
            "title": "Adaptive thermal control (heuristic fallback)",
            "kind": "aif_control", "confidence": 0.5,
            "rationale": "climate + temperature entities present (no LLM available)",
            "binding": {
                "observations": [temp] + ([power] if power else []),
                "primary_observation": temp,
                "occupancy": [{"entity": e,
                               "occupied_state": _OCCUPIED_DEFAULT.get(
                                   inv[e]["domain"], "on")} for e in occ],
                "actuator": {"entity": clim, "domain": "climate",
                             "service": "set_temperature", "temp_key": "temperature"},
                "feedback": power,
            },
            "C": {"comfort_when": "occupied",
                  "comfort_band": list(inv[temp]["range"] or [23.0, 26.0]),
                  "comfort_weight": 1.0, "energy_weight": 0.5},
        }]
    return sem, opp, False


# ══════════════════════════════════════════════════════════════════════════════
# Binding validation + warm-start measurement (code — never the LLM)
# ══════════════════════════════════════════════════════════════════════════════

def _validate_binding(binding, inv):
    problems = []
    if not binding:
        return ["opportunity has no binding"]
    prim = binding.get("primary_observation")
    if not prim or prim not in inv or not inv[prim]["numeric"]:
        problems.append(f"primary_observation invalid: {prim}")
    act = (binding.get("actuator") or {}).get("entity")
    if not act or act not in inv:
        problems.append(f"actuator invalid: {act}")
    for o in binding.get("observations") or []:
        if o not in inv:
            problems.append(f"unknown observation entity: {o}")
    for o in binding.get("occupancy") or []:
        if o.get("entity") not in inv:
            problems.append(f"unknown occupancy entity: {o.get('entity')}")
    return problems


def _warm_start(binding, patterns):
    """Measured response rates for the chosen loop (from mined responses)."""
    prim = binding.get("primary_observation")
    act = (binding.get("actuator") or {}).get("entity")
    fb = binding.get("feedback")
    best = None
    for r in patterns.get("responses") or []:
        if r["sensor"] == prim and r["actuator"] in (act, fb):
            if best is None or r["n"] > best["n"]:
                best = r
    if not best:
        return {"rate_active_c_per_h": None, "rate_inactive_c_per_h": None}
    return {"rate_active_c_per_h": best["rate_active"],
            "rate_inactive_c_per_h": best["rate_inactive"]}


def _clamp_C(c, overrides, inv, binding):
    """Sanity-clamp the preference block. The band is clamped against the
    PRIMARY OBSERVATION'S observed range (the loop may regulate any quantity —
    °C, watts, lux…), never against a fixed thermal window. lo < hi is
    guaranteed; a degenerate band falls back to the observed inter-quartile-ish
    middle of the sensor's range."""
    c = dict(c or {})
    for k in ("comfort_when", "comfort_band"):
        if overrides.get(k) is not None:
            c[k] = overrides[k]
    c.setdefault("comfort_when", "occupied")

    prim = binding.get("primary_observation")
    obs_range = inv.get(prim, {}).get("range")
    band = c.get("comfort_band")
    try:
        lo, hi = float(band[0]), float(band[1])
        if lo > hi:
            lo, hi = hi, lo
    except (TypeError, ValueError, IndexError):
        lo = hi = None

    if obs_range:
        rlo, rhi = obs_range
        margin = max((rhi - rlo) * 0.5, 1.0)
        if lo is None or hi <= rlo - margin or lo >= rhi + margin:
            # missing or entirely outside plausible territory → derive from data
            span = rhi - rlo
            lo, hi = rlo + 0.25 * span, rhi - 0.25 * span
        lo = max(lo, rlo - margin)
        hi = min(hi, rhi + margin)
    elif lo is None:
        lo, hi = 23.0, 26.0  # nothing observed and nothing given — last resort

    if hi - lo < 1e-6:
        hi = lo + 1.0
    c["comfort_band"] = [round(lo, 2), round(hi, 2)]
    c["comfort_weight"] = min(max(float(c.get("comfort_weight", 1.0)), 0.25), 3.0)
    c["energy_weight"] = min(max(float(c.get("energy_weight", 0.5)), 0.05), 2.0)
    return c


# ══════════════════════════════════════════════════════════════════════════════
# 3c — AIF config design + spawn
# ══════════════════════════════════════════════════════════════════════════════

def _design_aif_config(binding, c_block, warm, inv, payload):
    prim = binding["primary_observation"]
    rng = inv.get(prim, {}).get("range") or [23.0, 25.0]
    cfg = {
        "name": payload.get("aif_name") or "aif-ac",
        "type": "dynamic",
        "catalog": "aif-agent",
        "description": "Active-inference controller (proposes actuations + explanations).",
        "binding": binding,                       # fully LLM/data-derived
        "subscribes": ["homeassistant/state_changes/#"],   # 3d — planner wires
        "publishes": ["custom/aif_ac/suggestion", "custom/sensors/aif_ac/decision"],
        "tick_seconds": int(payload.get("tick_seconds") or DEFAULT_TICK_S),
        "propose_every_min": int(payload.get("propose_every_min") or DEFAULT_PROPOSE_MIN),
        "model": {
            "A": {"obs_noise": 0.85},
            "B": {"learnable": True, "lr_pB": 1.0, "policy_len": 4,
                  "warm_start": warm},
            "C": c_block,
            "D": {"initial_value": rng[-1], "value_range": list(rng)},
        },
    }
    # only include 'node' when actually set — main's remote-spawn branch treats
    # the mere presence of the key as intent (and None would crash .strip())
    node = payload.get("node")
    if node:
        cfg["node"] = str(node)
    return cfg


async def _spawn_aif(agent, config):
    registry = agent._actor._registry
    main = registry.find_by_name("main") if registry else None
    if main is not None and hasattr(main, "_spawn_from_config"):
        spawn_cfg = dict(config)
        try:
            from wactorz.catalogue_agents.active_inference_ac_agent import AGENT_CODE as AIF_CODE
            spawn_cfg["code"] = AIF_CODE
            spawn_cfg["trusted"] = True
            # Custom keys don't reach the spawned agent's recall() on their own:
            # ship the whole design through the _initial_state snapshot, which
            # SpawnMixin persists into the agent's store BEFORE it starts.
            spawn_cfg["_initial_state"] = {"aif_config": config}
        except ImportError:
            return {"spawned": False,
                    "error": "aif-agent catalogue recipe not installed "
                             "(wactorz.catalogue_agents.active_inference_ac_agent)"}
        actor = await main._spawn_from_config(spawn_cfg)
        return {"spawned": actor is not None,
                "actor": getattr(actor, "name", None) or config["name"]}
    res = await agent.send_to("main", {
        "action": "spawn", "config": config,
        "text": "spawn the aif-agent from the catalog with the attached config",
    })
    return {"spawned": not (isinstance(res, dict) and res.get("error")), "via": "main-task"}


# ══════════════════════════════════════════════════════════════════════════════
# LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

async def setup(agent):
    agent.state["last_report"] = None
    agent.state["last_config"] = None
    agent.state["last_spawn"] = None
    agent.declare_contract(publishes=["custom/optimizer/analysis"], subscribes=[])
    await agent.log("comfort-optimizer ready — send {'action': 'optimize'} "
                    "(optionally with a 'goal').")


async def process(agent):
    pass  # request-driven


# ══════════════════════════════════════════════════════════════════════════════
# handle_task
# ══════════════════════════════════════════════════════════════════════════════

def _pick_opportunity(opps, selector):
    controls = [o for o in opps if o.get("kind") == "aif_control"]
    if selector is None:
        return controls[0] if controls else None
    if isinstance(selector, int) or str(selector).isdigit():
        i = int(selector)
        return opps[i] if 0 <= i < len(opps) else None
    sel = str(selector).lower()
    return next((o for o in opps if sel in str(o.get("title", "")).lower()), None)


async def handle_task(agent, payload):
    if isinstance(payload, dict) and not payload.get("action") and payload.get("text"):
        try:
            parsed = json.loads(payload["text"])
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            pass
    action = str(payload.get("action") or payload.get("text") or "").strip().lower()

    if action == "status":
        return {"result": json.dumps(agent.state["last_report"] or {})[:600],
                "report": agent.state["last_report"],
                "aif_config": agent.state["last_config"]}

    if action in ("optimize", "analyze") or "optimi" in action or "analy" in action:
        dry = bool(payload.get("dry_run")) or action == "analyze" or "analy" in action
        hours = float(payload.get("hours") or DEFAULT_HOURS)

        # 3a — fetch
        try:
            rows = await _fetch_history(agent, hours)
        except Exception as exc:
            return {"result": f"Could not fetch data: {exc}", "spawned": False}
        if len(rows) < 10:
            return {"result": f"Only {len(rows)} row(s) in the last {hours:g} h — "
                              "not enough data. Let the collector run longer.",
                    "spawned": False}

        # 3b — discover + mine + LLM
        inv, per = _inventory(rows)
        patterns = _mine_patterns(rows, inv, per)
        semantics, opps, llm_used = await _llm_analyze(
            agent, inv, patterns, payload.get("goal"), payload)

        report = {"inventory": inv, "patterns": patterns,
                  "semantics": semantics, "opportunities": opps,
                  "llm_used": llm_used}
        agent.state["last_report"] = report
        await agent.publish("custom/optimizer/analysis", report)

        if not opps:
            return {"result": f"Discovered {len(inv)} entities but found no "
                              "actionable optimization opportunity.",
                    **report, "spawned": False}

        chosen = _pick_opportunity(opps, payload.get("opportunity"))
        opp_lines = "; ".join(
            f"[{i}] {o.get('title')} ({o.get('kind')}, conf={o.get('confidence')})"
            for i, o in enumerate(opps))
        if chosen is None or chosen.get("kind") != "aif_control":
            return {"result": f"Opportunities found — {opp_lines}. None selected "
                              "for control (pass 'opportunity' to pick one).",
                    **report, "spawned": False}

        binding = chosen.get("binding") or {}
        problems = _validate_binding(binding, inv)
        if problems:
            return {"result": f"Chosen opportunity '{chosen.get('title')}' has an "
                              f"invalid binding: {problems}",
                    **report, "spawned": False}

        # 3c — warm-start measured in code; C clamped; config designed
        warm = _warm_start(binding, patterns)
        c_block = _clamp_C(chosen.get("C"), payload, inv, binding)
        config = _design_aif_config(binding, c_block, warm, inv, payload)
        agent.state["last_config"] = config

        summary = (
            f"Discovered {len(inv)} entities; {'LLM' if llm_used else 'heuristic'} "
            f"analysis proposed {len(opps)} opportunit"
            f"{'y' if len(opps) == 1 else 'ies'} — {opp_lines}. "
            f"Selected '{chosen.get('title')}': regulate "
            f"{binding['primary_observation']} via "
            f"{binding['actuator']['entity']}, comfort_when="
            f"{c_block['comfort_when']}, band={c_block['comfort_band']}, "
            f"warm_start={warm}. B stays learnable."
        )

        if dry:
            return {"result": summary + " (dry run — config ready, not spawned)",
                    **report, "aif_config": config, "spawned": False}

        spawn_res = await _spawn_aif(agent, config)
        agent.state["last_spawn"] = spawn_res
        ok = spawn_res.get("spawned")
        return {"result": summary + (f" Spawned '{config['name']}'."
                                     if ok else f" SPAWN FAILED: {spawn_res.get('error')}"),
                **report, "aif_config": config, "spawned": bool(ok)}

    return {
        "result": "Actions: optimize (discover → analyze → design → spawn), "
                  "analyze / dry_run=true (no spawn), status. "
                  "Optional: goal='save energy', opportunity=<index|title>.",
        "commands": ["optimize", "analyze", "status"],
    }
'''
