"""
Sinergym ↔ Wactorz MQTT Bridge  — MAS edition (v2)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Extends sinergym_bridge.py (v2) to support a per-zone multi-agent setup.

What is new vs v1
─────────────────
  • SinergymFusekiBridge — writes simulation data to Apache Jena Fuseki
    (sinergym dataset) in RDF/Turtle using SOSA/SAREF/custom sgy: ontology.

    Named graphs managed:
      urn:sinergym:catalog       — env metadata (variable names, action bounds)
      urn:sinergym:observations  — per-step global observations (rolling window)
      urn:sinergym:zones         — per-step zone-sliced observations
      urn:sinergym:episodes      — episode summaries (start/end events)

    Environment variables (all optional, sensible defaults):
      FUSEKI_URL       Fuseki base URL          (default: http://localhost:3030)
      FUSEKI_DATASET   Sinergym dataset name    (default: sinergym)
      FUSEKI_USER      Fuseki admin user        (default: admin)
      FUSEKI_PASSWORD  Fuseki admin password    (default: empty = no auth)

  • Zone-sliced observation publishing (unchanged from v1):
      sinergym/env/{env_id}/zone/{zone}/observation  (per zone, every step)

  • Zone-aware action assembly (unchanged from v1).

ZONE OBS LAYOUT  (per-zone slice, e.g. 9 dims for 5zone env)
──────────────────────────────────────────────────────────────
  Shared outdoor block (always included, 3+6 = up to 9 shared dims):
    [0]   month
    [1]   day_of_month
    [2]   hour
    [3]   outdoor_temperature
    [4]   outdoor_humidity
    [5]   wind_speed
    [6]   wind_direction
    [7]   diffuse_solar_radiation
    [8]   direct_solar_radiation

  Zone-local block (5 dims for 5zone env):
    [9]   air_temperature_{zone}
    [10]  air_humidity_{zone}
    [11]  people_occupant_{zone}
    [12]  htg_setpoint_{zone}
    [13]  clg_setpoint_{zone}

  Total per-zone obs dim: 14 (for 5zone; generalises to any zone count).

ZONE ACTION LAYOUT  (2 dims per zone for 5zone env)
────────────────────────────────────────────────────
  [0]  htg_setpoint (normalised [-1,1])
  [1]  clg_setpoint (normalised [-1,1])
  Real bounds are published in zone env_info.

TOPIC SUMMARY
─────────────
  Publishes  →  sinergym/env/{env_id}/observation              (global, every step)
             →  sinergym/env/{env_id}/zone/{zone}/observation  (per zone, every step)
             →  sinergym/env/{env_id}/action_space             (global, startup)
             →  sinergym/env/{env_id}/env_info                 (global, startup + each ep)
             →  sinergym/env/{env_id}/zone/{zone}/env_info     (per zone, startup + each ep)
             →  sinergym/env/{env_id}/episode                  (episode start/end)

  Subscribes ←  sinergym/env/{env_id}/action                  (global, single-agent mode)
             ←  sinergym/env/{env_id}/zone/+/action            (per zone, MAS mode)

  In MAS mode the bridge waits for responses from ALL zone agents before
  stepping.  Zones that do not respond within ZONE_ACTION_TIMEOUT use a
  per-zone fallback (last known action → midpoint on first step).

USAGE
─────
  # Collect with random actions (no agent needed):
  python sinergym_bridge_mas.py --mode collect --episodes 10

  # Deploy with per-zone agents:
  python sinergym_bridge_mas.py --mode deploy --episodes 5

  # Deploy with the original single global optimizer (backward compat):
  python sinergym_bridge_mas.py --mode deploy --episodes 5 --single-agent

  # Use the OfficeMedium 15-zone env:
  python sinergym_bridge_mas.py --env Eplus-officemedium-mixed-continuous-v1 \\
      --zones "Core_bottom,Core_mid,Perimeter_bot_ZN_1" --zone-obs-local 3

  # Disable Fuseki integration:
  python sinergym_bridge_mas.py --mode collect --no-fuseki

WACTORZ SETUP (MAS flow)
─────────────────────────
  1. Start Wactorz broker
  2. Spawn one collector per zone:
       @sinergym-collector-zone-1 {"action":"configure",
         "zone_name":"SPACE1-1","zone_idx":0,"target_episodes":10}
  3. Spawn one optimizer per zone (or one centralised optimizer):
       @sinergym-optimizer-zone-1 {"action":"configure",
         "zone_name":"SPACE1-1","zone_idx":0}
  4. Run bridge in collect mode:
       python sinergym_bridge_mas.py --mode collect --episodes 10
  5. After training, run in deploy mode:
       python sinergym_bridge_mas.py --mode deploy --episodes 5
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import argparse
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone

import gymnasium as gym
import numpy as np
import paho.mqtt.client as mqtt

try:
    import sinergym  # noqa — registers envs
except ImportError:
    print("ERROR: sinergym not installed. pip install sinergym")
    raise


# ── Configuration ──────────────────────────────────────────────────────────────

DEFAULT_ENV_ID           = "Eplus-5zone-hot-continuous-v1"
DEFAULT_BROKER_HOST      = "host.docker.internal"
DEFAULT_BROKER_PORT      = 1883
ACTION_TIMEOUT           = 5.0    # global single-agent timeout (s)
ZONE_ACTION_TIMEOUT      = 4.0    # per-zone agent timeout (s)
FALLBACK_WARN_EVERY      = 500

# Fuseki defaults (overridable via env vars)
FUSEKI_URL      = os.environ.get("FUSEKI_URL",      "http://localhost:3030")
FUSEKI_DATASET  = os.environ.get("FUSEKI_DATASET",  "sinergym")
FUSEKI_USER     = os.environ.get("FUSEKI_USER",     "admin")
FUSEKI_PASSWORD = os.environ.get("FUSEKI_PASSWORD", "")

# ── 5zone obs layout (mirrors ddpg_5zone.py ObsIndex) ─────────────────────────

_5ZONE_ZONES = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]
_EXCLUDED_ZONES = {"PLENUM-1", "plenum-1", "PLENUM", "plenum"}

_SHARED_OBS_INDICES = list(range(9))
_ZONE_BASE        = 9
_ZONE_STRIDE      = 5
_ZONE_LOCAL_COUNT = 5
_TAIL_OBS_INDICES = [34, 35, 36]

_SINGLE_ACT_ZONE_STRIDE = 1
_SINGLE_ACT_ZONE_BASE   = 9
_N_ZONES = len(_5ZONE_ZONES)


def _build_zone_obs_indices(zone_idx: int, obs_dim: int, n_zones: int) -> list[int]:
    multi_agent_end = _ZONE_BASE + n_zones * _ZONE_STRIDE
    if obs_dim >= multi_agent_end:
        base  = _ZONE_BASE + zone_idx * _ZONE_STRIDE
        local = list(range(base, min(base + _ZONE_LOCAL_COUNT, obs_dim)))
        tail  = [i for i in _TAIL_OBS_INDICES if i < obs_dim and i not in local]
        return _SHARED_OBS_INDICES + local + tail
    return list(range(obs_dim))


def _zone_local_obs_by_name(obs_names, zone: str):
    """Resolve a zone's local obs variables BY NAME → [(global_idx, kind_name)].

    Matches obs variables whose name ends with ``_<zone>`` (e.g.
    ``air_temperature_Core_mid`` → idx, kind ``air_temperature``). Robust to '-'/'_'
    differences in zone names (the 5-zone env uses ``SPACE1-1`` in the zone list but
    ``..._SPACE1_1`` in variable names). This replaces the positional/strided slice,
    which silently mis-maps grouped obs layouts like OfficeMedium (all temps, then all
    humidities, …) and corrupted the per-zone RDF predicates in Fuseki.
    """
    if not obs_names:
        return []
    suffixes = {"_" + zone, "_" + zone.replace("-", "_")}
    out = []
    for idx, name in enumerate(obs_names):
        for suf in suffixes:
            if name.endswith(suf):
                out.append((idx, name[: -len(suf)]))
                break
    return out


def _build_zone_action_indices(zone_idx: int, n_zones: int) -> tuple[int, int]:
    return zone_idx, n_zones + zone_idx


# ── MQTT topics ────────────────────────────────────────────────────────────────

def obs_topic(env_id):                return f"sinergym/env/{env_id}/observation"
def action_topic(env_id):             return f"sinergym/env/{env_id}/action"
def episode_topic(env_id):            return f"sinergym/env/{env_id}/episode"
def space_topic(env_id):              return f"sinergym/env/{env_id}/action_space"
def env_info_topic(env_id):           return f"sinergym/env/{env_id}/env_info"
def zone_obs_topic(env_id, zone):     return f"sinergym/env/{env_id}/zone/{zone}/observation"
def zone_action_topic(env_id, zone):  return f"sinergym/env/{env_id}/zone/{zone}/action"


# ── Script-parity metrics (mirror maddpg_v3.evaluate) ───────────────────────────
_M_COMFORT_WINTER  = (20.0, 23.5)
_M_COMFORT_SUMMER  = (23.0, 26.0)
_M_IDX_HVAC_DEMAND = 90
_M_N_OCCUPIED      = 15

def _m_seasonal_comfort(month, day):
    is_summer = (month > 6 or (month == 6 and day >= 1)) and \
                (month < 10 or (month == 9 and day <= 30))
    return _M_COMFORT_SUMMER if is_summer else _M_COMFORT_WINTER

def _m_is_occupied(month, day, hour):
    try:
        from datetime import datetime
        weekend = datetime(2024, int(month), int(day)).weekday() >= 5
    except Exception:
        weekend = False
    return (not weekend) and (7 <= hour <= 19)
def zone_env_info_topic(env_id, zone):return f"sinergym/env/{env_id}/zone/{zone}/env_info"
def zone_wildcard_action(env_id):     return f"sinergym/env/{env_id}/zone/+/action"


# ── Normalisation helpers ──────────────────────────────────────────────────────

def denormalize(action_norm, low, high):
    return low + (action_norm + 1.0) * 0.5 * (high - low)

def normalize(action_real, low, high):
    rng = high - low
    rng = np.where(rng > 0, rng, 1.0)
    return 2.0 * (action_real - low) / rng - 1.0


# ══════════════════════════════════════════════════════════════════════════════
# ── Sinergym → Fuseki RDF bridge ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

# Ontology prefixes
_SGY_NS   = "https://waldiez.github.io/wactorz/sinergym#"
_SSN_NS   = "http://www.w3.org/ns/ssn/"
_SOSA_NS  = "http://www.w3.org/ns/sosa/"
_SAREF_NS = "https://saref.etsi.org/core/"
_XSD_NS   = "http://www.w3.org/2001/XMLSchema#"
_PROV_NS  = "http://www.w3.org/ns/prov#"
_RDFS_NS  = "http://www.w3.org/2000/01/rdf-schema#"

# Named graphs
GRAPH_SGY_CATALOG = "urn:sinergym:catalog"
GRAPH_SGY_OBS     = "urn:sinergym:observations"
GRAPH_SGY_ZONES   = "urn:sinergym:zones"
GRAPH_SGY_EPISODES= "urn:sinergym:episodes"
GRAPH_SGY_HOURLY  = "urn:sinergym:hourly"

# ── Tiered retention policy ───────────────────────────────────────────────────
# FULL resolution: keep every sampled step for the last N episodes
RETENTION_FULL_EPISODES   = 3     # was 5 — fewer full-res episodes in memory
# HOURLY resolution: keep one averaged row per hour for older episodes
# (Sinergym default timestep = 5 min → 12 steps/hour → avg window = 12)
RETENTION_HOURLY_WINDOW   = 12    # steps to average into one hourly record
# DROP: delete zone steps for episodes older than this
RETENTION_MAX_EPISODES    = 10    # was 20 — drop old episodes sooner
# Global obs graph — keep last N steps only
OBS_RETENTION_STEPS       = 5_000 # was 10_000
# Zone Fuseki sampling — store 1 in every N steps (15-min resolution for 5-min timestep)
# MQTT still receives every step; only Fuseki storage is sampled
ZONE_STORE_EVERY_N_STEPS  = 3     # 3× fewer zone triples, negligible info loss

_TTL_PREFIXES = """\
@prefix rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs:  <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:   <http://www.w3.org/2001/XMLSchema#> .
@prefix sosa:  <http://www.w3.org/ns/sosa/> .
@prefix ssn:   <http://www.w3.org/ns/ssn/> .
@prefix saref: <https://saref.etsi.org/core/> .
@prefix prov:  <http://www.w3.org/ns/prov#> .
@prefix sgy:   <https://waldiez.github.io/wactorz/sinergym#> .
"""

_BRIDGE_AGENT_IRI = "<urn:sinergym:bridge>"


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def _dt_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _esc(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
         .replace("\t", "\\t")
    )


def _literal(v) -> str:
    if isinstance(v, bool):
        return f'"{str(v).lower()}"^^xsd:boolean'
    if isinstance(v, int):
        return f'"{v}"^^xsd:integer'
    if isinstance(v, float):
        if v != v:          # NaN guard
            return '"0.0"^^xsd:decimal'
        return f'"{v:.6g}"^^xsd:decimal'
    return f'"{_esc(str(v))}"'


def _ttl(body: str) -> str:
    return _TTL_PREFIXES + "\n" + body


# ── IRI builders ──────────────────────────────────────────────────────────────

def _env_iri(env_id: str) -> str:
    return f"sgy:env_{_safe(env_id)}"

def _obs_iri(env_id: str, ep: int, step: int) -> str:
    return f"sgy:obs_{_safe(env_id)}_ep{ep}_s{step}"

def _zone_obs_iri(env_id: str, zone: str, ep: int, step: int) -> str:
    return f"sgy:zobs_{_safe(env_id)}_{_safe(zone)}_ep{ep}_s{step}"

def _episode_iri(env_id: str, ep: int) -> str:
    return f"sgy:ep_{_safe(env_id)}_{ep}"

def _zone_iri(env_id: str, zone: str) -> str:
    return f"sgy:zone_{_safe(env_id)}_{_safe(zone)}"

def _var_iri(env_id: str, var_name: str, kind: str) -> str:
    return f"sgy:var_{kind}_{_safe(env_id)}_{_safe(var_name)}"

def _agent_iri(name: str) -> str:
    return f"sgy:agent_{_safe(name)}"


# ── Turtle body builders ──────────────────────────────────────────────────────

def _catalog_body(env_id: str, obs_names: list, act_names: list,
                  act_low: list, act_high: list, zones: list, mode: str) -> str:
    env = _env_iri(env_id)
    dt  = _dt_now()
    lines = []

    lines.append(f"{_BRIDGE_AGENT_IRI}")
    lines.append(f"  a prov:SoftwareAgent ;")
    lines.append(f'  rdfs:label "Sinergym-Wactorz MAS Bridge" .')
    lines.append("")

    lines.append(f"{env}")
    lines.append(f"  a sgy:SimulationEnvironment ;")
    lines.append(f'  rdfs:label "{_esc(env_id)}" ;')
    lines.append(f'  sgy:envId "{_esc(env_id)}" ;')
    lines.append(f'  sgy:mode "{_esc(mode)}" ;')
    lines.append(f'  sgy:nZones "{len(zones)}"^^xsd:integer ;')
    for z in zones:
        lines.append(f"  sgy:hasZone {_zone_iri(env_id, z)} ;")
    lines.append(f'  prov:generatedAtTime "{dt}"^^xsd:dateTime ;')
    lines.append(f"  prov:wasAttributedTo {_BRIDGE_AGENT_IRI} .")
    lines.append("")

    # Zone nodes
    for z in zones:
        ziri = _zone_iri(env_id, z)
        lines.append(f"{ziri}")
        lines.append(f"  a sgy:Zone ;")
        lines.append(f'  rdfs:label "{_esc(z)}" ;')
        lines.append(f'  sgy:zoneName "{_esc(z)}" .')
        lines.append("")

    # Observation variable nodes
    for i, name in enumerate(obs_names or []):
        viri = _var_iri(env_id, name, "obs")
        lines.append(f"{viri}")
        lines.append(f"  a sosa:ObservableProperty ;")
        lines.append(f'  rdfs:label "{_esc(name)}" ;')
        lines.append(f'  sgy:index "{i}"^^xsd:integer ;')
        lines.append(f"  sosa:isObservedBy {env} .")
        lines.append("")

    # Action variable nodes with bounds
    for i, name in enumerate(act_names or []):
        viri = _var_iri(env_id, name, "act")
        lo   = act_low[i]  if i < len(act_low)  else 0.0
        hi   = act_high[i] if i < len(act_high) else 1.0
        lines.append(f"{viri}")
        lines.append(f"  a sosa:ActuatableProperty, saref:Property ;")
        lines.append(f'  rdfs:label "{_esc(name)}" ;')
        lines.append(f'  sgy:index "{i}"^^xsd:integer ;')
        lines.append(f'  sgy:actionLow {_literal(lo)} ;')
        lines.append(f'  sgy:actionHigh {_literal(hi)} ;')
        lines.append(f"  sosa:isActuatedBy {env} .")
        lines.append("")

    return "\n".join(lines)


def _obs_body(env_id: str, ep: int, step: int, obs: list, action_norm: list,
              action_real: list, reward: float, done: bool, mode: str,
              reward_custom=None, reward_hourly=None) -> str:
    iri    = _obs_iri(env_id, ep, step)
    ep_iri = _episode_iri(env_id, ep)
    env    = _env_iri(env_id)
    dt     = _dt_now()
    lines  = []

    lines.append(f"{iri}")
    lines.append(f"  a sosa:Observation, sgy:SimulationStep ;")
    lines.append(f"  sosa:madeBySensor {env} ;")
    lines.append(f"  sosa:hasFeatureOfInterest {env} ;")
    lines.append(f'  sgy:episode {_literal(ep)} ;')
    lines.append(f'  sgy:step {_literal(step)} ;')
    # sgy:reward stays the native Sinergym linear (original) reward for
    # backwards compatibility; the two extra rewards are added alongside it.
    lines.append(f'  sgy:reward {_literal(reward)} ;')
    lines.append(f'  sgy:rewardOriginal {_literal(reward)} ;')
    if reward_custom is not None:
        lines.append(f'  sgy:rewardCustom {_literal(float(reward_custom))} ;')
    if reward_hourly is not None:
        lines.append(f'  sgy:rewardHourly {_literal(float(reward_hourly))} ;')
    lines.append(f'  sgy:done {_literal(done)} ;')
    lines.append(f'  sgy:mode "{_esc(mode)}" ;')
    lines.append(f'  sgy:obsVector "{_esc(json.dumps(obs))}" ;')
    lines.append(f'  sgy:actionNorm "{_esc(json.dumps(action_norm))}" ;')
    lines.append(f'  sgy:actionReal "{_esc(json.dumps(action_real))}" ;')
    lines.append(f'  sgy:partOfEpisode {ep_iri} ;')
    lines.append(f'  sosa:resultTime "{dt}"^^xsd:dateTime ;')
    lines.append(f"  prov:wasAttributedTo {_BRIDGE_AGENT_IRI} .")
    lines.append("")

    return "\n".join(lines)


def _var_name_to_predicate(name: str) -> str:
    """
    Convert a Sinergym variable name like 'air_temperature_SPACE1-1'
    to a camelCase RDF predicate: sgy:airTemperature.
    Strips the zone suffix so all zones share the same predicate name.
    """
    for sep in ("_SPACE", "_space", "_Core", "_core", "_Perim", "_perim",
                "_Zone", "_zone", "_PLENUM", "_plenum"):
        if sep in name:
            name = name[:name.index(sep)]
            break
    parts = re.split(r"[_\-\s]+", name.strip())
    if not parts:
        return "obsValue"
    camel = parts[0].lower() + "".join(p.capitalize() for p in parts[1:])
    camel = re.sub(r"[^A-Za-z0-9]", "", camel)
    return camel or "obsValue"


# ── SSN/SOSA property IRIs ────────────────────────────────────────────────────
# Maps camelCase predicate names (from _var_name_to_predicate) to a
# standard ssn:Property IRI and a human label.
_SSN_PROPERTY_MAP: dict[str, tuple[str, str]] = {
    # Observed properties (sensors)
    "airTemperature":        ("sgy:prop_AirTemperature",       "Zone Air Temperature"),
    "airHumidity":           ("sgy:prop_AirHumidity",          "Zone Air Humidity"),
    "peopleOccupant":        ("sgy:prop_Occupancy",            "Zone Occupancy Count"),
    "outdoorTemperature":    ("sgy:prop_OutdoorTemperature",   "Outdoor Dry Bulb Temperature"),
    "outdoorHumidity":       ("sgy:prop_OutdoorHumidity",      "Outdoor Relative Humidity"),
    "windSpeed":             ("sgy:prop_WindSpeed",            "Wind Speed"),
    "windDirection":         ("sgy:prop_WindDirection",        "Wind Direction"),
    "diffuseSolarRadiation": ("sgy:prop_DiffuseSolar",         "Diffuse Solar Radiation"),
    "directSolarRadiation":  ("sgy:prop_DirectSolar",          "Direct Solar Radiation"),
    "month":                 ("sgy:prop_Month",                "Month"),
    "dayOfMonth":            ("sgy:prop_DayOfMonth",           "Day of Month"),
    "hour":                  ("sgy:prop_Hour",                 "Hour of Day"),
    # Actuatable properties (setpoints in obs = current measured value)
    "heatingSetpoint":       ("sgy:prop_HeatingSetpoint",      "Heating Temperature Setpoint"),
    "coolingSetpoint":       ("sgy:prop_CoolingSetpoint",      "Cooling Temperature Setpoint"),
    "heatSetpoint":          ("sgy:prop_HeatingSetpoint",      "Heating Temperature Setpoint"),
    "coolSetpoint":          ("sgy:prop_CoolingSetpoint",      "Cooling Temperature Setpoint"),
}

# Whether a property is an ActuatableProperty (True) or ObservableProperty (False)
_ACTUATABLE_PROPS = {
    "sgy:prop_HeatingSetpoint", "sgy:prop_CoolingSetpoint",
}


def _ssn_property_catalog_body() -> str:
    """
    Emit ssn:Property declarations for the catalog graph.
    Only needs to be written once — describes the property ontology.
    """
    lines = []
    seen = set()
    for pred, (prop_iri, label) in _SSN_PROPERTY_MAP.items():
        if prop_iri in seen:
            continue
        seen.add(prop_iri)
        prop_type = ("sosa:ActuatableProperty, ssn:Property"
                     if prop_iri in _ACTUATABLE_PROPS
                     else "sosa:ObservableProperty, ssn:Property")
        lines.append(f"{prop_iri}")
        lines.append(f"  a {prop_type} ;")
        lines.append(f'  rdfs:label "{label}" .')
        lines.append("")
    return "\n".join(lines)


# Shared outdoor obs indices in the zone slice (indices 0-8 per layout)
# These are identical across all zones each step — stored once in the global
# obs graph, not repeated per zone. Only zone-local dims are stored here.
_OUTDOOR_INDICES = set(range(9))   # month, day, hour, outdoor block


def _zone_obs_body(env_id: str, zone: str, ep: int, step: int,
                   zone_obs: list, zone_act_norm: list, zone_act_real: list,
                   zone_reward: float, global_reward: float, mode: str,
                   zone_obs_names: list = None,
                   zone_act_names: list = None,
                   agent_name: str = None) -> str:
    """
    Build Turtle for one zone observation step — lean flat-scalar design.

    WHAT IS STORED (per zone, per sampled step):
      - Zone-local obs only: airTemperature, airHumidity, peopleOccupant,
        heatSetpoint, coolSetpoint  (~5 dims, indices 9+ in zone slice)
      - Action real values: heatingSetpoint, coolingSetpoint (2 dims)
      - Reward, episode, step, zone metadata

    WHAT IS NOT STORED HERE:
      - Shared outdoor block (indices 0-8): month/hour/weather are identical
        across all zones — already stored in urn:sinergym:observations global
        graph once per step, not repeated 5x per zone.
      - SSN/SOSA linked observation nodes: removed — they added 16 extra IRIs
        per step (8x triple inflation) with no query benefit for dashboards.
        The sgy: scalar predicates are self-describing and query efficiently.

    Triple count per step: ~10 (was ~80 with SSN/SOSA layer).
    """
    step_iri = _zone_obs_iri(env_id, zone, ep, step)
    ep_iri   = _episode_iri(env_id, ep)
    lines    = [
        f"{step_iri}",
        f"  a sgy:ZoneStep ;",
        f'  sgy:zone "{_esc(zone)}" ;',
        f"  sgy:episode {ep} ;",
        f"  sgy:step {step} ;",
        f'  sgy:reward {_literal(zone_reward)} ;',
        f'  sgy:rewardGlobal {_literal(global_reward)} ;',
        f"  sgy:partOfEpisode {ep_iri} ;",
    ]

    # ── Zone-local obs dims only (skip shared outdoor block indices 0-8) ──────
    for i, val in enumerate(zone_obs):
        if i in _OUTDOOR_INDICES:
            continue   # stored once in global obs graph, not repeated per zone
        try:
            fval = float(val)
        except (TypeError, ValueError):
            continue
        var_name = zone_obs_names[i] if (zone_obs_names and i < len(zone_obs_names)) else None
        pred_key = _var_name_to_predicate(var_name) if var_name else f"obs_{i}"
        lines.append(f"  sgy:{pred_key} {_literal(fval)} ;")

    # ── Action dims (real engineering units) ──────────────────────────────────
    for i, val in enumerate(zone_act_real):
        try:
            fval = float(val)
        except (TypeError, ValueError):
            continue
        var_name = zone_act_names[i] if (zone_act_names and i < len(zone_act_names)) else None
        pred_key = _var_name_to_predicate(var_name) if var_name else f"act_{i}"
        lines.append(f"  sgy:action_{pred_key} {_literal(fval)} ;")

    # ── Provenance: attribute this decision to the agent that made it ─────────
    if agent_name:
        lines.append(f"  prov:wasAttributedTo {_agent_iri(agent_name)} ;")

    lines[-1] = lines[-1].rstrip(" ;") + " ."
    lines.append("")
    return "\n".join(lines)


def _episode_start_body(env_id: str, ep: int, mode: str, zones: list) -> str:
    iri = _episode_iri(env_id, ep)
    dt  = _dt_now()
    env = _env_iri(env_id)
    lines = []

    lines.append(f"{iri}")
    lines.append(f"  a sgy:Episode ;")
    lines.append(f'  sgy:episodeNumber {_literal(ep)} ;')
    lines.append(f'  sgy:mode "{_esc(mode)}" ;')
    lines.append(f'  sgy:nZones {_literal(len(zones))} ;')
    lines.append(f"  sgy:ofEnvironment {env} ;")
    lines.append(f'  sgy:startTime "{dt}"^^xsd:dateTime ;')
    lines.append(f"  prov:wasAttributedTo {_BRIDGE_AGENT_IRI} .")
    lines.append("")

    return "\n".join(lines)


def _episode_end_body(env_id: str, ep: int, stats: dict) -> str:
    iri = _episode_iri(env_id, ep)
    dt  = _dt_now()
    lines = []

    # SPARQL Update will DELETE old triples for this IRI and INSERT fresh ones
    lines.append(f"{iri}")
    lines.append(f"  a sgy:Episode ;")
    lines.append(f'  sgy:episodeNumber {_literal(ep)} ;')
    lines.append(f'  sgy:mode "{_esc(stats.get("mode", ""))}" ;')
    lines.append(f'  sgy:steps {_literal(stats.get("steps", 0))} ;')
    lines.append(f'  sgy:totalReward {_literal(stats.get("total_reward", 0.0))} ;')
    lines.append(f'  sgy:meanReward {_literal(stats.get("mean_reward", 0.0))} ;')
    # Three rewards (mirror pymdp v7 reporting): original/native linear,
    # custom (training-shaped) and hourly-schedule linear.
    lines.append(f'  sgy:originalRewardTotal {_literal(stats.get("original_reward", stats.get("total_reward", 0.0)))} ;')
    if stats.get("custom_reward") is not None:
        lines.append(f'  sgy:customRewardTotal {_literal(stats.get("custom_reward", 0.0))} ;')
    if stats.get("hourly_reward") is not None:
        lines.append(f'  sgy:hourlyRewardTotal {_literal(stats.get("hourly_reward", 0.0))} ;')
    if stats.get("comfort_score") is not None:
        lines.append(f'  sgy:comfortScore {_literal(stats.get("comfort_score", 0.0))} ;')
    if stats.get("comfort_score_occ") is not None:
        lines.append(f'  sgy:comfortScoreOcc {_literal(stats.get("comfort_score_occ", 0.0))} ;')
    lines.append(f'  sgy:totalEnergyW {_literal(stats.get("total_energy_W", 0.0))} ;')
    lines.append(f'  sgy:comfortViolations {_literal(stats.get("comfort_violations_degC_steps", 0.0))} ;')
    lines.append(f'  sgy:violationTimesteps {_literal(stats.get("violation_timesteps", 0))} ;')
    lines.append(f'  sgy:durationSeconds {_literal(stats.get("duration_s", 0.0))} ;')
    lines.append(f'  sgy:endTime "{dt}"^^xsd:dateTime ;')
    lines.append(f"  prov:wasAttributedTo {_BRIDGE_AGENT_IRI} .")
    lines.append("")

    return "\n".join(lines)


# ── HTTP client (sync, runs in its own thread) ────────────────────────────────

class SinergymFusekiBridge:
    """
    Writes Sinergym simulation data to Apache Jena Fuseki (sinergym dataset)
    as RDF/Turtle using the Graph Store Protocol and SPARQL Update.

    Runs in a background daemon thread — call push_* methods from the main
    bridge thread; they enqueue work and return immediately so the simulation
    loop is never blocked by network I/O.
    """

    def __init__(
        self,
        fuseki_url: str      = FUSEKI_URL,
        dataset: str         = FUSEKI_DATASET,
        fuseki_user: str     = FUSEKI_USER,
        fuseki_password: str = FUSEKI_PASSWORD,
        enabled: bool        = True,
        batch_size: int      = 20,
    ) -> None:
        self._base    = fuseki_url.rstrip("/")
        self._ds      = dataset
        self._auth    = (fuseki_user, fuseki_password) if fuseki_user else None
        self._enabled = enabled
        self._batch_size = batch_size
        # Unbounded queue — simulation loop never blocks or drops steps
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._step_counter = 0   # tracks total steps for retention pruning

    # ── Public API (called from main thread) ──────────────────────────────────

    def start(self) -> None:
        if not self._enabled:
            return
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="sinergym-fuseki"
        )
        self._thread.start()
        print(f"[Fuseki] Bridge started → {self._base}/{self._ds}")

    def stop(self) -> None:
        if not self._enabled or self._thread is None:
            return
        self._queue.put(None)   # sentinel
        self._thread.join(timeout=10)

    def push_catalog(self, env_id: str, obs_names: list, act_names: list,
                     act_low: list, act_high: list, zones: list, mode: str) -> None:
        if not self._enabled:
            return
        body = _catalog_body(env_id, obs_names, act_names, act_low, act_high, zones, mode)
        # Also embed SSN property declarations in catalog
        ssn_body = _ssn_property_catalog_body()
        self._enqueue(("replace_graph", GRAPH_SGY_CATALOG, _ttl(body + "\n" + ssn_body)))

    def push_obs(self, env_id: str, ep: int, step: int, obs: list,
                 action_norm: list, action_real: list,
                 reward: float, done: bool, mode: str,
                 reward_custom=None, reward_hourly=None) -> None:
        if not self._enabled:
            return
        body = _obs_body(env_id, ep, step, obs, action_norm, action_real,
                         reward, done, mode,
                         reward_custom=reward_custom, reward_hourly=reward_hourly)
        self._enqueue(("append_graph", GRAPH_SGY_OBS, body))
        self._step_counter += 1
        # Prune old observations every OBS_RETENTION_STEPS steps
        if self._step_counter % OBS_RETENTION_STEPS == 0:
            self._enqueue(("trim_obs", GRAPH_SGY_OBS, ep, step))

    def push_zone_obs(self, env_id: str, zone: str, ep: int, step: int,
                      zone_obs: list, zone_act_norm: list, zone_act_real: list,
                      zone_reward: float, global_reward: float, mode: str,
                      zone_obs_names: list = None,
                      zone_act_names: list = None,
                      agent_name: str = None, agent_policy: str = None) -> None:
        if not self._enabled:
            return

        # One-time PROV declaration of the deciding agent (lands in the zones
        # graph alongside the steps; survives trimming since it has no sgy:episode).
        if agent_name:
            declared = getattr(self, "_declared_agents", None)
            if declared is None:
                declared = set()
                self._declared_agents = declared
            if agent_name not in declared:
                declared.add(agent_name)
                a = _agent_iri(agent_name)
                decl = [
                    f"{a}",
                    f"  a prov:SoftwareAgent ;",
                    f'  rdfs:label "{_esc(agent_name)}" ;',
                    f'  sgy:controlsZone "{_esc(zone)}" ;',
                ]
                if agent_policy:
                    decl.append(f'  sgy:policy "{_esc(agent_policy)}" ;')
                decl.append(f"  prov:actedOnBehalfOf {_BRIDGE_AGENT_IRI} .")
                decl.append("")
                self._enqueue(("append_graph", GRAPH_SGY_ZONES, "\n".join(decl)))

        body = _zone_obs_body(env_id, zone, ep, step, zone_obs,
                               zone_act_norm, zone_act_real,
                               zone_reward, global_reward, mode,
                               zone_obs_names=zone_obs_names,
                               zone_act_names=zone_act_names,
                               agent_name=agent_name)
        self._enqueue(("append_graph", GRAPH_SGY_ZONES, body))

    def push_episode_start(self, env_id: str, ep: int, mode: str, zones: list) -> None:
        if not self._enabled:
            return
        body = _episode_start_body(env_id, ep, mode, zones)
        self._enqueue(("append_graph", GRAPH_SGY_EPISODES, body))

    def push_episode_end(self, env_id: str, ep: int, stats: dict) -> None:
        if not self._enabled:
            return
        body = _episode_end_body(env_id, ep, stats)
        self._enqueue(("upsert_episode", GRAPH_SGY_EPISODES, ep, env_id, _ttl(body)))
        # Trigger tiered retention maintenance after every episode
        self._enqueue(("retention", env_id, ep))

    # ── Internal ──────────────────────────────────────────────────────────────

    def _enqueue(self, item) -> None:
        # Unbounded queue — never blocks, never drops
        self._queue.put(item)

    def _gsp_url(self, graph: str) -> str:
        from urllib.parse import quote
        return f"{self._base}/{self._ds}/data?graph={quote(graph, safe='')}"

    def _update_url(self) -> str:
        return f"{self._base}/{self._ds}/update"

    def _put(self, graph: str, ttl: str) -> None:
        """PUT — replace entire named graph."""
        import urllib.request
        data = ttl.encode()
        req  = urllib.request.Request(
            self._gsp_url(graph), data=data, method="PUT",
            headers={"Content-Type": "text/turtle"},
        )
        self._add_auth(req)
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status not in (200, 201, 204):
                print(f"[Fuseki] PUT {graph} → {r.status}")

    def _post_raw(self, graph: str, ttl: str) -> None:
        """POST raw Turtle (already includes prefixes) to named graph."""
        import urllib.request
        data = ttl.encode()
        req  = urllib.request.Request(
            self._gsp_url(graph), data=data, method="POST",
            headers={"Content-Type": "text/turtle"},
        )
        self._add_auth(req)
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status not in (200, 201, 204):
                print(f"[Fuseki] POST {graph} → {r.status}")

    def _post(self, graph: str, ttl: str) -> None:
        """POST full Turtle document to named graph."""
        self._post_raw(graph, ttl)

    def _sparql_update(self, query: str) -> None:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({"update": query}).encode()
        req  = urllib.request.Request(
            self._update_url(), data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self._add_auth(req)
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status not in (200, 204):
                print(f"[Fuseki] SPARQL Update → {r.status}")

    def _add_auth(self, req) -> None:
        if self._auth and self._auth[0]:
            import base64
            creds = base64.b64encode(
                f"{self._auth[0]}:{self._auth[1]}".encode()
            ).decode()
            req.add_header("Authorization", f"Basic {creds}")

    def _upsert_episode(self, graph: str, ep: int, env_id: str, ttl: str) -> None:
        """Delete old triples for this episode IRI then append fresh ones."""
        full_iri = f"urn:sinergym:ep_{_safe(env_id)}_{ep}"
        delete_q = f"""
