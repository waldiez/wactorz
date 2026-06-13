"""
aif_fleet_agent.py — catalogue recipe for deploying the trained Factored Active
Inference policy (pymdp_office_v7_torch) as a fleet of per-zone inference agents
(decentralized execution), exactly mirroring maddpg_fleet_agent.py.

Place this file in your `catalogue_agents/` directory and register it in
catalog_agent.py `_build_catalog()` (see REGISTER block at the bottom).

What it does
------------
Spawning `aif-fleet` starts ONE launcher agent. The launcher:
  1. Reads the canonical zone list from the bridge's env_info topic
     (sinergym/env/{env_id}/env_info) — the same ordering the bridge uses to
     route per-zone action topics — or from an explicit `zones` param.
  2. Spawns one supervised DynamicAgent per zone: `aif-zone-00 … -NN`.
     Each child slices ONLY its zone out of the shared aif_model.pkl checkpoint
     (the batched AIF agents are mathematically independent, so an N=1 slice is
     numerically identical to the corresponding row of the N=15 batch),
     subscribes to the global observation topic, runs Active-Inference planning,
     and publishes a normalized [-1, 1] action to its own zone action topic. The
     MAS bridge denormalizes to real setpoints — so AIF agents are wire-
     compatible with MADDPG agents. No retraining: freeze_B=True (frozen B_T).

Requirements
------------
  * aif_infer.py AND pymdp_office_v7_torch.py on the wactorz process's import
    path (point `infer_dir` at the directory holding both).
  * Trained checkpoint aif_model.pkl on disk (PyMDPOffice.save() format,
    mode='zone'); pass an absolute path.
  * Action bounds (heat_low/heat_high/cool_low/cool_high) that MATCH the env the
    model was trained on and the bounds the bridge denormalizes with. The fleet
    will try to read them from env_info; otherwise it uses the launch params /
    the standard OfficeMedium defaults and logs which it used.
  * Bridge running in deploy mode (it writes obs+actions to Fuseki itself, so the
    agents do NOT write to Fuseki).

Control (via @aif-fleet or main natural language)
-------------------------------------------------
  {"action": "launch", "env_id": "...", "model_path": "/abs/aif_model.pkl"}
  {"action": "status"}
  {"action": "stop"}
"""

