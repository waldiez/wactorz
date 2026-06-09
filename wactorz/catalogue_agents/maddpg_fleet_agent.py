"""
maddpg_fleet_agent.py — catalogue recipe for deploying the trained MADDPG policy
as a fleet of per-zone inference agents (decentralized execution).

Place this file in your `catalogue_agents/` directory and register it in
catalog_agent.py `_build_catalog()` (see REGISTER block at the bottom).

What it does
------------
Spawning `maddpg-fleet` starts ONE launcher agent. The launcher:
  1. Reads the canonical zone list from the bridge's env_info topic
     (sinergym/env/{env_id}/env_info) — the same ordering the bridge uses to
     route per-zone action topics — or from an explicit `zones` param.
  2. Spawns one supervised DynamicAgent per zone: `maddpg-zone-00 … -NN`.
     Each child loads ONLY its actor_{i} from the shared checkpoint, subscribes
     to the global observation topic, runs inference, and publishes a
     normalized [-1, 1] action to its own zone action topic. The MAS bridge
     denormalizes to real setpoints. No retraining: eval() + no_grad + frozen
     normalizer.

Requirements
------------
  * maddpg_infer.py on the wactorz process's import path.
  * Trained checkpoint (model.pt) + frozen normalizer (normalizer.npz) on disk;
    pass absolute paths.
  * Bridge running in deploy mode (it already writes obs+actions to Fuseki with
    sampling + batching, so the agents do NOT write to Fuseki themselves).

Control (via @maddpg-fleet or main natural language)
----------------------------------------------------
  {"action": "launch", "env_id": "...", "model_path": "/abs/model.pt",
   "normalizer_path": "/abs/normalizer.npz"}     # explicit launch / reconfigure
  {"action": "status"}                            # children + env_info seen + config
  {"action": "stop"}                              # stop all zone children
"""

# ──────────────────────────────────────────────────────────────────────────────
# PER-ZONE CHILD AGENT CODE
# The launcher prepends a header binding ZONE_INDEX / ZONE_NAME / MODEL_PATH /
# NORM_PATH / ENV_ID / OBS_TOPIC / ACTION_TOPIC before spawning each child.
# ──────────────────────────────────────────────────────────────────────────────
ZONE_AGENT_CODE = r'''
import sys as _sys
# INFER_DIR is injected by the launcher header; ensure maddpg_infer is importable
# regardless of how wactorz was started.
if INFER_DIR and INFER_DIR not in _sys.path:
    _sys.path.insert(0, INFER_DIR)

import numpy as np
from maddpg_infer import ObservationBuilder, ZonePolicy


async def setup(agent):
    agent.state["ob"]      = ObservationBuilder()
    agent.state["pol"]     = ZonePolicy(MODEL_PATH, NORM_PATH, ZONE_INDEX)
    agent.state["last_ep"] = None
    agent.state["acted"]   = 0
    # Identity stamped on every action so the bridge can record PROV provenance.
    agent.state["name"]    = f"maddpg-zone-{ZONE_INDEX:02d}"

    async def on_obs(payload):
        obs = payload.get("obs")
        if obs is None:
            return
        ep   = payload.get("episode")
        last = agent.state.get("last_ep")
        # Reset the GRU window + temp-delta tracker on episode boundaries so the
        # sequence never bridges across episodes.
        if payload.get("done") or (last is not None and ep != last):
            agent.state["ob"].reset()
            agent.state["pol"].reset()
        agent.state["last_ep"] = ep

        obs_raw   = np.asarray(obs, dtype=np.float32)
        agent_obs = agent.state["ob"].build_agent_obs(obs_raw)   # (15, 29)
        norm_act  = agent.state["pol"].step(agent_obs[ZONE_INDEX])  # [-1, 1]^2

        await agent.publish(ACTION_TOPIC, {
            "action":     [float(norm_act[0]), float(norm_act[1])],
            "zone":       ZONE_NAME,
            "zone_index": ZONE_INDEX,
            "agent":      agent.state["name"],   # PROV: who decided
            "policy":     MODEL_PATH,            # PROV: which checkpoint
            "step":       payload.get("step"),
            "episode":    ep,
        })
        agent.state["acted"] = agent.state.get("acted", 0) + 1

    agent.subscribe(OBS_TOPIC, on_obs)
    await agent.log(f"zone agent ready: idx={ZONE_INDEX} zone={ZONE_NAME} -> {ACTION_TOPIC}")


async def process(agent):
    # Fully event-driven (subscription callback does the work); idle here.
    pass


async def handle_task(agent, payload):
    return {
        "ok":         True,
        "zone":       ZONE_NAME,
        "zone_index": ZONE_INDEX,
        "acted":      agent.state.get("acted", 0),
        "action_topic": ACTION_TOPIC,
    }
'''


