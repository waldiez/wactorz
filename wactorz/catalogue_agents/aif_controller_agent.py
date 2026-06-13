"""
aif_controller_agent.py — single-agent deployment of the Factored Active
Inference OfficeMedium policy, running ALL zones as one N=15 batch (the same
BatchedFactoredAgents object v7's PyMDPOffice drives). Publishes one normalized
[-1,1] action per zone to the per-zone action topics.

Why this exists alongside aif-fleet
------------------------------------
aif-fleet spawns 15 decentralized per-zone agents (mirrors maddpg-fleet). That
is equivalent to the full batch EXCEPT for discrete-argmax tie-breaking: an N=15
batched matmul and an N=1 matmul round flat-EFE ties differently, so a few zone
actions can flip on near-tie steps and (because beliefs are stateful) desync for
the rest of the episode. The reward impact is small (the flips are between near-
equal-EFE actions), but it is not bit-exact.

This controller keeps all zones in ONE batch, so it reproduces a full-batch v7
run exactly. Use it when you need precise parity with your v7 eval number; use
aif-fleet when you want the decentralized 15-agent topology.

Requirements / params are the same as aif-fleet (bounds + planning
hyperparameters MUST match the v7 run that produced the checkpoint):
  env_id, model_path, infer_dir, aif_src_dir, zones,
  heat_low/heat_high/cool_low/cool_high,
  policy_len, comfort_weight, energy_weight, epistemic_weight, unocc_gate,
  deadband_weight, pB_prior_scale, override, freeze_B, lr_pB, publish_mode

Control (via @aif-controller):
  {"action":"launch", ...}   # (re)configure + start
  {"action":"status"}
  {"action":"stop"}          # stop publishing (unsubscribe)
"""

