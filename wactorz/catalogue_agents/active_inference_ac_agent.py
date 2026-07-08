"""
CATALOG AGENT — aif-agent
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Generic active-inference controller, commissioned by the `comfort-optimizer`
(docs/design/automation-advisor.md — scenario step 4).

FLEXIBLE BY CONSTRUCTION — nothing about the regulated quantity is hardcoded:
  · the discrete state grid is built from the PRIMARY OBSERVATION'S observed
    value range (works for °C, watts, lux, %, …)
  · the action set derives from the actuator's HA domain (climate → setpoints,
    light/switch → off/levels) and can be overridden entirely via
    `model.actions` in the config
  · occupancy semantics come from the binding ({entity, occupied_state}), and
    the C preference block decides when comfort applies (occupied|unoccupied)

Generative model (pure Python — no torch; runs on a Pi):
  A — observation likelihood (Gaussian over grid bins, `model.A.obs_noise`)
  B — per-action transition kernels; prior drift from the optimizer's MEASURED
      warm-start rates, refined online by Dirichlet counts (`model.B.learnable`)
  C — preference utility over the grid (band, weights, comfort_when)
  D — initial belief (`model.D.initial_value`)
Planning minimizes expected free energy: comfort (pragmatic) − energy cost
+ curiosity (epistemic bonus for untried actions).

PROPOSAL-ONLY (human-on-the-loop): the agent NEVER calls HA services itself.
Every k minutes (or when its mind changes) it publishes the proposed actuation
TOGETHER WITH its explanation on the suggestion topic. A downstream
deterministic actuator (scenario step 5) — or the user — executes it.

MQTT CONTRACT
─────────────
  Subscribe: homeassistant/state_changes/#   (or config `subscribes`)
  Publish:   custom/{name}/suggestion        — action + full explanation (XAI)
             custom/sensors/{name}/decision  — flattened numeric decision
                                               record (collector-ingestable)

SPAWN CONFIG (delivered by the comfort-optimizer via _initial_state.aif_config)
────────────
{
  "name": "aif-ac", "type": "dynamic",
  "binding": {
    "observations": ["sensor.x", ...],
    "primary_observation": "sensor.x",
    "occupancy": [{"entity": "lock.y", "occupied_state": "unlocked"}],
    "actuator": {"entity": "climate.z", "domain": "climate",
                 "service": "set_temperature", "temp_key": "temperature"},
    "feedback": "sensor.power or null"
  },
  "model": {
    "A": {"obs_noise": 0.85},
    "B": {"learnable": true, "lr_pB": 1.0, "policy_len": 4,
          "warm_start": {"rate_active_c_per_h": -0.3, "rate_inactive_c_per_h": 0.05}},
    "C": {"comfort_when": "unoccupied", "comfort_band": [23.0, 24.5],
          "comfort_weight": 1.0, "energy_weight": 0.2},
    "D": {"initial_value": 24.5, "value_range": [22.2, 25.6]},
    "actions": null   // optional full override: [{label, intensity, call:{...}}]
  },
  "tick_seconds": 900, "propose_every_min": 15
}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

AGENT_CODE = r'''
import asyncio
import json
import math
import time

N_BINS          = 15
HORIZON         = 3
PRIOR_STRENGTH  = 5.0
EPISTEMIC_W     = 0.3
STALE_FACTOR    = 3       # obs older than STALE_FACTOR*tick ⇒ predict-only


# ══════════════════════════════════════════════════════════════════════════════
# GENERATIVE MODEL — generic 1-D grid, pure Python
# ══════════════════════════════════════════════════════════════════════════════

def _make_grid(value_range, band):
    """State grid spanning the observed range (+margin) AND the comfort band —
    unit-agnostic: whatever quantity the loop regulates."""
    los = [v for v in [value_range[0] if value_range else None,
                       band[0] if band else None] if v is not None]
    his = [v for v in [value_range[1] if value_range else None,
                       band[1] if band else None] if v is not None]
    lo, hi = min(los), max(his)
    span = max(hi - lo, 1.0)
    lo, hi = lo - 0.25 * span, hi + 0.25 * span
    step = (hi - lo) / (N_BINS - 1)
    return [lo + i * step for i in range(N_BINS)]


def _bin_of(grid, v):
    return min(range(len(grid)), key=lambda i: abs(grid[i] - v))


def _gauss_row(grid, center, sigma):
    row = [math.exp(-0.5 * ((g - center) / max(sigma, 1e-6)) ** 2) for g in grid]
    s = sum(row)
    return [x / s for x in row]


def _build_A(grid, obs_noise):
    """A[obs_bin][state_bin] — likelihood of observing bin o in state s."""
    step = grid[1] - grid[0]
    sigma = step * (1.5 - min(max(obs_noise, 0.0), 1.0))  # noise 1.0 → sharp
    return [_gauss_row(grid, g, max(sigma, step * 0.35)) for g in grid]


def _prior_kernel(grid, rate_per_h, dt_h):
    """B prior for one action: each bin drifts by rate*dt, Gaussian spread."""
    step = grid[1] - grid[0]
    shift = (rate_per_h or 0.0) * dt_h
    return [_gauss_row(grid, g + shift, step) for g in grid]


def _default_actions(binding, band):
    """Action set from the actuator's HA domain. Overridable via model.actions.
    intensity ∈ [0,1] is the generic 'how hard the actuator pushes' knob."""
    act = binding.get("actuator") or {}
    entity, domain = act.get("entity"), (act.get("domain") or "").lower()
    service, temp_key = act.get("service"), act.get("temp_key")
    actions = [{"label": "off", "intensity": 0.0,
                "call": {"entity": entity, "domain": domain,
                         "service": "turn_off" if domain != "climate" else "set_hvac_mode",
                         "service_data": {} if domain != "climate" else {"hvac_mode": "off"}}}]
    if domain == "climate" and temp_key:
        lo, hi = band
        span = max(hi - lo, 1.0)
        for i, frac in enumerate((0.33, 0.66, 1.0), 1):
            target = round(hi + 1 - frac * (span + 2), 1)
            actions.append({"label": f"set {target}", "intensity": frac,
                            "call": {"entity": entity, "domain": domain,
                                     "service": service or "set_temperature",
                                     "service_data": {temp_key: target}}})
    elif domain == "light":
        for frac in (0.33, 0.66, 1.0):
            actions.append({"label": f"on {int(frac*100)}%", "intensity": frac,
                            "call": {"entity": entity, "domain": domain,
                                     "service": "turn_on",
                                     "service_data": {"brightness_pct": int(frac * 100)}}})
    else:  # switch / fan / generic on-off
        actions.append({"label": "on", "intensity": 1.0,
                        "call": {"entity": entity, "domain": domain,
                                 "service": service or "turn_on", "service_data": {}}})
    return actions


def _utility(grid, c, comfort_active):
    """C — preference utility per grid bin."""
    lo, hi = c["comfort_band"]
    mid, half = (lo + hi) / 2, max((hi - lo) / 2, 1e-6)
    setback = (hi - lo)  # relaxed margin when comfort is inactive
    out = []
    for g in grid:
        if comfort_active:
            if lo <= g <= hi:
                out.append(c["comfort_weight"] * (1.0 - 0.5 * abs(g - mid) / half))
            else:
                over = (lo - g) if g < lo else (g - hi)
                out.append(-c["comfort_weight"] * (over / half) ** 2)
        else:
            over = max(lo - setback - g, 0.0, g - (hi + setback))
            out.append(-0.2 * c["comfort_weight"] * (over / half) ** 2)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# STATE INIT (separated from setup() so it is directly testable)
# ══════════════════════════════════════════════════════════════════════════════

def _init_state(agent, cfg):
    s = agent.state
    s["cfg"] = cfg
    binding = cfg.get("binding") or {}
    model = cfg.get("model") or {}
    c = dict(model.get("C") or {})
    c.setdefault("comfort_when", "occupied")
    c.setdefault("comfort_band", [23.0, 26.0])
    c.setdefault("comfort_weight", 1.0)
    c.setdefault("energy_weight", 0.5)
    d = model.get("D") or {}
    b = model.get("B") or {}
    ws = b.get("warm_start") or {}

    s["binding"] = binding
    s["C"] = c
    s["tick_s"] = int(cfg.get("tick_seconds") or 900)
    s["propose_s"] = int(cfg.get("propose_every_min") or 15) * 60
    s["grid"] = _make_grid(d.get("value_range"), c["comfort_band"])
    s["A"] = _build_A(s["grid"], float((model.get("A") or {}).get("obs_noise", 0.85)))
    s["learnable"] = bool(b.get("learnable", True))

    dt_h = s["tick_s"] / 3600.0
    drift = ws.get("rate_inactive_c_per_h") or 0.0
    active = ws.get("rate_active_c_per_h")
    s["actions"] = model.get("actions") or _default_actions(binding, c["comfort_band"])
    s["B_prior"] = []
    for a in s["actions"]:
        rate = drift if active is None else drift + a["intensity"] * (active - drift)
        s["B_prior"].append(_prior_kernel(s["grid"], rate, dt_h))
    s["B_counts"] = agent.recall("b_counts") or [
        [[0.0] * N_BINS for _ in range(N_BINS)] for _ in s["actions"]]
    s["action_tries"] = [sum(map(sum, m)) for m in s["B_counts"]]

    init = d.get("initial_value")
    s["belief"] = (_gauss_row(s["grid"], init, (s["grid"][1] - s["grid"][0]) * 1.5)
                   if init is not None else [1.0 / N_BINS] * N_BINS)
    s["entities"] = {}          # entity_id → (ts, state)
    s["prev_obs_bin"] = None
    s["prev_action"] = 0
    s["last_proposal"] = None
    s["last_proposal_ts"] = 0.0
    s["last_decision"] = None
    # topics — from config publishes if given, else name-derived defaults
    pubs = cfg.get("publishes") or []
    s["suggestion_topic"] = next((t for t in pubs if "suggestion" in t),
                                 f"custom/{agent.name}/suggestion")
    s["decision_topic"] = next((t for t in pubs if "decision" in t),
                               f"custom/sensors/{agent.name}/decision")


# ══════════════════════════════════════════════════════════════════════════════
# OBSERVATION INTAKE
# ══════════════════════════════════════════════════════════════════════════════

def _on_state_change(agent, entity_id, new_state, ts=None):
    """Feed one entity update (from MQTT or tests). new_state may be a raw
    string or the bridge's {'state': ...} dict."""
    if isinstance(new_state, dict):
        new_state = new_state.get("state")
    agent.state["entities"][entity_id] = (ts or time.time(), str(new_state))


