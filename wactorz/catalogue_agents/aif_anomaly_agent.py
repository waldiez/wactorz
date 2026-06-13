"""
aif_anomaly_agent.py — live anomaly detection over the Sinergym obs stream using
the AIF-trained detector (detector_aif_v3.pkl). Structural twin of
sinergym_anomaly_agent.py; only the defaults, identity, and Fuseki IRI differ so
the two detectors can run side-by-side without clobbering each other's records.

Place in catalogue_agents/ and register in catalog_agent.py _build_catalog()
(see REGISTER block at the bottom).

What it does
------------
Loads a pre-trained detector pickle and runs it online against the bridge's
GLOBAL observation stream. The detector reads the action it needs straight out of
obs[60:90] (the setpoints Sinergym writes back), so this agent subscribes to the
global obs topic ONLY. On each step it calls detector.update(obs, info); when an
Alert fires it:
  * publishes the alert to  sinergym/env/<env_id>/anomaly  (data plane)
  * writes an sgy:Alert triple to Fuseki (urn:sinergym:anomalies) with
    provenance. The alert IRI includes this agent's name, so AIF alerts and the
    forecast detector's alerts coexist in the store and each can be scored
    against the injector's ground-truth independently.

It resets the detector's rolling window on episode boundaries (reset_episode).

Detector module
---------------
By default this loads forecast_anomaly_detector.ForecastAnomalyDetector (the same
class the forecast detector uses) — set `detector_module` / `detector_class` in
the launch params if detector_aif_v3.pkl is produced by a different class. The
loader calls <class>.load(path); a version/format mismatch surfaces as a clear
load error in the logs rather than silently no-op'ing.

Files (place beside the model)
------------------------------
  <infer_dir>/   (default: state/maddpg_office, override via SINERGYM_MODEL_DIR)
      <detector module>.py           (imported)
      detector_aif_v3.pkl            (the trained pickle — loaded)

Launch params (defaults shown)
------------------------------
  env_id          "officeMedium-multiagent"
  infer_dir       "state/maddpg_office"  (or $SINERGYM_MODEL_DIR)
  detector_path   "<infer_dir>/detector_aif_v3.pkl"
  detector_module "forecast_anomaly_detector"
  detector_class  "ForecastAnomalyDetector"
  fuseki_url/dataset/user/password   (same store the bridge writes to)

Control (via @aif-anomaly)
--------------------------
  {"action":"status"}   -> fitted?, steps seen, alerts fired, last alert
  {"action":"reset"}    -> reset the detector's episode window
  {"action":"config", ...}  -> update params (re-loads detector)
"""