PREFIX sgy: <{_SGY_NS}>
DELETE {{
  GRAPH <{graph}> {{ <{full_iri}> ?p ?o . }}
}}
WHERE  {{
  GRAPH <{graph}> {{ <{full_iri}> ?p ?o . }}
}}
"""
        self._sparql_update(delete_q)
        self._post(graph, ttl)

    def _trim_obs_graph(self, graph: str, ep: int, step: int) -> None:
        """Keep only last OBS_RETENTION_STEPS steps in global obs graph."""
        if ep <= 1:
            return
        query = f"""
PREFIX sgy: <{_SGY_NS}>
DELETE {{ GRAPH <{graph}> {{ ?obs ?p ?o . }} }}
WHERE  {{ GRAPH <{graph}> {{
    ?obs a sgy:SimulationStep ; sgy:episode ?e ; ?p ?o .
    FILTER(?e < {ep - 1})
}} }}
"""
        self._sparql_update(query)
        print(f"[Fuseki] Trimmed global obs — kept ep {ep-1}+")

    def _run_retention(self, env_id: str, ep: int) -> None:
        """
        Tiered retention for urn:sinergym:zones after each episode ends.

        Policy:
          ep >= current-RETENTION_FULL_EPISODES  → keep every step (full res)
          ep >= current-RETENTION_MAX_EPISODES    → downsample to hourly averages
          ep <  current-RETENTION_MAX_EPISODES    → drop entirely

        Hourly averages are stored in urn:sinergym:hourly as sgy:HourlyAverage
        nodes with AVG() of all scalar properties over RETENTION_HOURLY_WINDOW
        consecutive steps. They are never re-averaged — written once and kept.
        """
        if ep <= RETENTION_FULL_EPISODES:
            return   # nothing to do yet

        full_threshold  = ep - RETENTION_FULL_EPISODES    # keep full below this
        drop_threshold  = ep - RETENTION_MAX_EPISODES     # drop below this

        print(f"[Fuseki] Retention: ep={ep}  full≥{full_threshold}  "
              f"hourly={drop_threshold}..{full_threshold}  drop<{drop_threshold}")

        # ── Step 1: drop very old episodes entirely ───────────────────────────
        if drop_threshold > 0:
            drop_q = f"""