# ──────────────────────────────────────────────────────────────────────────────
# PER-ZONE CHILD AGENT CODE
# The launcher prepends a header binding ZONE_INDEX / ZONE_NAME / MODEL_PATH /
# ENV_ID / OBS_TOPIC / ACTION_TOPIC / INFER_DIR / action bounds / POLICY_LEN /
# PUBLISH_MODE before spawning each child.
# ──────────────────────────────────────────────────────────────────────────────
ZONE_AGENT_CODE = r'''
import sys as _sys
# INFER_DIR is injected by the launcher header; ensure aif_infer +
# pymdp_office_v7_torch are importable regardless of how wactorz was started.
if INFER_DIR and INFER_DIR not in _sys.path:
    _sys.path.insert(0, INFER_DIR)
# AIF_SRC_DIR (optional) is the directory holding pymdp_office_v7_torch.py, in
# case it does not live next to aif_infer.py.
if AIF_SRC_DIR and AIF_SRC_DIR not in _sys.path:
    _sys.path.insert(0, AIF_SRC_DIR)

import numpy as np
from aif_infer import AIFZonePolicy


async def setup(agent):
    agent.state["pol"] = AIFZonePolicy(
        MODEL_PATH, ZONE_INDEX,
        heat_low=HEAT_LOW, heat_high=HEAT_HIGH,
        cool_low=COOL_LOW, cool_high=COOL_HIGH,
        comfort_weight=COMFORT_WEIGHT, energy_weight=ENERGY_WEIGHT,
        policy_len=POLICY_LEN, lr_pB=LR_PB, pB_prior_scale=PB_PRIOR_SCALE,
        epistemic_weight=EPISTEMIC_WEIGHT, unocc_gate=UNOCC_GATE,
        deadband_weight=DEADBAND_WEIGHT, freeze_B=FREEZE_B, override=OVERRIDE,
    )
    agent.state["last_ep"] = None
    agent.state["acted"]   = 0
    # Identity stamped on every action so the bridge can record PROV provenance.
    agent.state["name"]    = f"aif-zone-{ZONE_INDEX:02d}"

    async def on_obs(payload):
        obs = payload.get("obs")
        if obs is None:
            return
        ep   = payload.get("episode")
        last = agent.state.get("last_ep")
        # Reset beliefs + prev_action on episode boundaries so the AIF sequence
        # never bridges across episodes.
        if payload.get("done") or (last is not None and ep != last):
            agent.state["pol"].reset()
        agent.state["last_ep"] = ep

        obs_raw = np.asarray(obs, dtype=np.float64)
        norm_act, setpoints = agent.state["pol"].step(obs_raw)   # [-1,1]^2 , degC^2

        if PUBLISH_MODE == "setpoints":
            action_out = [float(setpoints[0]), float(setpoints[1])]
        else:  # "normalized" — matches the MADDPG bridge contract
            action_out = [float(norm_act[0]), float(norm_act[1])]

        await agent.publish(ACTION_TOPIC, {
            "action":     action_out,
            "setpoints":  [float(setpoints[0]), float(setpoints[1])],  # PROV: real degC
            "zone":       ZONE_NAME,
            "zone_index": ZONE_INDEX,
            "agent":      agent.state["name"],   # PROV: who decided
            "policy":     MODEL_PATH,            # PROV: which checkpoint
            "step":       payload.get("step"),
            "episode":    ep,
        })
        agent.state["acted"] = agent.state.get("acted", 0) + 1

    agent.subscribe(OBS_TOPIC, on_obs)
    await agent.log(f"aif zone agent ready: idx={ZONE_INDEX} zone={ZONE_NAME} -> {ACTION_TOPIC}")


async def process(agent):
    # Fully event-driven (subscription callback does the work); idle here.
    pass


async def handle_task(agent, payload):
    return {
        "ok":           True,
        "zone":         ZONE_NAME,
        "zone_index":   ZONE_INDEX,
        "acted":        agent.state.get("acted", 0),
        "action_topic": ACTION_TOPIC,
        "publish_mode": PUBLISH_MODE,
        "freeze_B":     FREEZE_B,
    }
'''