def _occupied(agent):
    for o in agent.state["binding"].get("occupancy") or []:
        hit = agent.state["entities"].get(o.get("entity"))
        if hit and hit[1] == str(o.get("occupied_state")):
            return True
    return False


def _fresh_obs(agent):
    prim = agent.state["binding"].get("primary_observation")
    hit = agent.state["entities"].get(prim)
    if not hit:
        return None
    ts, raw = hit
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if time.time() - ts > STALE_FACTOR * agent.state["tick_s"]:
        return None
    return v


# ══════════════════════════════════════════════════════════════════════════════
# BELIEF UPDATE · LEARNING · EFE PLANNING
# ══════════════════════════════════════════════════════════════════════════════

def _B_row(s, a, frm):
    prior = s["B_prior"][a][frm]
    counts = s["B_counts"][a][frm]
    total = sum(counts)
    if not s["learnable"] or total == 0:
        return prior
    return [(PRIOR_STRENGTH * p + n) / (PRIOR_STRENGTH + total)
            for p, n in zip(prior, counts)]


def _predict(s, belief, a):
    out = [0.0] * N_BINS
    for frm, b in enumerate(belief):
        if b < 1e-9:
            continue
        row = _B_row(s, a, frm)
        for to in range(N_BINS):
            out[to] += b * row[to]
    z = sum(out)
    return [x / z for x in out] if z else belief