# ──────────────────────────────────────────────────────────────────────────────
# FLEET LAUNCHER CODE
# ──────────────────────────────────────────────────────────────────────────────
FLEET_BODY = r'''
import asyncio
import os

DEFAULTS = {
    # NOTE: set these to your real OfficeMedium env id + artifact paths, or pass
    # them in the "launch" task. If model_path does not exist on start, the fleet
    # waits for an explicit launch task instead of auto-starting.
    "env_id":          "Eplus-officeMedium-MultiAgent-v1",
    "model_path":      "/data/maddpg/model.pt",
    "normalizer_path": "/data/maddpg/normalizer.npz",
    "zones":           None,    # optional explicit zone list, in bridge order
    "info_timeout":    30.0,    # seconds to wait for env_info before giving up
    "infer_dir":       None,    # dir containing maddpg_infer.py; default = model.pt dir
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
        msg = f"model not found at {cfg['model_path']}"
        await agent.log(msg, level="error")
        return {"ok": False, "message": msg}

    DynamicAgent = type(agent._actor)   # avoid hard-coding the package path
    env_id    = cfg["env_id"]
    obs_topic = f"sinergym/env/{env_id}/observation"
    infer_dir = cfg.get("infer_dir") or os.path.dirname(os.path.abspath(cfg["model_path"]))

    names = []
    for i, zone in enumerate(zones):
        action_topic = f"sinergym/env/{env_id}/zone/{zone}/action"
        header = (
            f"ZONE_INDEX = {i}\n"
            f"ZONE_NAME = {zone!r}\n"
            f"MODEL_PATH = {cfg['model_path']!r}\n"
            f"NORM_PATH = {cfg['normalizer_path']!r}\n"
            f"ENV_ID = {env_id!r}\n"
            f"OBS_TOPIC = {obs_topic!r}\n"
            f"ACTION_TOPIC = {action_topic!r}\n"
            f"INFER_DIR = {infer_dir!r}\n\n"
        )
        child_code = header + ZONE_AGENT_CODE
        cname = f"maddpg-zone-{i:02d}"
        try:
            await agent._actor.spawn(
                DynamicAgent,
                name          = cname,
                code          = child_code,
                poll_interval = 3600.0,     # event-driven; process() is idle
                trusted       = True,       # skip safety validator (pre-built)
                description   = f"MADDPG zone controller idx={i} zone={zone}",
            )
            names.append(cname)
        except Exception as e:
            await agent.log(f"spawn failed for {cname}: {e}", level="error")

    agent.state["children"] = names
    agent.persist("config", cfg)
    await agent.log(f"launched {len(names)}/{len(zones)} zone agents for env {env_id}")
    return {"ok": True, "message": f"launched {len(names)} zone agents",
            "children": names, "zones": zones}


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
    await agent.log(f"maddpg-fleet ready; listening for env_info on {info_topic}")

    # Auto-launch only if the checkpoint is already present.
    if os.path.exists(cfg["model_path"]):
        await _launch(agent, cfg)
    else:
        await agent.log(
            f"model not found at {cfg['model_path']} — send a 'launch' task "
            f"with model_path/env_id to start", level="warning")


async def process(agent):
    # Children are event-driven; nothing to poll here.
    pass


async def handle_task(agent, payload):
    import json as _json

    # The direct "@agent {json}" path delivers {"text": "<raw>"} WITHOUT parsing the
    # JSON, so normalize here. Accept: a structured dict with "action", a raw string,
    # the {"text": ...} wrapper, or a bare word like "status".
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
    for k in ("env_id", "model_path", "normalizer_path", "zones", "info_timeout", "infer_dir"):
        if req.get(k) is not None:
            cfg[k] = req[k]

    if not action:
        return {"ok": False,
                "message": "no action parsed, e.g. "
                           "@maddpg-fleet {\"action\":\"launch\",\"env_id\":\"...\","
                           "\"model_path\":\"state/maddpg_office/model.pt\","
                           "\"normalizer_path\":\"state/maddpg_office/normalizer.npz\"} "
                           "— or @maddpg-fleet status"}

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
#   code = _load_recipe("maddpg_fleet_agent.py")
#   if code:
#       catalog["maddpg-fleet"] = {
#           "name":         "maddpg-fleet",
#           "type":         "dynamic",
#           "description":  "Deploys the trained MADDPG OfficeMedium policy as a "
#                           "fleet of per-zone inference agents (one per zone). "
#                           "Inference only — loads model.pt + normalizer.npz, no retraining.",
#           "capabilities": ["sinergym", "maddpg", "rl_inference", "multi_agent",
#                            "building_control", "energy_optimization"],
#           "install":      ["torch", "numpy", "aiomqtt"],
#           "input_schema": {
#               "action":          "str — launch | stop | status",
#               "env_id":          "str — Sinergym env id (must match the bridge)",
#               "model_path":      "str — absolute path to trained model.pt",
#               "normalizer_path": "str — absolute path to normalizer.npz",
#               "zones":           "list — optional explicit zone names in bridge order",
#               "info_timeout":    "float — seconds to wait for env_info, default 30",
#               "infer_dir":       "str — dir holding maddpg_infer.py; default = model.pt dir",
#           },
#           "output_schema": {
#               "ok":        "bool",
#               "children":  "list — spawned zone agent names",
#               "zones":     "list",
#               "message":   "str",
#           },
#           "poll_interval": 3600,
#           "code":          code,
#       }
#       logger.info("[catalog] Loaded maddpg-fleet recipe")