# ──────────────────────────────────────────────────────────────────────────────
# FLEET LAUNCHER CODE
# ──────────────────────────────────────────────────────────────────────────────
FLEET_BODY = r'''
import asyncio
import os

DEFAULTS = {
    # NOTE: set these to your real OfficeMedium env id + checkpoint path, or pass
    # them in the "launch" task. If model_path does not exist on start, the fleet
    # waits for an explicit launch task instead of auto-starting.
    "env_id":       "Eplus-officeMedium-MultiAgent-v1",
    "model_path":   "/data/aif/aif_model.pkl",
    "zones":        None,        # optional explicit zone list, in bridge order
    "info_timeout": 30.0,        # seconds to wait for env_info before giving up
    "infer_dir":    None,        # dir with aif_infer.py + pymdp_office_v7_torch.py;
                                 # default = aif_model.pkl dir
    "aif_src_dir":  None,        # dir holding pymdp_office_v7_torch.py if it is NOT
                                 # next to aif_infer.py; default = infer_dir
    # Action bounds — MUST match the trained env / the bounds the bridge uses to
    # denormalize. Read from env_info when available; else these defaults apply.
    # (OfficeMedium: htg [15, 22], clg [24, 30].)
    "heat_low":     15.0,
    "heat_high":    22.0,
    "cool_low":     24.0,
    "cool_high":    30.0,
    "policy_len":   4,           # AIF planning horizon (match training!)
    "comfort_weight":  1.0,      # EFE comfort term scale (match training)
    "energy_weight":   0.5,      # EFE energy term scale  (match training!)
    "epistemic_weight":0.2,      # EFE info-gain term scale (match training)
    "unocc_gate":      0.1,      # unoccupied comfort gate (match training)
    "deadband_weight": 4.0,      # deadband penalty scale  (match training)
    "pB_prior_scale":  2.0,      # init pseudo-counts (overwritten by checkpoint)
    "override":     "safety",    # reactive safety-net mode: safety | aggressive
    "freeze_B":     False,       # False = keep adapting B_T online (matches v7
                                 # run_simulation default / the deploy baseline);
                                 # True = frozen inference (B_T fixed as trained)
    "lr_pB":        1.0,         # Dirichlet learning rate when freeze_B is False
    "publish_mode": "normalized",  # "normalized" (bridge denormalizes) | "setpoints"
}


def _cfg(agent):
    cfg   = dict(DEFAULTS)
    saved = agent.recall("config", None)
    if isinstance(saved, dict):
        cfg.update({k: v for k, v in saved.items() if v is not None})
    return cfg


async def _resolve_zones(agent, cfg):
    """Prefer an explicit list; else wait for the bridge's env_info topic."""
    if cfg.get("zones"):
        return list(cfg["zones"])
    waited = 0.0
    timeout = float(cfg.get("info_timeout", 30.0))
    while waited < timeout:
        info = agent.state.get("info")
        if info and info.get("zones"):
            return list(info["zones"])
        await asyncio.sleep(0.5)
        waited += 0.5
    return None


def _resolve_bounds(agent, cfg):
    """Best-effort: pull action bounds from env_info if the bridge exposes them,
    else keep the configured/default values. Returns (bounds_dict, source)."""
    b = {k: float(cfg[k]) for k in ("heat_low", "heat_high", "cool_low", "cool_high")}
    info = agent.state.get("info") or {}
    src = "config/defaults"
    try:
        # Accept a few plausible env_info shapes without depending on any of them.
        lo = info.get("action_low") or info.get("action_space", {}).get("low")
        hi = info.get("action_high") or info.get("action_space", {}).get("high")
        if lo is not None and hi is not None and len(lo) >= 1 and len(hi) >= 1:
            n = len(lo) // 2  # [htg_0..htg_{n-1}, clg_0..clg_{n-1}]
            b["heat_low"], b["heat_high"] = float(lo[0]),  float(hi[0])
            b["cool_low"], b["cool_high"] = float(lo[n]),  float(hi[n])
            src = "env_info"
        else:
            for k in ("heat_low", "heat_high", "cool_low", "cool_high"):
                if info.get(k) is not None:
                    b[k] = float(info[k]); src = "env_info"
    except Exception:
        pass
    return b, src


async def _launch(agent, cfg):
    if agent.state.get("children"):
        return {"ok": True, "message": "already launched",
                "children": agent.state["children"]}

    zones = await _resolve_zones(agent, cfg)
    if not zones:
        msg = ("no zone list available (env_info not seen and no 'zones' param) "
               "— refusing to launch so agents never publish to wrong topics")
        await agent.log(msg, level="error")
        return {"ok": False, "message": msg}

    if not os.path.exists(cfg["model_path"]):
        msg = f"AIF checkpoint not found at {cfg['model_path']}"
        await agent.log(msg, level="error")
        return {"ok": False, "message": msg}

    bounds, src = _resolve_bounds(agent, cfg)
    await agent.log(f"aif-fleet action bounds [{src}]: "
                    f"htg[{bounds['heat_low']},{bounds['heat_high']}] "
                    f"clg[{bounds['cool_low']},{bounds['cool_high']}] — these MUST "
                    f"match the trained env / the bridge's denormalization.",
                    level="info" if src == "env_info" else "warning")
    await agent.log(
        "aif-fleet planning cfg (MUST match the v7 training run): "
        f"policy_len={cfg['policy_len']} energy_weight={cfg['energy_weight']} "
        f"comfort_weight={cfg['comfort_weight']} epistemic_weight={cfg['epistemic_weight']} "
        f"unocc_gate={cfg['unocc_gate']} deadband_weight={cfg['deadband_weight']} "
        f"override={cfg['override']!r} freeze_B={cfg['freeze_B']} lr_pB={cfg['lr_pB']}")

    DynamicAgent = type(agent._actor)   # avoid hard-coding the package path
    env_id    = cfg["env_id"]
    obs_topic = f"sinergym/env/{env_id}/observation"
    infer_dir = cfg.get("infer_dir") or os.path.dirname(os.path.abspath(cfg["model_path"]))
    aif_src_dir = cfg.get("aif_src_dir") or infer_dir

    names = []
    for i, zone in enumerate(zones):
        action_topic = f"sinergym/env/{env_id}/zone/{zone}/action"
        header = (
            f"ZONE_INDEX = {i}\n"
            f"ZONE_NAME = {zone!r}\n"
            f"MODEL_PATH = {cfg['model_path']!r}\n"
            f"ENV_ID = {env_id!r}\n"
            f"OBS_TOPIC = {obs_topic!r}\n"
            f"ACTION_TOPIC = {action_topic!r}\n"
            f"INFER_DIR = {infer_dir!r}\n"
            f"AIF_SRC_DIR = {aif_src_dir!r}\n"
            f"HEAT_LOW = {bounds['heat_low']!r}\n"
            f"HEAT_HIGH = {bounds['heat_high']!r}\n"
            f"COOL_LOW = {bounds['cool_low']!r}\n"
            f"COOL_HIGH = {bounds['cool_high']!r}\n"
            f"POLICY_LEN = {int(cfg['policy_len'])}\n"
            f"COMFORT_WEIGHT = {float(cfg['comfort_weight'])!r}\n"
            f"ENERGY_WEIGHT = {float(cfg['energy_weight'])!r}\n"
            f"EPISTEMIC_WEIGHT = {float(cfg['epistemic_weight'])!r}\n"
            f"UNOCC_GATE = {float(cfg['unocc_gate'])!r}\n"
            f"DEADBAND_WEIGHT = {float(cfg['deadband_weight'])!r}\n"
            f"PB_PRIOR_SCALE = {float(cfg['pB_prior_scale'])!r}\n"
            f"OVERRIDE = {cfg['override']!r}\n"
            f"FREEZE_B = {bool(cfg['freeze_B'])!r}\n"
            f"LR_PB = {float(cfg['lr_pB'])!r}\n"
            f"PUBLISH_MODE = {cfg['publish_mode']!r}\n\n"
        )
        child_code = header + ZONE_AGENT_CODE
        cname = f"aif-zone-{i:02d}"
        try:
            await agent._actor.spawn(
                DynamicAgent,
                name          = cname,
                code          = child_code,
                poll_interval = 3600.0,     # event-driven; process() is idle
                trusted       = True,       # skip safety validator (pre-built)
                description   = f"AIF zone controller idx={i} zone={zone}",
            )
            names.append(cname)
        except Exception as e:
            await agent.log(f"spawn failed for {cname}: {e}", level="error")

    agent.state["children"] = names
    agent.persist("config", cfg)
    await agent.log(f"launched {len(names)}/{len(zones)} AIF zone agents for env {env_id}")
    return {"ok": True, "message": f"launched {len(names)} zone agents",
            "children": names, "zones": zones, "bounds": bounds, "bounds_source": src}


async def _stop_children(agent):
    reg = getattr(agent._actor, "_registry", None)
    sup = getattr(reg, "_supervisor_ref", None) if reg else None
    stopped = []
    for cname in agent.state.get("children", []):
        try:
            child = reg.find_by_name(cname) if reg else None
            if child:
                if sup:
                    sup.release(cname)          # Erlang unlink: avoid restart race
                await child.stop()
                stopped.append(cname)
        except Exception as e:
            await agent.log(f"stop failed for {cname}: {e}", level="warning")
    agent.state["children"] = []
    return stopped


async def setup(agent):
    agent.state["children"] = []
    agent.state["info"]     = None
    cfg = _cfg(agent)

    info_topic = f"sinergym/env/{cfg['env_id']}/env_info"

    async def on_info(payload):
        if isinstance(payload, dict) and payload.get("zones"):
            agent.state["info"] = payload

    agent.subscribe(info_topic, on_info)
    await agent.log(f"aif-fleet ready; listening for env_info on {info_topic}")

    # Auto-launch only if the checkpoint is already present.
    if os.path.exists(cfg["model_path"]):
        await _launch(agent, cfg)
    else:
        await agent.log(
            f"AIF checkpoint not found at {cfg['model_path']} — send a 'launch' "
            f"task with model_path/env_id to start", level="warning")


async def process(agent):
    # Children are event-driven; nothing to poll here.
    pass


async def handle_task(agent, payload):
    import json as _json

    # The direct "@agent {json}" path delivers {"text": "<raw>"} WITHOUT parsing
    # the JSON, so normalize here. Accept: a structured dict with "action", a raw
    # string, the {"text": ...} wrapper, or a bare word like "status".
    req = {}
    if isinstance(payload, dict) and "action" in payload:
        req = payload
    else:
        if isinstance(payload, str):
            raw = payload
        elif isinstance(payload, dict):
            raw = payload.get("text") or ""
        else:
            raw = ""
        s = (raw or "").strip()
        if s.startswith("{"):
            try:
                req = _json.loads(s)
            except Exception:
                req = {}
        elif s:
            req = {"action": s.split()[0]}

    action = (req.get("action") or "").lower().strip()
    cfg = _cfg(agent)
    for k in ("env_id", "model_path", "zones", "info_timeout", "infer_dir",
              "aif_src_dir", "heat_low", "heat_high", "cool_low", "cool_high",
              "policy_len", "comfort_weight", "energy_weight", "epistemic_weight",
              "unocc_gate", "deadband_weight", "pB_prior_scale", "override",
              "freeze_B", "lr_pB", "publish_mode"):
        if req.get(k) is not None:
            cfg[k] = req[k]

    if not action:
        return {"ok": False,
                "message": "no action parsed, e.g. "
                           "@aif-fleet {\"action\":\"launch\",\"env_id\":\"...\","
                           "\"model_path\":\"state/maddpg_office/aif_model.pkl\"} "
                           "— or @aif-fleet status"}

    if action == "launch":
        agent.persist("config", cfg)
        return await _launch(agent, cfg)

    if action == "status":
        return {"ok": True,
                "children":      agent.state.get("children", []),
                "n_children":    len(agent.state.get("children", [])),
                "env_info_seen": bool(agent.state.get("info")),
                "config":        cfg}

    if action == "stop":
        stopped = await _stop_children(agent)
        return {"ok": True, "message": f"stopped {len(stopped)} zone agents",
                "stopped": stopped}

    return {"ok": False, "message": f"unknown action {action!r}; use launch|stop|status"}
'''