AGENT_CODE = r'''
import asyncio
import json
import os
import sys

ENV_ID_DEFAULT = "officeMedium-multiagent"
# Portable default: relative to the wactorz working dir (repo root), overridable via env.
INFER_DIR_DEFAULT = os.environ.get("SINERGYM_MODEL_DIR", "state/maddpg_office")
DETECTOR_FILE_DEFAULT = "detector_aif_v3.pkl"
DETECTOR_MODULE_DEFAULT = "forecast_anomaly_detector"
DETECTOR_CLASS_DEFAULT  = "ForecastAnomalyDetector"

SGY_NS  = "https://waldiez.github.io/wactorz/sinergym#"
G_ANOM  = "urn:sinergym:anomalies"
BRIDGE_IRI = "<urn:sinergym:bridge>"

PREFIXES = (
    "PREFIX sgy: <" + SGY_NS + ">\n"
    "PREFIX prov: <http://www.w3.org/ns/prov#>\n"
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
)


def _cfg(agent, payload=None):
    cfg = {
        "env_id":          ENV_ID_DEFAULT,
        "infer_dir":       INFER_DIR_DEFAULT,
        "detector_path":   None,
        "detector_module": DETECTOR_MODULE_DEFAULT,
        "detector_class":  DETECTOR_CLASS_DEFAULT,
        "fuseki_url":      "http://localhost:3030",
        "fuseki_dataset":  "sinergym",
        "fuseki_user":     "admin",
        "fuseki_password": "admin",
        "agent_name":      "aif-anomaly",
    }
    saved = agent.recall("config", None)
    if isinstance(saved, dict):
        cfg.update({k: v for k, v in saved.items() if v is not None})
    if isinstance(payload, dict):
        cfg.update({k: payload[k] for k in cfg if payload.get(k) is not None})
    if not cfg["detector_path"]:
        cfg["detector_path"] = cfg["infer_dir"].rstrip("/") + "/" + DETECTOR_FILE_DEFAULT
    return cfg


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _load_detector(agent, cfg):
    # Make the detector module importable, then load the trained pickle.
    if cfg["infer_dir"] not in sys.path:
        sys.path.insert(0, cfg["infer_dir"])
    import importlib
    mod = importlib.import_module(cfg["detector_module"])
    klass = getattr(mod, cfg["detector_class"])
    det = klass.load(cfg["detector_path"])
    return det


# ── Fuseki write (data-plane SPARQL UPDATE; matches the bridge's store) ──────────
def _fuseki_update(cfg, body_ttl):
    import urllib.request, urllib.parse, base64
    update = (PREFIXES + "INSERT DATA { GRAPH <" + G_ANOM + "> {\n" + body_ttl + "\n} }")
    url = cfg["fuseki_url"].rstrip("/") + "/" + cfg["fuseki_dataset"] + "/update"
    data = urllib.parse.urlencode({"update": update}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    user = cfg.get("fuseki_user")
    if user:
        tok = base64.b64encode(f"{user}:{cfg.get('fuseki_password','')}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def _alert_ttl(cfg, alert, step, episode):
    # Agent name in the IRI so AIF alerts don't clobber the forecast detector's
    # alerts (which key on env/ep/step only) in the shared anomalies graph.
    iri = f"sgy:alert_{_esc(cfg['agent_name'])}_{_esc(cfg['env_id'])}_ep{episode}_s{step}"
    a_iri = f"sgy:agent_{_esc(cfg['agent_name'])}"
    lines = [
        f"{iri} a sgy:Alert ;",
        f'  sgy:envId "{_esc(cfg["env_id"])}" ;',
        f"  sgy:episode {int(episode) if episode is not None else 0} ;",
        f"  sgy:step {int(step) if step is not None else 0} ;",
        f'  sgy:kindGuess "{_esc(alert.kind_guess)}" ;',
        f"  sgy:severity {float(alert.severity):.4f} ;",
        f'  sgy:detector "{_esc(alert.detector_name)}" ;',
        f'  sgy:message "{_esc(alert.message)}" ;',
    ]
    if getattr(alert, "zone_idx", None) is not None:
        lines.append(f"  sgy:zoneIndex {int(alert.zone_idx)} ;")
    if getattr(alert, "sources", None):
        lines.append(f'  sgy:sources "{_esc(",".join(alert.sources))}" ;')
    lines.append(f"  prov:wasAttributedTo {a_iri} .")
    lines.append(f'{a_iri} a prov:SoftwareAgent ; rdfs:label "{_esc(cfg["agent_name"])}" ; '
                 f"prov:actedOnBehalfOf {BRIDGE_IRI} .")
    return "\n".join(lines)


async def setup(agent):
    cfg = _cfg(agent)
    agent.state["cfg"]        = cfg
    agent.state["det"]        = None
    agent.state["last_ep"]    = None
    agent.state["n_steps"]    = 0
    agent.state["n_alerts"]   = 0
    agent.state["last_alert"] = None
    agent.state["fuseki_fail"] = 0

    try:
        agent.state["det"] = await asyncio.to_thread(_load_detector, agent, cfg)
        fitted = getattr(agent.state["det"], "is_fitted", False)
        await agent.log(f"[aif-anomaly] detector loaded from {cfg['detector_path']} "
                  f"({cfg['detector_module']}.{cfg['detector_class']}, is_fitted={fitted})")
        if not fitted:
            await agent.log("[aif-anomaly] WARNING: detector reports is_fitted=False; "
                      "update() will no-op until a fitted model is loaded", level="warning")
    except Exception as e:
        await agent.log(f"[aif-anomaly] FAILED to load detector: {e}", level="error")

    obs_topic   = f"sinergym/env/{cfg['env_id']}/observation"
    agent.state["alert_topic"] = f"sinergym/env/{cfg['env_id']}/anomaly"

    async def on_obs(payload):
        det = agent.state.get("det")
        if det is None or not isinstance(payload, dict):
            return
        obs = payload.get("obs")
        if not isinstance(obs, list):
            return
        ep = payload.get("episode")
        step = payload.get("step")
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}

        # Episode boundary -> reset the detector's rolling window.
        last = agent.state.get("last_ep")
        if last is not None and ep != last:
            try:
                det.reset_episode()
            except Exception:
                pass
        agent.state["last_ep"] = ep

        import numpy as _np
        try:
            alert = det.update(_np.asarray(obs, dtype=_np.float64), info, None)
        except Exception as e:
            if agent.state["n_steps"] % 500 == 0:
                await agent.log(f"[aif-anomaly] update() error: {e}", level="warning")
            alert = None
        agent.state["n_steps"] = agent.state.get("n_steps", 0) + 1

        if not alert:
            return

        agent.state["n_alerts"] += 1
        rec = {
            "step":       step,
            "episode":    ep,
            "kind_guess": alert.kind_guess,
            "severity":   float(alert.severity),
            "zone_idx":   getattr(alert, "zone_idx", None),
            "sources":    list(getattr(alert, "sources", []) or []),
            "message":    alert.message,
            "detector":   alert.detector_name,
            "agent":      cfg["agent_name"],
        }
        agent.state["last_alert"] = rec
        # 1) publish on the data plane
        try:
            await agent.publish(agent.state["alert_topic"], rec)
        except Exception as e:
            await agent.log(f"[aif-anomaly] publish failed: {e}", level="warning")
        # 2) persist to Fuseki with provenance (off the event loop)
        try:
            await asyncio.to_thread(_fuseki_update, cfg, _alert_ttl(cfg, alert, step, ep))
        except Exception as e:
            agent.state["fuseki_fail"] += 1
            if agent.state["fuseki_fail"] <= 3:
                await agent.log(f"[aif-anomaly] Fuseki write failed: {e}", level="warning")
        await agent.log(f"[aif-anomaly] ALERT step={step} kind={alert.kind_guess} "
                  f"sev={alert.severity:.2f} zone={getattr(alert,'zone_idx',None)}")

    agent.subscribe(obs_topic, on_obs)
    await agent.log(f"aif-anomaly ready; watching {obs_topic}, alerts -> "
              f"{agent.state['alert_topic']} and Fuseki <{G_ANOM}>.")


async def process(agent):
    pass


async def handle_task(agent, payload):
    if isinstance(payload, dict) and "action" not in payload and "text" in payload:
        raw = (payload.get("text") or "").strip()
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"action": raw.split()[0] if raw else ""}
        elif raw:
            payload = {"action": raw.split()[0]}

    action = ""
    if isinstance(payload, dict):
        action = (payload.get("action") or "").lower().strip()
    elif isinstance(payload, str):
        action = payload.lower().strip()

    if action == "config":
        cfg = _cfg(agent, payload if isinstance(payload, dict) else None)
        agent.persist("config", cfg)
        agent.state["cfg"] = cfg
        try:
            agent.state["det"] = await asyncio.to_thread(_load_detector, agent, cfg)
            agent.state["last_ep"] = None
            return f"Config updated; detector reloaded from {cfg['detector_path']}."
        except Exception as e:
            return f"Config saved but detector reload failed: {e}"

    if action == "reset":
        det = agent.state.get("det")
        if det is not None:
            try:
                det.reset_episode()
            except Exception:
                pass
        agent.state["last_ep"] = None
        return "Detector episode window reset."

    # default: status
    det = agent.state.get("det")
    fitted = getattr(det, "is_fitted", False) if det is not None else False
    la = agent.state.get("last_alert")
    cfg = agent.state.get("cfg", {})
    return (f"aif-anomaly status:\n"
            f"  detector loaded: {det is not None} (is_fitted={fitted})\n"
            f"  detector path:   {cfg.get('detector_path')}\n"
            f"  module/class:    {cfg.get('detector_module')}.{cfg.get('detector_class')}\n"
            f"  obs steps seen:  {agent.state.get('n_steps', 0)}\n"
            f"  alerts fired:    {agent.state.get('n_alerts', 0)}\n"
            f"  last alert:      {la}\n"
            f"  alert topic:     {agent.state.get('alert_topic')}\n"
            f"  Fuseki graph:    {G_ANOM} (write failures: {agent.state.get('fuseki_fail', 0)})")
'''