def _tick(agent):
    """One control step: correct → learn → plan → decide. Returns decision."""
    s = agent.state
    obs = _fresh_obs(agent)
    stale = obs is None

    # predict with previous action
    belief = _predict(s, s["belief"], s["prev_action"])
    if not stale:
        obs_bin = _bin_of(s["grid"], obs)
        like = s["A"][obs_bin]
        belief = [b * l for b, l in zip(belief, like)]
        z = sum(belief) or 1.0
        belief = [b / z for b in belief]
        # learn the observed transition (Dirichlet count)
        if s["learnable"] and s["prev_obs_bin"] is not None:
            s["B_counts"][s["prev_action"]][s["prev_obs_bin"]][obs_bin] += 1.0
            s["action_tries"][s["prev_action"]] += 1.0
        s["prev_obs_bin"] = obs_bin
    s["belief"] = belief

    occupied = _occupied(agent)
    comfort_active = occupied == (s["C"]["comfort_when"] == "occupied")
    util = _utility(s["grid"], s["C"], comfort_active)

    scores = []
    for ai, act in enumerate(s["actions"]):
        b, comfort = belief, 0.0
        for _ in range(HORIZON):
            b = _predict(s, b, ai)
            comfort += sum(p * u for p, u in zip(b, util))
        energy = s["C"]["energy_weight"] * act["intensity"] * HORIZON
        curiosity = EPISTEMIC_W / (1.0 + s["action_tries"][ai])
        scores.append({"action": ai, "comfort": round(comfort, 3),
                       "energy": round(-energy, 3), "curiosity": round(curiosity, 3),
                       "total": round(comfort - energy + curiosity, 3)})
    ranked = sorted(scores, key=lambda x: -x["total"])
    chosen, runner = ranked[0], (ranked[1] if len(ranked) > 1 else None)
    s["prev_action"] = chosen["action"]

    mean = sum(g * p for g, p in zip(s["grid"], belief))
    sd = math.sqrt(max(sum(p * (g - mean) ** 2
                           for g, p in zip(s["grid"], belief)), 0.0))
    act = s["actions"][chosen["action"]]
    prim = s["binding"].get("primary_observation")
    text = (
        f"Propose '{act['label']}' on {act['call'].get('entity')}: "
        f"{prim} believed ≈ {mean:.1f}±{sd:.1f}"
        + (" (no fresh reading — prediction only)" if stale else "")
        + f"; room {'occupied' if occupied else 'empty'} → comfort "
        + ("ACTIVE" if comfort_active else "relaxed")
        + f" (band {s['C']['comfort_band'][0]}–{s['C']['comfort_band'][1]}, "
          f"comfort_when={s['C']['comfort_when']}); "
        + f"EFE: comfort {chosen['comfort']:+.2f}, energy {chosen['energy']:+.2f}, "
          f"curiosity {chosen['curiosity']:+.2f}"
        + (f"; runner-up '{s['actions'][runner['action']]['label']}' "
           f"({runner['total']:+.2f})" if runner else "")
    )
    decision = {
        "action": act["call"], "label": act["label"], "intensity": act["intensity"],
        "explanation": {
            "text": text,
            "belief": {"mean": round(mean, 2), "sd": round(sd, 2),
                       "occupied": occupied, "comfort_active": comfort_active,
                       "fresh_observation": not stale},
            "efe_terms": {k: chosen[k] for k in ("comfort", "energy", "curiosity", "total")},
            "runner_up": ({"label": s["actions"][runner["action"]]["label"],
                           "total": runner["total"]} if runner else None),
        },
        "agent": agent.name, "ts": time.time(),
    }
    s["last_decision"] = decision
    return decision