# Compose the final AGENT_CODE: bind ZONE_AGENT_CODE as a literal in the fleet's
# namespace, then the fleet body (which references it when spawning children).
AGENT_CODE = "ZONE_AGENT_CODE = " + repr(ZONE_AGENT_CODE) + "\n\n" + FLEET_BODY


# ──────────────────────────────────────────────────────────────────────────────
# REGISTER — add this block inside _build_catalog() in catalog_agent.py
# ──────────────────────────────────────────────────────────────────────────────
#   code = _load_recipe("aif_fleet_agent.py")
#   if code:
#       catalog["aif-fleet"] = {
#           "name":         "aif-fleet",
#           "type":         "dynamic",
#           "description":  "Deploys the trained Factored Active Inference "
#                           "OfficeMedium policy as a fleet of per-zone inference "
#                           "agents (one per zone). Inference only — slices "
#                           "aif_model.pkl per zone (freeze_B), no retraining.",
#           "capabilities": ["sinergym", "active_inference", "pymdp", "aif",
#                            "rl_inference", "multi_agent", "building_control",
#                            "energy_optimization"],
#           "install":      ["torch", "numpy", "aiomqtt"],
#           "input_schema": {
#               "action":       "str  — launch | stop | status",
#               "env_id":       "str  — Sinergym env id (must match the bridge)",
#               "model_path":   "str  — absolute path to trained aif_model.pkl",
#               "zones":        "list — optional explicit zone names in bridge order",
#               "info_timeout": "float — seconds to wait for env_info, default 30",
#               "infer_dir":    "str  — dir with aif_infer.py + pymdp_office_v7_torch.py",
#               "heat_low":     "float — htg action min (match trained env)",
#               "heat_high":    "float — htg action max (match trained env)",
#               "cool_low":     "float — clg action min (match trained env)",
#               "cool_high":    "float — clg action max (match trained env)",
#               "policy_len":   "int  — AIF planning horizon, default 4",
#               "freeze_B":     "bool — False keeps adapting B_T online (matches "
#                               "v7 eval default); True = frozen inference",
#               "lr_pB":        "float — Dirichlet lr when freeze_B is False, default 1.0",
#               "publish_mode": "str  — normalized | setpoints, default normalized",
#           },
#           "output_schema": {
#               "ok":       "bool",
#               "children": "list — spawned zone agent names",
#               "zones":    "list",
#               "message":  "str",
#           },
#           "poll_interval": 3600,
#           "code":          code,
#       }
#       logger.info("[catalog] Loaded aif-fleet recipe")