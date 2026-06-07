"""
sinergym_schema_agent.py — authoritative knowledge of the Sinergym I/O layout.

Place in catalogue_agents/ and register in catalog_agent.py _build_catalog()
(see REGISTER block at the bottom).

Why this exists
---------------
Raw observation / action arrays on MQTT are just numbers. The bridge knows what
each index means and broadcasts it on the `env_info` topics, but consumers
(other agents, the planner) don't. This agent subscribes to env_info, caches the
full schema, and answers questions about it — so any agent can ask "what is obs
index 12?" or "what are Perimeter_top_ZN_1's obs indices?" instead of guessing.

It is registered with capabilities so the PLANNER can discover it and consult it
(or advise other agents to) before interpreting raw vectors.

What it knows (captured live from env_info, published at each episode start)
---------------------------------------------------------------------------
  * global obs vector: index -> variable-name map, obs_dim
  * action variables, bounds (low/high), action_dim
  * per-zone observation indices + names (what the per-zone slice contains)
  * reward component names
  * info-dict keys (captured from a live observation message)

Control (via @sinergym-schema or planner)
-----------------------------------------
  "obs" / "indices"        -> full index -> variable-name table
  {"action":"zone","zone":"Core_mid"}  -> that zone's obs indices + names
  "actions"                -> action variables + bounds
  "reward"                 -> reward component names
  "info"                   -> info-dict keys
  {"env_id": "..."}        -> set which env to listen to (default officeMedium-multiagent)
  (anything else)          -> a short summary + how to ask

NOTE on timing: env_info is published at each EPISODE START and is not retained,
so this agent must be running when an episode begins. Once captured, the schema
is persisted and survives restarts.
"""