PREFIX sgy: <{_SGY_NS}>
PREFIX sosa: <{_SOSA_NS}>
DELETE {{ GRAPH <{GRAPH_SGY_ZONES}> {{ ?s ?p ?o . }} }}
WHERE  {{ GRAPH <{GRAPH_SGY_ZONES}> {{
    ?s sgy:episode ?ep ; ?p ?o .
    FILTER(?ep < {drop_threshold})
}} }}
"""
            self._sparql_update(drop_q)
            print(f"[Fuseki] Dropped zone steps for ep < {drop_threshold}")

        # ── Step 2: downsample intermediate episodes to hourly averages ───────
        # For each episode in [drop_threshold, full_threshold), query all
        # ZoneStep IRIs, group into windows of RETENTION_HOURLY_WINDOW steps,
        # compute AVG of key scalars, write to GRAPH_SGY_HOURLY, then delete
        # the original full-res steps from GRAPH_SGY_ZONES.
        #
        # We do this via SPARQL CONSTRUCT — fetch the data, compute averages
        # in Python (SPARQL AVG over large ranges is slow), write hourly TTL.

        for target_ep in range(max(1, drop_threshold), full_threshold):
            self._downsample_episode(env_id, target_ep)

    def _downsample_episode(self, env_id: str, ep: int) -> None:
        """
        Downsample one episode from full-res (every step) to hourly averages.
        Skips if already downsampled (hourly records already exist for this ep).
        """
        import urllib.request, urllib.parse, json as _json

        # Check if already downsampled
        check_q = f"""
