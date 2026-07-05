"""sinergym_labeler_agent.py — republish observations as keyed {name: value} dicts.

Place in catalogue_agents/ and register in catalog_agent.py _build_catalog()
(see REGISTER block at the bottom).

Why
---
Raw obs messages carry an INDEXED array ("obs": [4.0, 26.0, ...]) with no labels,
so consumers must know the index layout. This agent stays on the MQTT data plane
(no wactorz task/delegation needed): it learns the variable names from `env_info`,
then for every observation it republishes the SAME data as a name-keyed dict on a
parallel topic. Other agents just subscribe and read obs["air_temperature_Core_mid"].

Because the global obs variable names are zone-suffixed, the LABELED GLOBAL topic
already contains every zone's correctly-attributed value by key — the most reliable
way to read a specific zone's temperature/occupancy/etc.

Topics
------
  IN   sinergym/env/<env_id>/env_info                     (schema; episode start)
  IN   sinergym/env/<env_id>/observation                  (raw, indexed)
  OUT  sinergym/env/<env_id>/observation/labeled          (keyed dict)

  IN   sinergym/env/<env_id>/zone/+/env_info              (per-zone schema)
  IN   sinergym/env/<env_id>/zone/+/observation           (raw per-zone)
  OUT  sinergym/env/<env_id>/zone/<zone>/observation/labeled

Labeled payload (global) — FLAT, no wrapper
--------------------------------------------
  {
    "month": 4.0, "air_temperature_Core_mid": 21.5, ...,
    "step": 1254, "episode": 1, "reward": -1.01, "done": false,
    "info": { ... }, "env_id": "...", "source": "global"
  }

Control (optional, via @sinergym-labeler)
-----------------------------------------
  "status"                 -> whether names are cached + the output topics
  {"env_id": "..."}        -> set which env to label (default officeMedium-multiagent)
"""