async def _propose(agent, decision, force=False):
    """Publish action + explanation together on the suggestion topic (and the
    flattened record on the decision topic) — every k minutes, or on change."""
    s = agent.state
    now = time.time()
    changed = (s["last_proposal"] is None
               or s["last_proposal"]["label"] != decision["label"])
    due = now - s["last_proposal_ts"] >= s["propose_s"]
    if not (force or changed or due):
        return False
    await agent.publish(s["suggestion_topic"], decision)
    exp = decision["explanation"]
    await agent.publish(s["decision_topic"], {
        "intensity": decision["intensity"],
        "belief_mean": exp["belief"]["mean"], "belief_sd": exp["belief"]["sd"],
        "occupied": int(exp["belief"]["occupied"]),
        "comfort_active": int(exp["belief"]["comfort_active"]),
        "fresh_observation": int(exp["belief"]["fresh_observation"]),
        "efe_comfort": exp["efe_terms"]["comfort"],
        "efe_energy": exp["efe_terms"]["energy"],
        "efe_curiosity": exp["efe_terms"]["curiosity"],
        "efe_total": exp["efe_terms"]["total"],
    })
    s["last_proposal"] = decision
    s["last_proposal_ts"] = now
    agent.persist("b_counts", s["B_counts"])
    await agent.log(exp["text"])
    return True


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND WORKERS
# ══════════════════════════════════════════════════════════════════════════════

async def _mqtt_subscriber(agent):
    try:
        import aiomqtt
    except ImportError:
        await agent.log("aiomqtt unavailable — observation intake disabled", level="warning")
        return
    import os
    topics = agent.state["cfg"].get("subscribes") or ["homeassistant/state_changes/#"]
    watched = set((agent.state["binding"].get("observations") or [])
                  + [o.get("entity") for o in agent.state["binding"].get("occupancy") or []]
                  + [e for e in [agent.state["binding"].get("feedback")] if e])
    while True:
        try:
            async with aiomqtt.Client(
                agent._actor._mqtt_broker, agent._actor._mqtt_port,
                username=os.environ.get("MQTT_USERNAME") or None,
                password=os.environ.get("MQTT_PASSWORD") or None,
            ) as client:
                for t in topics:
                    await client.subscribe(t)
                async for msg in client.messages:
                    try:
                        payload = json.loads(msg.payload.decode())
                        eid = payload.get("entity_id")
                        if eid and (not watched or eid in watched):
                            _on_state_change(agent, eid, payload.get("new_state"))
                    except Exception:
                        pass
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(5)