PREFIX sgy: <{_SGY_NS}>
ASK {{ GRAPH <{GRAPH_SGY_HOURLY}> {{
    ?h a sgy:HourlyAverage ; sgy:episode {ep} .
}} }}
"""
        try:
            data = urllib.parse.urlencode({"query": check_q}).encode()
            req  = urllib.request.Request(
                f"{self._base}/{self._ds}/sparql", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Accept": "application/sparql-results+json"},
            )
            self._add_auth(req)
            with urllib.request.urlopen(req, timeout=30) as r:
                result = _json.loads(r.read())
            if result.get("boolean", False):
                # Already downsampled — just delete the full-res steps
                self._delete_episode_zones(ep)
                return
        except Exception as exc:
            print(f"[Fuseki] Downsample check failed ep={ep}: {exc}")
            return

        # Fetch all ZoneStep scalars for this episode
        fetch_q = f"""
PREFIX sgy: <{_SGY_NS}>
SELECT ?zone ?step ?airTemperature ?outdoorTemperature
       ?action_heatingSetpoint ?action_coolingSetpoint
       ?reward ?hour
WHERE {{
  GRAPH <{GRAPH_SGY_ZONES}> {{
    ?s a sgy:ZoneStep ;
       sgy:episode {ep} ;
       sgy:zone ?zone ;
       sgy:step ?step ;
       sgy:reward ?reward .
    OPTIONAL {{ ?s sgy:airTemperature ?airTemperature . }}
    OPTIONAL {{ ?s sgy:outdoorTemperature ?outdoorTemperature . }}
    OPTIONAL {{ ?s sgy:action_heatingSetpoint ?action_heatingSetpoint . }}
    OPTIONAL {{ ?s sgy:action_coolingSetpoint ?action_coolingSetpoint . }}
    OPTIONAL {{ ?s sgy:hour ?hour . }}
  }}
}}
ORDER BY ?zone ?step
"""
        try:
            data = urllib.parse.urlencode({"query": fetch_q}).encode()
            req  = urllib.request.Request(
                f"{self._base}/{self._ds}/sparql", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Accept": "application/sparql-results+json"},
            )
            self._add_auth(req)
            with urllib.request.urlopen(req, timeout=60) as r:
                rows = _json.loads(r.read()).get("results", {}).get("bindings", [])
        except Exception as exc:
            print(f"[Fuseki] Downsample fetch failed ep={ep}: {exc}")
            return

        if not rows:
            return

        # Group by zone → windows of RETENTION_HOURLY_WINDOW steps → average
        from collections import defaultdict
        by_zone: dict = defaultdict(list)
        for row in rows:
            zone = row.get("zone", {}).get("value", "unknown")
            by_zone[zone].append({
                "step":    int(row.get("step",    {}).get("value", 0)),
                "reward":  float(row.get("reward", {}).get("value", 0.0)),
                "airTemp": float(row.get("airTemperature",          {}).get("value", 0.0)) if "airTemperature"          in row else None,
                "outTemp": float(row.get("outdoorTemperature",      {}).get("value", 0.0)) if "outdoorTemperature"      in row else None,
                "htg":     float(row.get("action_heatingSetpoint",  {}).get("value", 0.0)) if "action_heatingSetpoint"  in row else None,
                "clg":     float(row.get("action_coolingSetpoint",  {}).get("value", 0.0)) if "action_coolingSetpoint"  in row else None,
                "hour":    float(row.get("hour",   {}).get("value", 0.0)) if "hour"   in row else None,
            })

        ttl_lines = []
        window = RETENTION_HOURLY_WINDOW

        for zone, steps in by_zone.items():
            steps.sort(key=lambda r: r["step"])
            for w_start in range(0, len(steps), window):
                window_steps = steps[w_start:w_start + window]
                if not window_steps:
                    continue
                mid_step = window_steps[len(window_steps) // 2]["step"]

                def _avg(key):
                    vals = [s[key] for s in window_steps if s.get(key) is not None]
                    return round(sum(vals) / len(vals), 4) if vals else None

                avg_reward  = _avg("reward")
                avg_airtemp = _avg("airTemp")
                avg_outtemp = _avg("outTemp")
                avg_htg     = _avg("htg")
                avg_clg     = _avg("clg")
                avg_hour    = _avg("hour")
                h_iri       = f"sgy:hourly_{_safe(env_id)}_{_safe(zone)}_ep{ep}_w{w_start}"

                ttl_lines.append(f"{h_iri}")
                ttl_lines.append(f"  a sgy:HourlyAverage ;")
                ttl_lines.append(f'  sgy:zone "{_esc(zone)}" ;')
                ttl_lines.append(f"  sgy:episode {ep} ;")
                ttl_lines.append(f"  sgy:stepStart {window_steps[0]['step']} ;")
                ttl_lines.append(f"  sgy:stepEnd {window_steps[-1]['step']} ;")
                ttl_lines.append(f"  sgy:stepMid {mid_step} ;")
                ttl_lines.append(f"  sgy:windowSize {len(window_steps)} ;")
                if avg_reward  is not None: ttl_lines.append(f"  sgy:avgReward {_literal(avg_reward)} ;")
                if avg_airtemp is not None: ttl_lines.append(f"  sgy:airTemperature {_literal(avg_airtemp)} ;")
                if avg_outtemp is not None: ttl_lines.append(f"  sgy:outdoorTemperature {_literal(avg_outtemp)} ;")
                if avg_htg     is not None: ttl_lines.append(f"  sgy:action_heatingSetpoint {_literal(avg_htg)} ;")
                if avg_clg     is not None: ttl_lines.append(f"  sgy:action_coolingSetpoint {_literal(avg_clg)} ;")
                if avg_hour    is not None: ttl_lines.append(f"  sgy:hour {_literal(avg_hour)} ;")
                ttl_lines[-1] = ttl_lines[-1].rstrip(" ;") + " ."
                ttl_lines.append("")

        if ttl_lines:
            combined = _TTL_PREFIXES + "\n" + "\n".join(ttl_lines)
            try:
                self._post_raw(GRAPH_SGY_HOURLY, combined)
                print(f"[Fuseki] Hourly averages written ep={ep} zone_count={len(by_zone)}")
            except Exception as exc:
                print(f"[Fuseki] Hourly write failed ep={ep}: {exc}")
                return

        # Delete the full-res steps now that hourly is written
        self._delete_episode_zones(ep)

    def _delete_episode_zones(self, ep: int) -> None:
        """Delete all ZoneStep triples for a specific episode."""
        # Also delete linked SSN/SOSA observation nodes
        delete_q = f"""