AGENT_CODE = r'''
import asyncio
import os
import sys as _sys

DEFAULTS = {
    "env_id":       "Eplus-officeMedium-MultiAgent-v1",
    "model_path":   "/data/aif/aif_model.pkl",
    "zones":        None,
    "info_timeout": 30.0,
    "infer_dir":    None,
    "aif_src_dir":  None,
    "heat_low":     15.0, "heat_high": 22.0, "cool_low": 24.0, "cool_high": 30.0,
    "policy_len":   4,
    "comfort_weight":  1.0, "energy_weight": 0.5, "epistemic_weight": 0.2,
    "unocc_gate":      0.1, "deadband_weight": 4.0, "pB_prior_scale": 2.0,
    "override":     "safety", "freeze_B": False, "lr_pB": 1.0,
    "publish_mode": "normalized",
}


def _cfg(agent):
    cfg = dict(DEFAULTS)
    saved = agent.recall("config", None)
    if isinstance(saved, dict):
        cfg.update({k: v for k, v in saved.items() if v is not None})
    return cfg


async def _resolve_zones(agent, cfg):
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


def _resolve_bounds(cfg, info):
    b = {k: float(cfg[k]) for k in ("heat_low", "heat_high", "cool_low", "cool_high")}
    info = info or {}
    src = "config/defaults"
    try:
        lo = info.get("action_low") or info.get("action_space", {}).get("low")
        hi = info.get("action_high") or info.get("action_space", {}).get("high")
        if lo is not None and hi is not None and len(lo) >= 1 and len(hi) >= 1:
            n = len(lo) // 2
            b["heat_low"], b["heat_high"] = float(lo[0]), float(hi[0])
            b["cool_low"], b["cool_high"] = float(lo[n]), float(hi[n])
            src = "env_info"
    except Exception:
        pass
    return b, src


def _build_controller(cfg, zones, bounds):
    infer_dir = cfg.get("infer_dir") or os.path.dirname(os.path.abspath(cfg["model_path"]))
    src_dir   = cfg.get("aif_src_dir") or infer_dir
    for d in (infer_dir, src_dir):
        if d and d not in _sys.path:
            _sys.path.insert(0, d)
    from aif_infer import AIFBatchController
    return AIFBatchController(
        cfg["model_path"],
        heat_low=bounds["heat_low"], heat_high=bounds["heat_high"],
        cool_low=bounds["cool_low"], cool_high=bounds["cool_high"],
        zones=zones,
        comfort_weight=cfg["comfort_weight"], energy_weight=cfg["energy_weight"],
        policy_len=int(cfg["policy_len"]), lr_pB=float(cfg["lr_pB"]),
        pB_prior_scale=float(cfg["pB_prior_scale"]),
        epistemic_weight=cfg["epistemic_weight"], unocc_gate=cfg["unocc_gate"],
        deadband_weight=cfg["deadband_weight"], freeze_B=bool(cfg["freeze_B"]),
        override=cfg["override"],
    )


async def _launch(agent, cfg):
    zones = await _resolve_zones(agent, cfg)
    if not zones:
        return {"ok": False, "message": "no zone list (env_info not seen and no 'zones' param)"}
    if not os.path.exists(cfg["model_path"]):
        return {"ok": False, "message": f"AIF checkpoint not found at {cfg['model_path']}"}

    bounds, bsrc = _resolve_bounds(cfg, agent.state.get("info"))
    try:
        ctrl = await asyncio.to_thread(_build_controller, cfg, zones, bounds)
    except Exception as e:
        await agent.log(f"[aif-controller] build failed: {e}", level="error")
        return {"ok": False, "message": f"controller build failed: {e}"}

    agent.state["ctrl"]    = ctrl
    agent.state["zones"]   = zones
    agent.state["bounds"]  = bounds
    agent.state["last_ep"] = None
    agent.state["acted"]   = 0
    env_id = cfg["env_id"]
    agent.state["env_id"]  = env_id
    agent.persist("config", cfg)

    await agent.log(
        f"[aif-controller] ready (single N={len(zones)} batch — exact v7 parity). "
        f"bounds[{bsrc}] htg[{bounds['heat_low']},{bounds['heat_high']}] "
        f"clg[{bounds['cool_low']},{bounds['cool_high']}] | policy_len={cfg['policy_len']} "
        f"energy_weight={cfg['energy_weight']} freeze_B={cfg['freeze_B']}")
    return {"ok": True, "message": f"controller running for {len(zones)} zones",
            "zones": zones, "bounds": bounds, "bounds_source": bsrc}


async def setup(agent):
    agent.state["ctrl"] = None
    agent.state["info"] = None
    cfg = _cfg(agent)

    info_topic = f"sinergym/env/{cfg['env_id']}/env_info"

    async def on_info(payload):
        if isinstance(payload, dict) and payload.get("zones"):
            agent.state["info"] = payload

    async def on_obs(payload):
        ctrl = agent.state.get("ctrl")
        if ctrl is None or not isinstance(payload, dict):
            return
        obs = payload.get("obs")
        if obs is None:
            return
        ep = payload.get("episode")
        last = agent.state.get("last_ep")
        if payload.get("done") or (last is not None and ep != last):
            ctrl.reset()
        agent.state["last_ep"] = ep

        import numpy as _np
        results = ctrl.step(_np.asarray(obs, dtype=_np.float64))
        env_id = agent.state.get("env_id", cfg["env_id"])
        pmode  = agent.recall("config", {}).get("publish_mode", "normalized")
        for zi, (zone, norm, sp) in enumerate(results):
            action_out = ([float(sp[0]), float(sp[1])] if pmode == "setpoints"
                          else [float(norm[0]), float(norm[1])])
            await agent.publish(f"sinergym/env/{env_id}/zone/{zone}/action", {
                "action":     action_out,
                "setpoints":  [float(sp[0]), float(sp[1])],
                "zone":       zone,
                "zone_index": zi,
                "agent":      "aif-controller",
                "policy":     cfg["model_path"],
                "step":       payload.get("step"),
                "episode":    ep,
            })
        agent.state["acted"] = agent.state.get("acted", 0) + 1

    agent.subscribe(info_topic, on_info)
    agent.subscribe(f"sinergym/env/{cfg['env_id']}/observation", on_obs)
    await agent.log(f"aif-controller subscribed; waiting to launch on env {cfg['env_id']}")

    if os.path.exists(cfg["model_path"]):
        await _launch(agent, cfg)


async def process(agent):
    pass


async def handle_task(agent, payload):
    import json as _json
    req = {}
    if isinstance(payload, dict) and "action" in payload:
        req = payload
    else:
        raw = payload if isinstance(payload, str) else (payload.get("text") or "" if isinstance(payload, dict) else "")
        s = (raw or "").strip()
        if s.startswith("{"):
            try: req = _json.loads(s)
            except Exception: req = {}
        elif s:
            req = {"action": s.split()[0]}

    action = (req.get("action") or "").lower().strip()
    cfg = _cfg(agent)
    for k in list(DEFAULTS.keys()):
        if req.get(k) is not None:
            cfg[k] = req[k]

    if action == "launch":
        return await _launch(agent, cfg)
    if action == "stop":
        agent.state["ctrl"] = None
        return {"ok": True, "message": "controller stopped (no longer publishing)"}
    # status
    return {"ok": True,
            "running":  agent.state.get("ctrl") is not None,
            "zones":    agent.state.get("zones"),
            "acted":    agent.state.get("acted", 0),
            "bounds":   agent.state.get("bounds"),
            "config":   cfg}
'''


# ──────────────────────────────────────────────────────────────────────────────
# REGISTER — add inside _build_catalog() in catalog_agent.py
# ──────────────────────────────────────────────────────────────────────────────
#   code = _load_recipe("aif_controller_agent.py")
#   if code:
#       catalog["aif-controller"] = {
#           "name":         "aif-controller",
#           "type":         "dynamic",
#           "description":  "Single-agent AIF OfficeMedium controller: runs all "
#                           "zones as one batch (exact v7 parity) and publishes "
#                           "per-zone normalized actions. Use instead of aif-fleet "
#                           "when bit-exact parity with a full-batch v7 run matters.",
#           "capabilities": ["sinergym", "active_inference", "pymdp", "aif",
#                            "rl_inference", "building_control", "energy_optimization"],
#           "install":      ["torch", "numpy", "aiomqtt"],
#           "input_schema": {
#               "action":          "str  — launch | stop | status",
#               "env_id":          "str", "model_path": "str", "zones": "list",
#               "infer_dir":       "str", "aif_src_dir": "str",
#               "heat_low":"float","heat_high":"float","cool_low":"float","cool_high":"float",
#               "policy_len":"int","energy_weight":"float","comfort_weight":"float",
#               "epistemic_weight":"float","unocc_gate":"float","deadband_weight":"float",
#               "pB_prior_scale":"float","override":"str","freeze_B":"bool","lr_pB":"float",
#               "publish_mode":"str",
#           },
#           "output_schema": {"ok": "bool", "zones": "list", "message": "str"},
#           "poll_interval": 3600,
#           "code":          code,
#       }
#       logger.info("[catalog] Loaded aif-controller recipe")