async def _tick_loop(agent):
    while True:
        try:
            await asyncio.sleep(agent.state["tick_s"])
            decision = _tick(agent)
            await _propose(agent, decision)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            await agent.log(f"tick error: {exc}", level="warning")


# ══════════════════════════════════════════════════════════════════════════════
# LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

async def setup(agent):
    cfg = agent.recall("aif_config") or {}
    if not (cfg.get("binding") or {}).get("primary_observation"):
        await agent.log("no binding in config — waiting for 'configure' task",
                        level="warning")
        cfg = {"binding": {}, "model": {}}
    _init_state(agent, cfg)
    agent.declare_contract(
        publishes=[agent.state["suggestion_topic"], agent.state["decision_topic"]],
        subscribes=cfg.get("subscribes") or ["homeassistant/state_changes/#"],
    )
    if agent.state["binding"].get("primary_observation"):
        asyncio.create_task(_mqtt_subscriber(agent))
        asyncio.create_task(_tick_loop(agent))
    await agent.log(
        f"aif-agent ready | regulates "
        f"{agent.state['binding'].get('primary_observation')} via "
        f"{(agent.state['binding'].get('actuator') or {}).get('entity')} | "
        f"grid [{agent.state['grid'][0]:.1f}…{agent.state['grid'][-1]:.1f}] | "
        f"proposals → {agent.state['suggestion_topic']}"
    )


async def process(agent):
    pass  # the tick loop does the work


# ══════════════════════════════════════════════════════════════════════════════
# handle_task
# ══════════════════════════════════════════════════════════════════════════════

async def handle_task(agent, payload):
    if isinstance(payload, dict) and not payload.get("action") and payload.get("text"):
        try:
            parsed = json.loads(payload["text"])
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            pass
    action = str(payload.get("action") or payload.get("text") or "").strip().lower()

    if action in ("propose", "propose_now") or "propose" in action:
        decision = _tick(agent)
        await _propose(agent, decision, force=True)
        return {"result": decision["explanation"]["text"], "decision": decision}

    if action == "status":
        d = agent.state["last_decision"]
        return {"result": d["explanation"]["text"] if d else "no decision yet",
                "decision": d,
                "grid": [round(g, 2) for g in agent.state["grid"]],
                "C": agent.state["C"],
                "suggestion_topic": agent.state["suggestion_topic"]}

    if action == "explain":
        d = agent.state["last_decision"]
        if not d:
            return {"result": "No decision yet."}
        text = d["explanation"]["text"]
        if agent.llm is not None:
            try:
                text = await agent.llm.chat(
                    "Rephrase this controller decision for a non-technical user "
                    "in 2 sentences: " + text)
            except Exception:
                pass
        return {"result": text, "decision": d}

    if action == "configure":
        c = agent.state["C"]
        changed = []
        for k in ("comfort_when", "comfort_band", "comfort_weight", "energy_weight"):
            if payload.get(k) is not None:
                c[k] = payload[k]
                changed.append(k)
        if "comfort_band" in changed:
            agent.state["grid"] = _make_grid(
                (agent.state["cfg"].get("model") or {}).get("D", {}).get("value_range"),
                c["comfort_band"])
            agent.state["A"] = _build_A(
                agent.state["grid"],
                float((agent.state["cfg"].get("model") or {}).get("A", {}).get("obs_noise", 0.85)))
        if not changed:
            return {"result": "Nothing to configure. Fields: comfort_when, "
                              "comfort_band, comfort_weight, energy_weight."}
        cfg = agent.state["cfg"]
        cfg.setdefault("model", {})["C"] = c
        agent.persist("aif_config", cfg)
        return {"result": f"Updated C: {changed}. Now {c}.", "C": c}

    return {
        "result": "Actions: propose (decide + publish now), status, explain "
                  "(LLM narration if available), configure (update C at runtime).",
        "commands": ["propose", "status", "explain", "configure"],
    }
'''