AGENT_CODE = r'''
import asyncio
import json

ENV_ID_DEFAULT = "officeMedium-multiagent"


def _topics(env_id):
    base = f"sinergym/env/{env_id}"
    return {
        "env_info":      f"{base}/env_info",
        "zone_env_info": f"{base}/zone/+/env_info",   # MQTT wildcard
        "obs":           f"{base}/observation",
    }


async def setup(agent):
    env_id = agent.recall("env_id", None) or ENV_ID_DEFAULT
    agent.state["env_id"]     = env_id
    agent.state["global"]     = agent.recall("schema_global", None)
    agent.state["zones"]      = agent.recall("schema_zones", {}) or {}
    agent.state["info_keys"]  = agent.recall("schema_info_keys", None)
    t = _topics(env_id)

    async def on_env_info(payload):
        if not isinstance(payload, dict):
            return
        names = payload.get("obs_variable_names")
        g = {
            "obs_variable_names":    names,
            "action_variable_names": payload.get("action_variable_names"),
            "action_low":            payload.get("action_low"),
            "action_high":           payload.get("action_high"),
            "action_dim":            payload.get("action_dim"),
            "obs_dim":               payload.get("obs_dim"),
            "reward_components":     payload.get("reward_components"),
            "zones":                 payload.get("zones"),
            "n_zones":               payload.get("n_zones"),
        }
        agent.state["global"] = g
        agent.persist("schema_global", g)
        agent.log(f"[schema] captured global env_info: obs_dim={g.get('obs_dim')} "
                  f"action_dim={g.get('action_dim')} zones={g.get('n_zones')}")

    async def on_zone_env_info(payload):
        if not isinstance(payload, dict):
            return
        zone = payload.get("zone")
        if not zone:
            return
        agent.state["zones"][zone] = {
            "zone_obs_indices":      payload.get("zone_obs_indices"),
            "obs_variable_names":    payload.get("obs_variable_names"),
            "action_variable_names": payload.get("action_variable_names"),
            "action_low":            payload.get("action_low"),
            "action_high":           payload.get("action_high"),
        }
        agent.persist("schema_zones", agent.state["zones"])

    async def on_obs(payload):
        if agent.state.get("info_keys"):
            return   # capture info keys once
        if isinstance(payload, dict):
            info = payload.get("info")
            if isinstance(info, dict) and info:
                keys = sorted(info.keys())
                agent.state["info_keys"] = keys
                agent.persist("schema_info_keys", keys)
                agent.log(f"[schema] captured info keys: {keys}")

    agent.subscribe(t["env_info"], on_env_info)
    agent.subscribe(t["zone_env_info"], on_zone_env_info)
    agent.subscribe(t["obs"], on_obs)
    agent.log(f"sinergym-schema ready; listening on {t['env_info']} "
              f"(env_info is emitted at each episode start).")


async def process(agent):
    pass   # request-driven


def _index_table(g):
    names = g.get("obs_variable_names") or []
    if not names:
        return "No obs_variable_names captured yet."
    rows = "\n".join(f"  [{i:2d}] {n}" for i, n in enumerate(names))
    return f"Observation vector ({len(names)} dims) — index -> variable:\n{rows}"


def _zone_table(zinfo, zone):
    names = zinfo.get("obs_variable_names") or []
    idxs  = zinfo.get("zone_obs_indices") or []
    rows = "\n".join(
        f"  slice[{k}] = global obs[{idxs[k] if k < len(idxs) else '?'}] -> {nm}"
        for k, nm in enumerate(names)
    ) or "  (no per-zone names captured)"
    return (f"Per-zone observation layout for {zone}:\n{rows}\n"
            f"Action bounds (htg, clg): low={zinfo.get('action_low')} "
            f"high={zinfo.get('action_high')}")


async def handle_task(agent, payload):
    action = ""
    arg = None
    text = None
    if isinstance(payload, dict):
        action = (payload.get("action") or "").lower().strip()
        arg = payload.get("zone") or payload.get("arg")
        text = payload.get("text") or payload.get("question")
        if payload.get("env_id"):
            agent.state["env_id"] = payload["env_id"]
            agent.persist("env_id", payload["env_id"])
    elif isinstance(payload, str):
        text = payload

    blob = f"{action} {text or ''}".lower()
    g = agent.state.get("global")

    if not g:
        return ("I haven't captured the schema yet. env_info is published at each "
                "episode start and isn't retained, so start (or restart) an episode "
                "while I'm running, then ask again.")

    # per-zone
    if "zone" in blob or arg:
        zone = arg
        if not zone and text:
            for z in (g.get("zones") or []):
                if z.lower() in text.lower():
                    zone = z
                    break
        zinfo = agent.state.get("zones", {}).get(zone) if zone else None
        if not zinfo:
            return (f"No per-zone schema for {zone!r} yet. Known zones: "
                    f"{', '.join(g.get('zones') or []) or '(none)'}. "
                    f"(Per-zone env_info arrives at the next episode start.)")
        return _zone_table(zinfo, zone)

    if "action" in blob:
        return (f"Action variables ({g.get('action_dim')}):\n"
                f"  names: {g.get('action_variable_names')}\n"
                f"  low:   {g.get('action_low')}\n"
                f"  high:  {g.get('action_high')}")

    if "reward" in blob:
        return f"Reward components: {g.get('reward_components')}"

    if "info" in blob:
        ik = agent.state.get("info_keys")
        return (f"info-dict keys: {ik}" if ik else
                "No observation with a populated info dict seen yet — ask again once "
                "the simulation is stepping.")

    if any(k in blob for k in ("obs", "observation", "index", "indices", "layout", "variable")):
        return _index_table(g)

    # default summary
    return (f"Sinergym schema for env '{agent.state.get('env_id')}':\n"
            f"  obs_dim={g.get('obs_dim')}, action_dim={g.get('action_dim')}, "
            f"n_zones={g.get('n_zones')}\n"
            f"  zones: {', '.join(g.get('zones') or [])}\n"
            f"Ask me: 'obs' (index->name table), 'zone <name>', 'actions', 'reward', or 'info'.")
'''


# ──────────────────────────────────────────────────────────────────────────────
# REGISTER — add this block inside _build_catalog() in catalog_agent.py
# ──────────────────────────────────────────────────────────────────────────────
#   code = _load_recipe("sinergym_schema_agent.py")
#   if code:
#       catalog["sinergym-schema"] = {
#           "name":         "sinergym-schema",
#           "type":         "dynamic",
#           "description":  "Authoritative map of the Sinergym I/O layout: which "
#                           "observation index is which variable, per-zone observation "
#                           "indices, action variables and their bounds, reward "
#                           "components, and info-dict keys. Consult this agent before "
#                           "interpreting raw obs/action arrays from MQTT or Fuseki.",
#           "capabilities": ["sinergym", "schema", "observation_layout",
#                            "observation_indices", "action_space", "reward_components",
#                            "introspection", "metadata"],
#           "install":      [],   # stdlib only; learns the schema from env_info
#           "input_schema": {
#               "action": "str — optional: obs | zone | actions | reward | info",
#               "zone":   "str — zone name (with action 'zone')",
#               "text":   "str — or a plain question, e.g. 'what is obs index 12?'",
#               "env_id": "str — optional, which env to listen to",
#           },
#           "output_schema": {"answer": "str — the requested schema info"},
#           "poll_interval": 3600,
#           "code":          code,
#       }
#       logger.info("[catalog] Loaded sinergym-schema recipe")