PREFIX sgy: <{_SGY_NS}>
DELETE {{ GRAPH <{GRAPH_SGY_ZONES}> {{ ?s ?p ?o . }} }}
WHERE  {{ GRAPH <{GRAPH_SGY_ZONES}> {{
    ?s sgy:episode {ep} ; ?p ?o .
}} }}
"""
        self._sparql_update(delete_q)
        print(f"[Fuseki] Deleted full-res zone steps ep={ep}")

    def _worker(self) -> None:
        """
        Background thread: drain the queue and write to Fuseki.

        append_graph items are batched — we accumulate up to batch_size TTL
        bodies per named graph and POST them in a single request.
        This eliminates the one-HTTP-round-trip-per-step bottleneck and
        ensures no steps are dropped even at high simulation speeds.
        """
        print(f"[Fuseki] Worker thread running (batch_size={self._batch_size}).")
        pending: dict[str, list[str]] = {}   # graph → [ttl_body, ...]

        def flush(graph: str) -> None:
            if not pending.get(graph):
                return
            combined = _TTL_PREFIXES + "\n" + "\n".join(pending[graph])
            try:
                self._post_raw(graph, combined)
            except Exception as exc:
                print(f"[Fuseki] Batch write error ({graph}): {exc}")
            pending[graph] = []

        def flush_all() -> None:
            for g in list(pending.keys()):
                flush(g)

        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                flush_all()   # flush on idle
                continue

            if item is None:
                flush_all()
                print("[Fuseki] Worker thread stopping.")
                break

            try:
                op = item[0]
                if op == "replace_graph":
                    _, graph, ttl = item
                    flush(graph)      # flush pending before replacing
                    self._put(graph, ttl)
                elif op == "append_graph":
                    _, graph, body = item   # body is TTL without prefixes
                    pending.setdefault(graph, []).append(body)
                    if len(pending[graph]) >= self._batch_size:
                        flush(graph)
                elif op == "upsert_episode":
                    _, graph, ep, env_id, ttl = item
                    flush(graph)
                    self._upsert_episode(graph, ep, env_id, ttl)
                elif op == "trim_obs":
                    _, graph, ep, step = item
                    flush_all()
                    self._trim_obs_graph(graph, ep, step)
                elif op == "retention":
                    _, env_id, ep = item
                    flush_all()
                    self._run_retention(env_id, ep)
            except Exception as exc:
                print(f"[Fuseki] Write error ({item[0]}): {exc}")

            self._queue.task_done()


# ══════════════════════════════════════════════════════════════════════════════
# ── Main bridge (unchanged logic, Fuseki hooks added) ────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class SinergymBridgeMAS:
    def __init__(
        self,
        env_id:       str   = DEFAULT_ENV_ID,
        broker_host:  str   = DEFAULT_BROKER_HOST,
        broker_port:  int   = DEFAULT_BROKER_PORT,
        mode:         str   = "collect",
        n_episodes:   int   = 10,
        render:       bool  = False,
        zones:        list  = None,
        single_agent: bool  = False,
        zone_obs_local: int = None,
        fuseki_enabled: bool = True,
        fuseki_url:   str   = FUSEKI_URL,
        fuseki_dataset: str = FUSEKI_DATASET,
        fuseki_user:  str   = FUSEKI_USER,
        fuseki_password: str = FUSEKI_PASSWORD,
        inject_anomalies: bool = False,
        anomaly_seed: int = 5,
    ):
        self.env_id       = env_id
        self.broker_host  = broker_host
        self.broker_port  = broker_port
        self.mode         = mode
        self.n_episodes   = n_episodes
        self.render       = render
        self.single_agent = single_agent
        self.inject_anomalies = inject_anomalies
        self.anomaly_seed     = anomaly_seed

        self.zones: list[str]          = zones or []
        self.n_zones: int              = 0
        self.zone_obs_indices: dict    = {}
        self.zone_action_indices: dict = {}
        self._zone_local_kind: dict    = {}   # zone → {global_obs_idx: kind_name}
        self.zone_obs_local_override   = zone_obs_local

        self._act_low:  np.ndarray | None = None
        self._act_high: np.ndarray | None = None

        self._obs_variable_names:    list[str] | None = None
        self._action_variable_names: list[str] | None = None
        self._reward_component_names: list[str]       = []

        self._zone_queues: dict[str, queue.Queue] = {}
        self._zone_last_action: dict[str, np.ndarray] = {}
        self._zone_agent: dict[str, dict] = {}   # zone -> {agent, policy} for PROV
        self._global_action_queue: queue.Queue = queue.Queue(maxsize=1)

        self._mqtt         = None
        self._total_steps  = 0
        self._fallback_steps = 0

        # Fuseki bridge (runs in background thread)
        self._fuseki = SinergymFusekiBridge(
            fuseki_url=fuseki_url,
            dataset=fuseki_dataset,
            fuseki_user=fuseki_user,
            fuseki_password=fuseki_password,
            enabled=fuseki_enabled,
        )

    # ── Zone setup ─────────────────────────────────────────────────────────────

    def _setup_zones(self, env: gym.Env):
        obs_dim = env.observation_space.shape[0]
        act_dim = env.action_space.shape[0] if hasattr(env.action_space, 'shape') else 0

        if not self.zones:
            unwrapped = env.unwrapped
            detected  = getattr(unwrapped, 'zone_names', None) or \
                        getattr(unwrapped, 'zones',      None)
            if detected:
                self.zones = [str(z) for z in detected]
            else:
                if self._action_variable_names:
                    seen = []
                    for name in self._action_variable_names:
                        parts = name.split("_", 1)
                        if len(parts) == 2 and parts[1] not in seen:
                            seen.append(parts[1])
                    if seen:
                        self.zones = seen
                if not self.zones:
                    self.zones = _5ZONE_ZONES

        excluded = [z for z in self.zones if z in _EXCLUDED_ZONES]
        if excluded:
            print(f"[ZONES] Excluding non-HVAC zones: {excluded}")
            self.zones = [z for z in self.zones if z not in _EXCLUDED_ZONES]
        if not self.zones:
            print("[ZONES] WARNING: all zones excluded — falling back to 5zone defaults")
            self.zones = list(_5ZONE_ZONES)

        self.n_zones = len(self.zones)

        n_local_dims    = self.zone_obs_local_override or _ZONE_LOCAL_COUNT
        local_block_end = _ZONE_BASE + self.n_zones * n_local_dims
        has_5zone_layout = (
            obs_dim >= local_block_end and
            obs_dim <= local_block_end + len(_TAIL_OBS_INDICES) + 1
        )

        if act_dim == self.n_zones * 2:
            action_layout = "per_zone"
        elif act_dim == 2:
            action_layout = "shared"
            print(f"[ZONES] act_dim=2 detected — standard single-actuator env. "
                  f"All zones share the same action indices (0, 1).")
        else:
            action_layout = "strided"
            print(f"[ZONES] act_dim={act_dim} does not match n_zones*2={self.n_zones*2}. "
                  f"Using strided split.")

        for i, zone in enumerate(self.zones):
            # Prefer name-based slicing — correct for any obs layout. Fall back to the
            # positional/strided heuristics only when variable names are unavailable.
            named = _zone_local_obs_by_name(self._obs_variable_names, zone)
            if named:
                local_idx = [idx for idx, _ in named]
                self.zone_obs_indices[zone] = _SHARED_OBS_INDICES + local_idx
                self._zone_local_kind[zone] = {idx: kind for idx, kind in named}
            elif has_5zone_layout:
                self.zone_obs_indices[zone] = _build_zone_obs_indices(i, obs_dim, self.n_zones)
            else:
                total_local    = obs_dim - len(_SHARED_OBS_INDICES)
                per_zone_local = max(1, total_local // self.n_zones)
                base           = len(_SHARED_OBS_INDICES) + i * per_zone_local
                local          = list(range(base, min(base + per_zone_local, obs_dim)))
                self.zone_obs_indices[zone] = _SHARED_OBS_INDICES + local

            if action_layout == "per_zone":
                self.zone_action_indices[zone] = _build_zone_action_indices(i, self.n_zones)
            elif action_layout == "shared":
                self.zone_action_indices[zone] = (0, 1)
            else:
                base     = (i * 2) % act_dim
                clg_base = (base + 1) % act_dim
                self.zone_action_indices[zone] = (base, clg_base)

            htg_idx = min(self.zone_action_indices[zone][0], act_dim - 1)
            clg_idx = min(self.zone_action_indices[zone][1], act_dim - 1)
            self.zone_action_indices[zone] = (htg_idx, clg_idx)

            self._zone_queues[zone]      = queue.Queue(maxsize=1)
            htg_mid = float((self._act_low[htg_idx] + self._act_high[htg_idx]) / 2)
            clg_mid = float((self._act_low[clg_idx] + self._act_high[clg_idx]) / 2)
            self._zone_last_action[zone] = np.array([htg_mid, clg_mid], dtype=np.float32)

        print(f"\n[ZONES] Detected {self.n_zones} zones | obs_dim={obs_dim} act_dim={act_dim} "
              f"layout={action_layout}")
        for zone in self.zones:
            oi = self.zone_obs_indices[zone]
            ai = self.zone_action_indices[zone]
            print(f"  {zone}: obs_indices=[{oi[0]}..{oi[-1]}] ({len(oi)} dims)  "
                  f"action_indices={ai}")
        if action_layout == "shared":
            print(f"\n  [NOTE] Standard env with 2 global actuators. All zones share action (0,1).")

    # ── MQTT ───────────────────────────────────────────────────────────────────

    def _setup_mqtt(self):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        def on_connect(c, userdata, flags, rc, props):
            if rc != 0:
                print(f"[MQTT] Connection failed: rc={rc}")
                return
            c.subscribe(action_topic(self.env_id))
            c.subscribe(zone_wildcard_action(self.env_id))
            print(f"[MQTT] Connected. Subscribed to global + per-zone action topics.")

        def on_message(c, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode())
            except Exception as e:
                print(f"[MQTT] Bad payload: {e}")
                return

            topic = msg.topic

            if "/zone/" in topic and topic.endswith("/action"):
                parts = topic.split("/")
                try:
                    zone_name = parts[-2]
                except IndexError:
                    return
                action = payload.get("action")
                if action is not None and zone_name in self._zone_queues:
                    # Record who decided this action (for PROV); harmless if absent.
                    self._zone_agent[zone_name] = {
                        "agent":  payload.get("agent"),
                        "policy": payload.get("policy"),
                    }
                    q = self._zone_queues[zone_name]
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    q.put_nowait(action)
                return

            if topic == action_topic(self.env_id):
                action = payload.get("action")
                if action is not None:
                    try:
                        self._global_action_queue.get_nowait()
                    except queue.Empty:
                        pass
                    self._global_action_queue.put_nowait(action)

        def on_disconnect(c, userdata, flags, rc, props):
            if rc != 0:
                print(f"[MQTT] Unexpected disconnect rc={rc}")

        client.on_connect    = on_connect
        client.on_message    = on_message
        client.on_disconnect = on_disconnect
        client.connect(self.broker_host, self.broker_port, keepalive=60)
        client.loop_start()
        return client

    def _publish(self, topic, payload):
        if self._mqtt:
            self._mqtt.publish(topic, json.dumps(payload), qos=0)

    # ── Env metadata ───────────────────────────────────────────────────────────

    def _extract_variable_names(self, env: gym.Env):
        unwrapped = env.unwrapped
        for attr in ("observation_variables", "obs_var_names",
                     "observation_space_names", "variables"):
            val = getattr(unwrapped, attr, None)
            if val is not None:
                try:
                    self._obs_variable_names = list(val); break
                except Exception:
                    pass
        for attr in ("action_variables", "action_var_names",
                     "action_space_names", "action_names"):
            val = getattr(unwrapped, attr, None)
            if val is not None:
                try:
                    self._action_variable_names = list(val); break
                except Exception:
                    pass

    def _extract_reward_components(self, info: dict):
        if self._reward_component_names or not info:
            return
        EXCLUDE = {"time_elapsed(hours)", "month", "day", "hour", "minute",
                   "is_raining", "timestep", "action", "year", "day_of_month",
                   "day_of_week", "weekday"}
        KEYWORDS = ("energy", "comfort", "power", "demand", "penalty", "term",
                    "violation", "reward", "emission", "co2", "hvac", "temperature_viol")
        comps = [k for k, v in info.items()
                 if k not in EXCLUDE and isinstance(v, (int, float))
                 and any(kw in k.lower() for kw in KEYWORDS)]
        self._reward_component_names = comps
        if comps:
            print(f"[ENV] reward components: {comps}")

    def _publish_action_space(self):
        self._publish(space_topic(self.env_id), {
            "low":  self._act_low.tolist(),
            "high": self._act_high.tolist(),
            "note": "Bridge normalises optimizer [-1,1] to these real bounds",
        })

    def _publish_env_info(self):
        obs_dim    = len(self._obs_variable_names) if self._obs_variable_names else None
        action_dim = len(self._act_low)

        self._publish(env_info_topic(self.env_id), {
            "env_id":                self.env_id,
            "obs_variable_names":    self._obs_variable_names,
            "action_variable_names": self._action_variable_names,
            "action_low":            self._act_low.tolist(),
            "action_high":           self._act_high.tolist(),
            "action_dim":            action_dim,
            "obs_dim":               obs_dim,
            "reward_components":     self._reward_component_names,
            "mode":                  self.mode,
            "zones":                 self.zones,
            "n_zones":               self.n_zones,
        })

        for zone in self.zones:
            oi          = self.zone_obs_indices.get(zone, [])
            htg_idx, clg_idx = self.zone_action_indices.get(zone, (0, 1))

            zone_obs_names = None
            if self._obs_variable_names:
                zone_obs_names = [
                    self._obs_variable_names[i]
                    for i in oi if i < len(self._obs_variable_names)
                ]
            zone_act_names = None
            if self._action_variable_names:
                zone_act_names = [
                    self._action_variable_names[j]
                    for j in (htg_idx, clg_idx)
                    if j < len(self._action_variable_names)
                ]

            self._publish(zone_env_info_topic(self.env_id, zone), {
                "env_id":                self.env_id,
                "zone":                  zone,
                "zone_obs_indices":      oi,
                "obs_dim":               len(oi),
                "obs_variable_names":    zone_obs_names,
                "action_variable_names": zone_act_names,
                "action_low":            [float(self._act_low[htg_idx]),
                                          float(self._act_low[clg_idx])],
                "action_high":           [float(self._act_high[htg_idx]),
                                          float(self._act_high[clg_idx])],
                "action_dim":            2,
                "reward_components":     self._reward_component_names,
                "mode":                  self.mode,
                "global_action_low":     self._act_low.tolist(),
                "global_action_high":    self._act_high.tolist(),
            })

    # ── Zone obs slicing ───────────────────────────────────────────────────────

    def _slice_zone_obs(self, obs: np.ndarray, zone: str) -> np.ndarray:
        indices = self.zone_obs_indices.get(zone, list(range(len(obs))))
        return obs[indices]

    def _publish_zone_observations(
        self,
        obs: np.ndarray,
        action_real: np.ndarray,
        action_norm: np.ndarray,
        reward: float,
        done: bool,
        info_serial: dict,
        step: int,
        ep: int,
    ):
        for zone in self.zones:
            zone_obs         = self._slice_zone_obs(obs, zone)
            htg_idx, clg_idx = self.zone_action_indices[zone]

            zone_act_real = np.array([
                float(action_real[htg_idx]),
                float(action_real[clg_idx]),
            ], dtype=np.float32)
            zone_act_norm = np.array([
                float(action_norm[htg_idx]),
                float(action_norm[clg_idx]),
            ], dtype=np.float32)

            zone_reward = reward / max(self.n_zones, 1)
            if info_serial:
                for key_candidate in (
                    f"temperature_violation_{zone}",
                    f"comfort_{zone}",
                    f"zone_comfort_{zone}",
                ):
                    if key_candidate in info_serial:
                        zone_reward = float(info_serial[key_candidate])
                        break

            # Build zone-specific variable name subsets for Fuseki
            oi = self.zone_obs_indices.get(zone, [])
            zone_obs_names = None
            if self._obs_variable_names:
                # Use the normalized per-zone kind names (air_temperature, htg_setpoint, …)
                # for local dims so RDF predicates are clean (sgy:airTemperature) and not
                # zone-baked/mis-mapped; shared/outdoor dims keep their raw names (the RDF
                # writer skips them anyway).
                kindmap = self._zone_local_kind.get(zone, {})
                zone_obs_names = [
                    kindmap.get(i, self._obs_variable_names[i])
                    for i in oi if i < len(self._obs_variable_names)
                ]
            zone_act_names = None
            if self._action_variable_names:
                zone_act_names = [
                    self._action_variable_names[j]
                    for j in (htg_idx, clg_idx)
                    if j < len(self._action_variable_names)
                ]

            self._publish(zone_obs_topic(self.env_id, zone), {
                "obs":          zone_obs.tolist(),
                "action":       zone_act_norm.tolist(),
                "action_real":  zone_act_real.tolist(),
                "reward":       float(zone_reward),
                "reward_global": float(reward),
                "done":         bool(done),
                "info":         info_serial,
                "step":         step,
                "episode":      ep,
                "env_id":       self.env_id,
                "zone":         zone,
                "mode":         self.mode,
            })

            # ── Fuseki: zone observation (sampled every ZONE_STORE_EVERY_N_STEPS) ──
            if step % ZONE_STORE_EVERY_N_STEPS == 0:
                _ag = self._zone_agent.get(zone, {})
                self._fuseki.push_zone_obs(
                    env_id=self.env_id,
                    zone=zone,
                    ep=ep,
                    step=step,
                    zone_obs=zone_obs.tolist(),
                    zone_act_norm=zone_act_norm.tolist(),
                    zone_act_real=zone_act_real.tolist(),
                    zone_reward=float(zone_reward),
                    global_reward=float(reward),
                    mode=self.mode,
                    zone_obs_names=zone_obs_names,
                    zone_act_names=zone_act_names,
                    agent_name=_ag.get("agent"),
                    agent_policy=_ag.get("policy"),
                )

    # ── Action assembly ────────────────────────────────────────────────────────

    def _get_action_collect(self, env: gym.Env) -> tuple[np.ndarray, np.ndarray]:
        action_real = env.action_space.sample()
        action_norm = normalize(action_real, self._act_low, self._act_high)
        return action_real, action_norm

    def _get_action_global(self) -> tuple[np.ndarray, np.ndarray]:
        mid = (self._act_low + self._act_high) / 2.0
        try:
            action_norm_list = self._global_action_queue.get(timeout=ACTION_TIMEOUT)
            action_norm      = np.clip(
                np.array(action_norm_list, dtype=np.float32), -1.0, 1.0
            )
            action_real = np.clip(
                denormalize(action_norm, self._act_low, self._act_high),
                self._act_low, self._act_high,
            )
            if self._fallback_steps > 0:
                print(f"[ACTION] Global optimizer reconnected after {self._fallback_steps} fallback steps.")
                self._fallback_steps = 0
            return action_real, action_norm
        except queue.Empty:
            self._fallback_steps += 1
            if self._fallback_steps == 1:
                print(
                    f"\n[WARNING] No global action after {ACTION_TIMEOUT}s — midpoint fallback.\n"
                    f"  Check @sinergym-optimizer status (phase must be 'deploying')\n"
                )
            elif self._fallback_steps % FALLBACK_WARN_EVERY == 0:
                print(f"[WARNING] Still no global action — {self._fallback_steps} fallback steps.")
            action_norm = normalize(mid, self._act_low, self._act_high)
            return mid.copy(), action_norm

    def _get_action_mas(self) -> tuple[np.ndarray, np.ndarray]:
        action_real = np.zeros(len(self._act_low), dtype=np.float32)
        action_norm = np.zeros(len(self._act_low), dtype=np.float32)

        zone_results: dict[str, tuple] = {}
        lock = threading.Lock()

        def collect_zone(zone):
            q = self._zone_queues[zone]
            htg_idx, clg_idx = self.zone_action_indices[zone]
            lo = np.array([self._act_low[htg_idx],  self._act_low[clg_idx]],  dtype=np.float32)
            hi = np.array([self._act_high[htg_idx], self._act_high[clg_idx]], dtype=np.float32)
            mid = (lo + hi) / 2.0

            try:
                zone_norm_list = q.get(timeout=ZONE_ACTION_TIMEOUT)
                zone_norm = np.clip(np.array(zone_norm_list, dtype=np.float32), -1.0, 1.0)
                zone_real = np.clip(denormalize(zone_norm, lo, hi), lo, hi)
                with lock:
                    self._zone_last_action[zone] = zone_real.copy()
                zone_results[zone] = (zone_real, zone_norm)
            except queue.Empty:
                with lock:
                    fallback_real = self._zone_last_action.get(zone, mid.copy())
                fallback_norm = normalize(fallback_real, lo, hi)
                zone_results[zone] = (fallback_real, fallback_norm)
                print(f"[WARN] Zone '{zone}' timed out — using last known action "
                      f"(htg={fallback_real[0]:.2f}, clg={fallback_real[1]:.2f})")

        threads = [threading.Thread(target=collect_zone, args=(z,), daemon=True)
                   for z in self.zones]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=ZONE_ACTION_TIMEOUT + 0.5)

        fallback_count = 0
        for zone in self.zones:
            htg_idx, clg_idx = self.zone_action_indices[zone]
            if zone not in zone_results:
                lo_h = self._act_low[htg_idx];  hi_h = self._act_high[htg_idx]
                lo_c = self._act_low[clg_idx];  hi_c = self._act_high[clg_idx]
                action_real[htg_idx] = (lo_h + hi_h) / 2.0
                action_real[clg_idx] = (lo_c + hi_c) / 2.0
                action_norm[htg_idx] = 0.0
                action_norm[clg_idx] = 0.0
                fallback_count += 1
                continue
            zone_real, zone_norm = zone_results[zone]
            action_real[htg_idx] = zone_real[0]
            action_real[clg_idx] = zone_real[1]
            action_norm[htg_idx] = zone_norm[0]
            action_norm[clg_idx] = zone_norm[1]

        if fallback_count:
            self._fallback_steps += fallback_count
            if self._fallback_steps % FALLBACK_WARN_EVERY == 0:
                print(f"[WARNING] {self._fallback_steps} cumulative zone-level fallback steps.")

        return action_real, action_norm

    def _get_action(self, env: gym.Env) -> tuple[np.ndarray, np.ndarray]:
        if self.mode == "collect":
            return self._get_action_collect(env)
        if self.single_agent:
            return self._get_action_global()
        return self._get_action_mas()

    # ── Episode ────────────────────────────────────────────────────────────────

    def _run_episode(self, env: gym.Env, ep: int) -> dict:
        obs, info = env.reset()
        # Sinergym returns a placeholder observation from reset(); the first REAL
        # obs arrives on a subsequent step(), but exactly when can vary (sometimes
        # 1 step, sometimes a few) while EnergyPlus spins up. Step with a neutral
        # in-bounds action until the obs is real (valid month). Do NOT re-reset
        # (that starts a new episode and never reads the ready data).
        sp = env.action_space
        try:
            neutral = ((np.asarray(sp.low) + np.asarray(sp.high)) / 2.0).astype(np.float32)
        except Exception:
            neutral = sp.sample()

        primed_steps = 0
        _max_prime = 15
        while primed_steps < _max_prime and \
                not (1.0 <= float(np.asarray(obs).ravel()[0]) <= 12.0):
            time.sleep(0.5)   # let EnergyPlus fill the obs queue before stepping
            try:
                obs, _r, _term, _trunc, info = env.step(neutral)
            except ValueError:
                # While EnergyPlus is still spinning up, the obs is a placeholder
                # with a garbage date and Sinergym's own reward_fn raises
                # ("month must be in 1..12"). Swallow it and keep priming.
                pass
            primed_steps += 1
        if not (1.0 <= float(np.asarray(obs).ravel()[0]) <= 12.0):
            raise RuntimeError(
                f"EnergyPlus did not stream a real observation after {primed_steps} "
                f"priming steps — check the env/version setup."
            )
        done      = False
        step      = primed_steps
        total_rew = 0.0
        total_energy    = 0.0
        total_comfort   = 0.0
        violation_steps = 0
        # script-parity metrics (mirror maddpg_v3.evaluate)
        m_energy_W  = 0.0
        m_zones_ok  = 0
        m_zone_chk  = 0
        m_dev_sum   = 0.0
        m_dev_steps = 0
        m_db_viol   = 0
        m_db_total  = 0
        # custom (shaped) reward + per-month eval-format logging
        custom_total = 0.0
        cur_month    = 0
        mo_reward    = 0.0
        mo_ok = mo_chk = 0
        mo_dev_sum = 0.0
        mo_dev_steps = 0
        mo_cs = self._cre_cs_cls() if self._metrics_ok else None
        cs_ep = self._cre_cs_cls() if self._metrics_ok else None   # whole-episode CS
        total_hlr = 0.0           # hourly-schedule linear reward (episode total)
        mo_hlr    = 0.0           # ... and current-month running sum
        mo_orig   = 0.0           # current-month native (original) reward sum
        t0        = time.time()
        self._fallback_steps = 0

        self._extract_reward_components(info)
        self._publish_env_info()

        self._publish(episode_topic(self.env_id), {
            "event": "episode_start", "episode": ep,
            "env_id": self.env_id, "mode": self.mode,
            "zones": self.zones,
        })

        # ── Fuseki: episode start ─────────────────────────────────────────────
        self._fuseki.push_episode_start(self.env_id, ep, self.mode, self.zones)

        while not done:
            action_real, action_norm = self._get_action(env)

            next_obs, reward, terminated, truncated, info = env.step(action_real)
            done       = terminated or truncated
            step      += 1
            # If the custom-reward wrapper is active, info carries BOTH rewards.
            # Accumulate the custom (shaped) reward; revert `reward` to the native
            # env reward so all existing publishing / Fuseki / parity stay intact.
            reward_custom_step = None
            if self._custom_ok and info and ("custom_reward" in info):
                reward_custom_step = float(info["custom_reward"])
                custom_total += reward_custom_step
                mo_reward    += reward_custom_step
                reward = float(info.get("original_reward", reward))
            total_rew += float(reward)
            mo_orig   += float(reward)
            self._total_steps += 1

            # Hourly-schedule linear reward (same metric pymdp v7 accumulates).
            reward_hourly_step = None
            if self._hlr_ok:
                try:
                    _hlr = self._hlr_fn(np.asarray(next_obs, dtype=np.float64))
                    reward_hourly_step = float(_hlr[0] if isinstance(_hlr, (tuple, list)) else _hlr)
                    total_hlr += reward_hourly_step
                    mo_hlr    += reward_hourly_step
                except Exception:
                    reward_hourly_step = None
            # Whole-episode comfort score (CS), updated every step.
            if cs_ep is not None:
                try:
                    cs_ep.update(np.asarray(next_obs, dtype=np.float32))
                except Exception:
                    pass

            self._extract_reward_components(info)

            if info:
                ep_val = float(info.get("total_power_demand",
                               info.get("energy_penalty",
                               info.get("HVAC_electricity_demand_rate", 0.0))))
                cv_val = float(info.get("total_temperature_violation",
                               info.get("comfort_penalty", 0.0)))
                total_energy  += abs(ep_val)
                total_comfort += abs(cv_val)
                if abs(cv_val) > 0:
                    violation_steps += 1

            # ── script-parity metrics: post-step obs + applied action ─────────
            try:
                _no = np.asarray(next_obs, dtype=np.float32)
                m_energy_W += float(_no[_M_IDX_HVAC_DEMAND])
                _mon, _day, _hr = int(_no[0]), int(_no[1]), int(_no[2])
                _zt = _no[9:24]
                _cl, _ch = _m_seasonal_comfort(_mon, _day)
                if _m_is_occupied(_mon, _day, _hr):
                    m_zones_ok += int(np.sum((_zt >= _cl) & (_zt <= _ch)))
                    m_zone_chk += _M_N_OCCUPIED
                    _dev = np.maximum(_cl - _zt, 0.0) + np.maximum(_zt - _ch, 0.0)
                    m_dev_sum  += float(np.mean(_dev))
                    m_dev_steps += 1
                _ar = np.asarray(action_real, dtype=np.float32)
                if _ar.shape[0] >= 2 * _M_N_OCCUPIED:
                    for _i in range(_M_N_OCCUPIED):
                        m_db_total += 1
                        if _ar[_i] >= _ar[_M_N_OCCUPIED + _i] - 2.0:
                            m_db_viol += 1
            except Exception:
                pass

            # ── eval-format per-month metrics (mirror maddpg_v3.evaluate) ─────
            if self._metrics_ok:
                try:
                    _no = np.asarray(next_obs, dtype=np.float32)
                    m_now = int(_no[0])
                    if m_now != cur_month:
                        if cur_month > 0:
                            season = "S" if self._cre_seasonal(cur_month, 15) == self._cre_summer else "W"
                            _zcr = (mo_ok / mo_chk * 100.0) if mo_chk > 0 else 100.0
                            _dev = (mo_dev_sum / mo_dev_steps) if mo_dev_steps > 0 else 0.0
                            _zt  = _no[9:24]
                            print(f"  Month {cur_month:2d} [{season}] | "
                                  f"OR: {mo_orig:10.0f} | "
                                  f"CR: {mo_reward:8.2f} | "
                                  f"HLR: {mo_hlr:8.2f} | "
                                  f"T: {float(np.mean(_zt)):5.1f}°C "
                                  f"[{float(np.min(_zt)):.1f}-{float(np.max(_zt)):.1f}] | "
                                  f"CS: {mo_cs.mean:.3f} (occ:{mo_cs.mean_occ:.3f}) | "
                                  f"ZCR: {_zcr:5.1f}% | Dev: {_dev:.3f}°C")
                        cur_month = m_now
                        mo_reward = 0.0
                        mo_hlr = 0.0
                        mo_orig = 0.0
                        mo_ok = mo_chk = 0
                        mo_dev_sum = 0.0
                        mo_dev_steps = 0
                        mo_cs.reset()
                    # accumulate this step into the current month
                    _ok, _tot = self._cre_zcr(_no, occupied_only=True)
                    mo_ok += _ok
                    mo_chk += _tot
                    _d, _n = self._cre_dev(_no, occupied_only=True)
                    mo_dev_sum += _d
                    mo_dev_steps += _n
                    mo_cs.update(_no)
                except Exception as _e:
                    if not getattr(self, "_monthly_err_logged", False):
                        self._monthly_err_logged = True
                        import traceback
                        print(f"[MONTHLY] metrics disabled — first error: {_e!r}")
                        traceback.print_exc()
            # Scalars (hvac_fault_active, hvac_efficiency, anomaly_step, ...) already
            # pass the scalar filter below; the list-valued keys (active labels,
            # kinds, event ids) would be dropped, so JSON-stringify them so they
            # survive intact and reach detectors / Fuseki / the UI for scoring.
            if info:
                for _k in ("anomalies_active", "anomaly_kinds", "anomaly_event_ids"):
                    _v = info.get(_k)
                    if isinstance(_v, (list, tuple)):
                        info = {**info, _k: json.dumps(list(_v))}

            info_serial = {
                k: v for k, v in (info or {}).items()
                if isinstance(v, (int, float, str, bool))
            }

            # ── Publish global observation (MQTT) ─────────────────────────────
            self._publish(obs_topic(self.env_id), {
                "obs":         obs.tolist(),
                "action":      action_norm.tolist(),
                "action_real": action_real.tolist(),
                "reward":         float(reward),          # native linear (original)
                "reward_original": float(reward),
                "reward_custom":  reward_custom_step,     # training-shaped (or None)
                "reward_hourly":  reward_hourly_step,     # hourly-schedule (or None)
                "cs":      round(cs_ep.mean, 4)     if cs_ep is not None else None,
                "cs_occ":  round(cs_ep.mean_occ, 4) if cs_ep is not None else None,
                "done":        bool(done),
                "info":        info_serial,
                "step":        step,
                "episode":     ep,
                "env_id":      self.env_id,
                "mode":        self.mode,
            })

            # ── Fuseki: global observation (sampled every ZONE_STORE_EVERY_N_STEPS) ─
            if step % ZONE_STORE_EVERY_N_STEPS == 0:
                self._fuseki.push_obs(
                    env_id=self.env_id,
                    ep=ep,
                    step=step,
                    obs=obs.tolist(),
                    action_norm=action_norm.tolist(),
                    action_real=action_real.tolist(),
                    reward=float(reward),
                    done=bool(done),
                    mode=self.mode,
                    reward_custom=reward_custom_step,
                    reward_hourly=reward_hourly_step,
                )

            # ── Publish + store per-zone observations ─────────────────────────
            self._publish_zone_observations(
                obs, action_real, action_norm,
                reward, done, info_serial, step, ep,
            )

            obs = next_obs

            if self.render:
                env.render()

            if step % 500 == 0:
                mode_tag = "" if self.single_agent else " [MAS]"
                print(
                    f"  [ep {ep}] step={step:5d}{mode_tag}  "
                    f"reward={reward:+.3f}  total={total_rew:+.1f}  "
                    f"energy={total_energy/1e6:.2f}MWh  "
                    f"comfort_viol={total_comfort:.1f}°C·steps"
                )

        duration = time.time() - t0
        energy_kwh = m_energy_W * 0.25 / 1000.0
        zcr      = (m_zones_ok / m_zone_chk * 100.0) if m_zone_chk > 0 else 100.0
        mean_dev = (m_dev_sum / m_dev_steps) if m_dev_steps > 0 else 0.0
        db_rate  = (m_db_viol / m_db_total * 100.0) if m_db_total > 0 else 0.0
        stats = {
            "episode":        ep,
            "steps":          step,
            "total_reward":   round(total_rew, 4),
            "mean_reward":    round(total_rew / max(step, 1), 6),
            "total_energy_W": round(total_energy, 2),
            "comfort_violations_degC_steps": round(total_comfort, 2),
            "violation_timesteps": violation_steps,
            # script-parity metrics (mirror maddpg_v3.evaluate)
            "energy_kwh":             round(energy_kwh, 1),
            "zone_comfort_rate":      round(zcr, 1),
            "mean_deviation_degC":    round(mean_dev, 3),
            "deadband_violation_pct": round(db_rate, 1),
            "custom_reward":          round(custom_total, 2) if self._custom_ok else None,
            "original_reward":        round(total_rew, 2),
            "hourly_reward":          round(total_hlr, 2) if self._hlr_ok else None,
            "comfort_score":          round(cs_ep.mean, 4)     if cs_ep is not None else None,
            "comfort_score_occ":      round(cs_ep.mean_occ, 4) if cs_ep is not None else None,
            "duration_s":     round(duration, 2),
            "env_id":         self.env_id,
            "mode":           self.mode,
        }
        self._publish(episode_topic(self.env_id), {"event": "episode_end", **stats})

        # ── Fuseki: episode end ───────────────────────────────────────────────
        self._fuseki.push_episode_end(self.env_id, ep, stats)

        ep_rewards = getattr(self, '_all_ep_rewards', [])
        ep_rewards.append(total_rew)
        self._all_ep_rewards = ep_rewards

        vs_prev  = total_rew - ep_rewards[-2] if len(ep_rewards) > 1 else None
        best_so_far = max(ep_rewards)

        comparison = ""
        if vs_prev is not None:
            comparison = f"  ({'▲' if vs_prev >= 0 else '▼'}{abs(vs_prev):.0f} vs ep{ep-1})"
        print(
            f"\n{'─'*60}"
            f"\n  Episode {ep} Summary ({self.mode} mode"
            f"{'' if self.single_agent else ', MAS'}):"
            f"\n  Mean reward:          {total_rew/max(step,1):+.4f}"
            f"\n  Cumulative reward:    {total_rew:+.2f}{comparison}"
            f"\n  Best so far:          {best_so_far:+.2f}"
            f"\n  Total Power Demand:   {total_energy:,.2f} W"
            f"\n  Comfort violations:   {total_comfort:.2f} °C·steps"
            f"\n  Violation timesteps:  {violation_steps} / {step}"
            f"\n  ── script-parity (vs maddpg_v3 evaluate) ──"
            f"\n  Energy:               {energy_kwh:,.0f} kWh"
            f"\n  Zone-Comfort Rate:    {zcr:.1f}%  (occupied only)"
            f"\n  Mean Deviation:       {mean_dev:.3f} °C  (occupied only)"
            f"\n  Deadband Violations:  {db_rate:.1f}%"
            + (f"\n  EVALUATION RESULTS:"
               f"\n  Custom Reward:        {custom_total:.2f} "
               f"(mean/step: {custom_total/max(step,1):.4f})" if self._custom_ok else "")
            + (f"\n  Hourly Reward:        {total_hlr:.2f}" if self._hlr_ok else "")
            + (f"\n  Original Reward:      {total_rew:.2f}"
               f"\n  ── three rewards (mirror pymdp v7) ──"
               f"\n    Original (linear):  {total_rew:.2f}"
               + (f"\n    Custom (shaped):    {custom_total:.2f}" if self._custom_ok else "")
               + (f"\n    Hourly (schedule):  {total_hlr:.2f}" if self._hlr_ok else ""))
            + (f"\n  CS (weighted/occ):    {cs_ep.mean:.4f} / {cs_ep.mean_occ:.4f}"
               if cs_ep is not None else "")
            + f"\n  Duration:             {duration/3600:.2f}h"
            f"\n{'─'*60}"
        )
        return stats

    # ── Main ───────────────────────────────────────────────────────────────────

    def run(self):
        # Start Fuseki background thread
        self._fuseki.start()

        self._mqtt = self._setup_mqtt()
        time.sleep(1.0)

        print(f"\n{'='*60}")
        print(f"  Sinergym ↔ Wactorz Bridge  [MAS v2]")
        print(f"  env      : {self.env_id}")
        print(f"  broker   : {self.broker_host}:{self.broker_port}")
        print(f"  mode     : {self.mode}")
        print(f"  episodes : {self.n_episodes}")
        print(f"  agent    : {'single (global action topic)' if self.single_agent else 'MAS (per-zone action topics)'}")
        print(f"  fuseki   : {self._fuseki._base}/{self._fuseki._ds}"
              f"{' (disabled)' if not self._fuseki._enabled else ''}")

        if self.env_id == "officeMedium-multiagent":
            # Training parity: build via the same factory MADDPG was trained on
            # (paired setpoint ordering), NOT the registered YAML env which groups
            # setpoints and would silently scramble the policy's observation.
            from register_env import make_custom_env
            env = make_custom_env(weather="mixed")   # match Config.WEATHER if you set one
        else:
            env = gym.make(self.env_id)

        # ── Optional anomaly injection ───────────────────────────────────────
        # Wrap the env BEFORE we read/publish obs, so the perturbed observation
        # and the injector's ground-truth `info` annotations flow out on MQTT and
        # into Fuseki automatically. The detector agent sees exactly this stream.
        if self.inject_anomalies:
            try:
                from anomaly_injector import AnomalyEnvWrapper, AnomalySchedule
                # 15 occupied zones among 18 (skip the 3 plenums at 12,13,14),
                # matching the 92-dim OfficeMedium layout the detector expects.
                occ_idx = [0, 1, 2, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]
                n_zones_full = 18
                sched = AnomalySchedule.default_demo(n_zones=n_zones_full,
                                                     seed=self.anomaly_seed)
                env = AnomalyEnvWrapper(
                    env,
                    schedule=sched,
                    outdoor_temp_idx=3,
                    zone_temp_start=9,
                    n_zones=n_zones_full,
                    occ_start_idx=45,
                    hvac_demand_idx=90,
                    meter_idx=91,
                    occupied_zone_indices=occ_idx,
                    random_seed=self.anomaly_seed,
                    verbose=True,
                )
                print(f"[ANOMALY] Injection ENABLED (seed={self.anomaly_seed}, "
                      f"{len(sched.entries)} scheduled events). Ground truth will "
                      f"ride obs `info` to MQTT/Fuseki.")
            except Exception as e:
                print(f"[ANOMALY] Injection requested but failed to enable: {e}")

        # ── Reporting metrics (CS / ZCR / deviation / seasonal / monthly) ─────
        # Self-contained — no maddpg / ensemble_controller dependency. These
        # drive the per-month log + the comfort-score reporting and are always
        # available as long as metrics_utils.py is on the path.
        self._metrics_ok = False
        try:
            from metrics_utils import (get_seasonal_comfort, COMFORT_SUMMER,
                                       CSAccumulator, compute_zone_comfort_rate,
                                       compute_mean_deviation)
            self._cre_seasonal = get_seasonal_comfort
            self._cre_summer   = COMFORT_SUMMER
            self._cre_cs_cls   = CSAccumulator
            self._cre_zcr      = compute_zone_comfort_rate
            self._cre_dev      = compute_mean_deviation
            self._metrics_ok = True
            print("[METRICS] metrics_utils loaded — monthly + comfort-score "
                  "(CS/ZCR/deviation) metrics enabled.")
        except Exception as e:
            print(f"[METRICS] Disabled (could not import metrics_utils): {e}")

        # ── Custom (training-shaped) reward — now self-contained ──────────────
        # CustomRewardWrapper is inlined in metrics_utils (verbatim from
        # maddpg_v2), so no maddpg / ensemble_controller dependency remains.
        self._custom_ok = False
        try:
            from metrics_utils import CustomRewardWrapper
            if CustomRewardWrapper is None:
                raise ImportError("gymnasium unavailable in metrics_utils")
            env = CustomRewardWrapper(env)   # default RewardConfig = the eval one
            self._custom_ok = True
            print("[CUSTOM-REWARD] Wrapped env via metrics_utils.CustomRewardWrapper "
                  "— Custom Reward enabled (no maddpg needed).")
        except Exception as e:
            print(f"[CUSTOM-REWARD] Disabled ({e}); running original + hourly "
                  "rewards and metrics only.")

        # ── Hourly-schedule linear reward (re-exported via metrics_utils) ────
        self._hlr_ok = False
        try:
            from metrics_utils import compute_hourly_linear_reward
            if compute_hourly_linear_reward is None:
                raise ImportError("hourly_reward_metric not importable")
            self._hlr_fn = compute_hourly_linear_reward
            self._hlr_ok = True
            print("[HOURLY-REWARD] compute_hourly_linear_reward enabled (via metrics_utils).")
        except Exception as e:
            print(f"[HOURLY-REWARD] Disabled (could not import hourly metric): {e}")
        self._act_low  = env.action_space.low.copy()  if hasattr(env.action_space, 'low')  else np.zeros(1)
        self._act_high = env.action_space.high.copy() if hasattr(env.action_space, 'high') else np.ones(1)

        self._extract_variable_names(env)
        self._setup_zones(env)
        self._publish_action_space()
        self._publish_env_info()

        # ── Fuseki: push catalog (env metadata) ───────────────────────────────
        self._fuseki.push_catalog(
            env_id=self.env_id,
            obs_names=self._obs_variable_names or [],
            act_names=self._action_variable_names or [],
            act_low=self._act_low.tolist(),
            act_high=self._act_high.tolist(),
            zones=self.zones,
            mode=self.mode,
        )

        print(f"\n  Global action space (real bounds):")
        for i, (lo, hi) in enumerate(zip(self._act_low, self._act_high)):
            name = (self._action_variable_names[i]
                    if self._action_variable_names and i < len(self._action_variable_names)
                    else f"action_{i}")
            print(f"    [{i}] {name}: [{lo:.2f}, {hi:.2f}]")

        if not self.single_agent:
            print(f"\n  Per-zone action topics:")
            for zone in self.zones:
                print(f"    {zone_action_topic(self.env_id, zone)}")
            print(f"\n  Per-zone obs topics:")
            for zone in self.zones:
                print(f"    {zone_obs_topic(self.env_id, zone)}  "
                      f"({len(self.zone_obs_indices.get(zone,[]))} dims)")

        print(f"{'='*60}\n")

        all_stats = []
        try:
            for ep_num in range(1, self.n_episodes + 1):
                print(f"\n[Episode {ep_num}/{self.n_episodes}]")
                stats = self._run_episode(env, ep_num)
                all_stats.append(stats)
                print(
                    f"  → steps={stats['steps']}  "
                    f"total_reward={stats['total_reward']:+.2f}  "
                    f"mean_reward={stats['mean_reward']:+.4f}  "
                    f"({stats['duration_s']:.1f}s)"
                )
        except KeyboardInterrupt:
            print("\n[Bridge] Interrupted.")
        finally:
            env.close()
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
            self._fuseki.stop()

        if all_stats:
            n_eps        = len(all_stats)
            mean_reward  = sum(s["total_reward"]   for s in all_stats) / n_eps
            mean_steps   = sum(s["steps"]          for s in all_stats) / n_eps
            mean_energy  = sum(s["total_energy_W"] for s in all_stats) / n_eps
            mean_comfort = sum(s["comfort_violations_degC_steps"] for s in all_stats) / n_eps
            mean_energy_kwh = sum(s.get("energy_kwh", 0.0)             for s in all_stats) / n_eps
            mean_zcr        = sum(s.get("zone_comfort_rate", 0.0)      for s in all_stats) / n_eps
            mean_dev_all    = sum(s.get("mean_deviation_degC", 0.0)    for s in all_stats) / n_eps
            mean_db         = sum(s.get("deadband_violation_pct", 0.0) for s in all_stats) / n_eps
            all_rewards  = [s["total_reward"] for s in all_stats]
            best_ep      = max(all_rewards)
            worst_ep     = min(all_rewards)
            trend        = all_rewards[-1] - all_rewards[0] if n_eps > 1 else 0.0
            print(f"\n{'='*60}")
            print(f"  Run Summary: {n_eps} episodes  ({self.mode} mode)")
            print(f"  Mean cumulative reward : {mean_reward:+.2f}")
            print(f"  Best episode           : {best_ep:+.2f}")
            print(f"  Worst episode          : {worst_ep:+.2f}")
            if n_eps > 1:
                print(f"  Trend (ep1→ep{n_eps})      : {'▲' if trend >= 0 else '▼'}{abs(trend):.0f}")
            print(f"  Mean steps/episode     : {mean_steps:.0f}")
            print(f"  Mean power demand      : {mean_energy:,.2f} W")
            print(f"  Mean comfort violations: {mean_comfort:.2f} °C·steps")
            print(f"  ── script-parity (vs maddpg_v3 evaluate) ──")
            print(f"  Energy                 : {mean_energy_kwh:,.0f} kWh")
            print(f"  Zone-Comfort Rate      : {mean_zcr:.1f}%  (occupied only)")
            print(f"  Mean Deviation         : {mean_dev_all:.3f} °C  (occupied only)")
            print(f"  Deadband Violations    : {mean_db:.1f}%")
            print(f"  Total steps            : {self._total_steps}")
            print(f"{'='*60}\n")

        return all_stats


# ── CLI ────────────────────────────────────────────────────────────────────────

def get_args():
    p = argparse.ArgumentParser(
        description="Sinergym ↔ Wactorz MQTT Bridge — MAS edition v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  collect  — random actions → per-zone data for training
  deploy   — per-zone optimizer actions (MAS) or single global action

Environment variables for Fuseki:
  FUSEKI_URL       (default: http://localhost:3030)
  FUSEKI_DATASET   (default: sinergym)
  FUSEKI_USER      (default: admin)
  FUSEKI_PASSWORD  (default: empty)

Examples:
  python sinergym_bridge_mas.py --mode collect --episodes 10
  python sinergym_bridge_mas.py --mode deploy --episodes 5
  python sinergym_bridge_mas.py --mode deploy --single-agent --episodes 5
  python sinergym_bridge_mas.py --mode collect --no-fuseki
  python sinergym_bridge_mas.py --env Eplus-5zone-hot-continuous-v1 --episodes 10 \\
      --zones "SPACE1-1,SPACE2-1,SPACE3-1,SPACE4-1,SPACE5-1"
        """,
    )
    p.add_argument("--env",           default=DEFAULT_ENV_ID)
    p.add_argument("--broker",        default=DEFAULT_BROKER_HOST)
    p.add_argument("--port",          type=int, default=DEFAULT_BROKER_PORT)
    p.add_argument("--mode",          choices=["collect", "deploy", "eval"], default="collect")
    p.add_argument("--episodes",      type=int, default=10)
    p.add_argument("--render",        action="store_true")
    p.add_argument("--single-agent",  action="store_true",
                   help="Use global action topic instead of per-zone topics (v2 compat)")
    p.add_argument("--zones",         default=None,
                   help="Comma-separated zone names (auto-detected if omitted)")
    p.add_argument("--zone-obs-local",type=int, default=None,
                   help="Override per-zone local obs dim count (default: 5 for 5zone env)")
    p.add_argument("--no-fuseki",     action="store_true",
                   help="Disable Fuseki integration entirely")
    p.add_argument("--fuseki-url",    default=FUSEKI_URL,
                   help=f"Fuseki base URL (default: {FUSEKI_URL})")
    p.add_argument("--fuseki-dataset",default=FUSEKI_DATASET,
                   help=f"Fuseki dataset name (default: {FUSEKI_DATASET})")
    p.add_argument("--fuseki-user",   default=FUSEKI_USER)
    p.add_argument("--fuseki-password",default=FUSEKI_PASSWORD)
    p.add_argument("--inject-anomalies", action="store_true",
                   help="Wrap the env in AnomalyEnvWrapper to inject scheduled "
                        "anomalies (ground truth is annotated into obs info).")
    p.add_argument("--anomaly-seed",  type=int, default=5,
                   help="Reproducible anomaly schedule seed (default 5).")
    return p.parse_args()


if __name__ == "__main__":
    args  = get_args()
    zones = [z.strip() for z in args.zones.split(",")] if args.zones else None
    bridge = SinergymBridgeMAS(
        env_id           = args.env,
        broker_host      = args.broker,
        broker_port      = args.port,
        mode             = args.mode,
        n_episodes       = args.episodes,
        render           = args.render,
        zones            = zones,
        single_agent     = args.single_agent,
        zone_obs_local   = args.zone_obs_local,
        fuseki_enabled   = not args.no_fuseki,
        fuseki_url       = args.fuseki_url,
        fuseki_dataset   = args.fuseki_dataset,
        fuseki_user      = args.fuseki_user,
        fuseki_password  = args.fuseki_password,
        inject_anomalies = args.inject_anomalies,
        anomaly_seed     = args.anomaly_seed,
    )
    bridge.run()