# ──────────────────────────────────────────────────────────────────────────────
# REGISTER — add this block inside _build_catalog() in catalog_agent.py
# ──────────────────────────────────────────────────────────────────────────────
#   code = _load_recipe("aif_anomaly_agent.py")
#   if code:
#       catalog["aif-anomaly"] = {
#           "name":         "aif-anomaly",
#           "type":         "dynamic",
#           "description":  "Live anomaly detection on the Sinergym observation "
#                           "stream using the AIF-trained detector "
#                           "(detector_aif_v3.pkl). Subscribes to the global obs "
#                           "topic, publishes alerts on .../anomaly, and records "
#                           "them in Fuseki with provenance (agent-scoped IRIs) so "
#                           "it can be scored alongside the forecast detector.",
#           "capabilities": ["sinergym", "anomaly_detection", "fault_detection",
#                            "monitoring", "active_inference", "aif", "hvac_fault",
#                            "sensor_drift"],
#           "install":      ["torch", "numpy"],
#           "input_schema": {
#               "action":          "str — optional: status | reset | config",
#               "detector_path":   "str — path to detector_aif_v3.pkl",
#               "detector_module": "str — module exposing the detector class",
#               "detector_class":  "str — detector class name (has .load/.update)",
#               "infer_dir":       "str — dir with the detector module + .pkl",
#               "env_id":          "str — optional",
#               "fuseki_url":      "str — optional",
#           },
#           "output_schema": {"status": "str"},
#           "poll_interval": 3600,
#           "code":          code,
#       }
#       logger.info("[catalog] Loaded aif-anomaly recipe")