AGENT_CODE = r"""
import asyncio
import json

ENV_ID_DEFAULT = "officeMedium-multiagent"
SUFFIX = "/labeled"


def _topics(env_id):
    base = f"sinergym/env/{env_id}"
    return {
        "env_info":      f"{base}/env_info",
        "zone_env_info": f"{base}/zone/+/env_info",
        "obs":           f"{base}/observation",
        "zone_obs":      f"{base}/zone/+/observation",
        "obs_out":       f"{base}/observation{SUFFIX}",
        "base":          base,
    }


def _zone_out(base, zone):
    return f"{base}/zone/{zone}/observation{SUFFIX}"


async def setup(agent):
    env_id = agent.recall("env_id", None) or ENV_ID_DEFAULT
    agent.state["env_id"]     = env_id
    agent.state["names"]      = agent.recall("obs_names", None)     # global, ordered
    agent.state["zone_names"] = agent.recall("zone_names", {}) or {}
    agent.state["labeled"]    = 0
    agent.state["warned"]     = False
    t = _topics(env_id)

    async def on_env_info(payload):
        if isinstance(payload, dict) and payload.get("obs_variable_names"):
            agent.state["names"] = payload["obs_variable_names"]
            agent.persist("obs_names", agent.state["names"])
            await agent.log(f"[labeler] learned {len(agent.state['names'])} global obs names")

    async def on_zone_env_info(payload):
        if isinstance(payload, dict) and payload.get("zone") and payload.get("obs_variable_names"):
            agent.state["zone_names"][payload["zone"]] = payload["obs_variable_names"]
            agent.persist("zone_names", agent.state["zone_names"])

    async def on_obs(payload):
        if not isinstance(payload, dict):
            return
        names = agent.state.get("names")
        vals = payload.get("obs")
        if not names or not isinstance(vals, list):
            if not agent.state["warned"]:
                await agent.log("[labeler] obs arrived before schema; waiting for env_info "
                          "(emitted at episode start)", level="warning")
                agent.state["warned"] = True
            return
        keyed = {names[i]: vals[i] for i in range(min(len(names), len(vals)))}
        # Flat payload: obs variables at top level, metadata merged alongside.
        out = dict(keyed)
        out.update({
            "step":    payload.get("step"),
            "episode": payload.get("episode"),
            "reward":  payload.get("reward"),
            "done":    payload.get("done"),
            "info":    payload.get("info"),
            "env_id":  agent.state["env_id"],
            "source":  "global",
        })
        await agent.publish(t["obs_out"], out)
        agent.state["labeled"] = agent.state.get("labeled", 0) + 1

    async def on_zone_obs(payload):
        if not isinstance(payload, dict):
            return
        zone = payload.get("zone")
        names = agent.state.get("zone_names", {}).get(zone)
        vals = payload.get("obs")
        if not zone or not names or not isinstance(vals, list):
            return   # global labeled topic is the reliable per-zone source anyway
        keyed = {names[i]: vals[i] for i in range(min(len(names), len(vals)))}
        out = dict(keyed)
        out.update({
            "zone":        zone,
            "step":        payload.get("step"),
            "episode":     payload.get("episode"),
            "reward":      payload.get("reward"),
            "action":      payload.get("action"),
            "action_real": payload.get("action_real"),
            "info":        payload.get("info"),
            "env_id":      agent.state["env_id"],
            "source":      "zone",
        })
        await agent.publish(_zone_out(t["base"], zone), out)

    agent.subscribe(t["env_info"], on_env_info)
    agent.subscribe(t["zone_env_info"], on_zone_env_info)
    agent.subscribe(t["obs"], on_obs)
    agent.subscribe(t["zone_obs"], on_zone_obs)
    await agent.log(f"sinergym-labeler ready; republishing keyed obs to {t['obs_out']} "
              f"(and per-zone .../zone/<zone>/observation{SUFFIX}).")


async def process(agent):
    pass


async def handle_task(agent, payload):
    if isinstance(payload, dict) and payload.get("env_id"):
        agent.state["env_id"] = payload["env_id"]
        agent.persist("env_id", payload["env_id"])
        agent.state["names"] = None
        return f"Now labeling env '{payload['env_id']}'; waiting for its env_info."
    t = _topics(agent.state["env_id"])
    have = bool(agent.state.get("names"))
    return (f"sinergym-labeler status:\n"
            f"  env_id: {agent.state['env_id']}\n"
            f"  schema cached: {have} ({len(agent.state.get('names') or [])} names)\n"
            f"  messages labeled: {agent.state.get('labeled', 0)}\n"
            f"  output topic: {t['obs_out']}\n"
            f"  per-zone output: {t['base']}/zone/<zone>/observation{SUFFIX}\n"
            f"Subscribe to the output topic and read variables at the top level, e.g. "
            f"payload['air_temperature_Core_mid'].")
"""


# ──────────────────────────────────────────────────────────────────────────────
# REGISTER — add this block inside _build_catalog() in catalog_agent.py
# ──────────────────────────────────────────────────────────────────────────────
#   code = _load_recipe("sinergym_labeler_agent.py")
#   if code:
#       catalog["sinergym-labeler"] = {
#           "name":         "sinergym-labeler",
#           "type":         "dynamic",
#           "description":  "Republishes Sinergym observations as name-keyed dicts on "
#                           "MQTT (.../observation/labeled) so other agents can read obs "
#                           "by variable name instead of array index. Pure data-plane; "
#                           "no task delegation needed.",
#           "capabilities": ["sinergym", "observation", "labeled_observation",
#                            "mqtt", "schema", "data_plane"],
#           "install":      [],
#           "input_schema": {
#               "env_id": "str — optional, which env to label",
#               "action": "str — optional: status",
#           },
#           "output_schema": {"status": "str"},
#           "poll_interval": 3600,
#           "code":          code,
#       }
#       logger.info("[catalog] Loaded sinergym-labeler recipe")
