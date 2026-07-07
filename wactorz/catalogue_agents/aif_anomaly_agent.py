"""aif_anomaly_agent.py — live anomaly detection over the Sinergym obs stream using
the AIF-trained detector (detector_aif_v6.pkl). Structural twin of
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
the launch params if detector_aif_v6.pkl is produced by a different class. The
loader calls <class>.load(path); a version/format mismatch surfaces as a clear
load error in the logs rather than silently no-op'ing.

Files (place beside the model)
------------------------------
  <infer_dir>/   (default: models/aif, override via SINERGYM_MODEL_DIR)
      <detector module>.py           (imported)
      detector_aif_v6.pkl            (the trained pickle — loaded)

Launch params (defaults shown)
------------------------------
  env_id          "officeMedium-multiagent"
  infer_dir       "models/aif"  (or $SINERGYM_MODEL_DIR)
  detector_path   "<infer_dir>/detector_aif_v6.pkl"
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
INFER_DIR_DEFAULT = os.environ.get("SINERGYM_MODEL_DIR", "models/aif")
DETECTOR_FILE_DEFAULT = "detector_aif_v6.pkl"
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
        # ── real-time episode collapsing + live TP/FP scoring ──────────────
        "burst_steps":     16,    # alerts within this gap collapse into 1 episode
        "gt_grace":        12,    # a detection up to this many steps after a GT
                                  # window still counts as a true positive
        "publish_raw":     False, # raw per-step alerts off; only collapsed
                                  # episodes are published (to the topic below)
        "episode_topic":   None,  # where collapsed episodes go; default = the
                                  # original detector topic .../anomaly
        "score_live":      True,  # label episodes TP/FP from the bridge's
                                  # ground-truth info (anomalies_active/event_ids)
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
    try:
        return klass.load(cfg["detector_path"])
    except TypeError as e:
        # Detector class drift: the pickle's saved hyperparameters include
        # constructor args this on-path version of the class doesn't accept
        # (e.g. 'mz_fault_zones'). Best fix is to put the MATCHING detector
        # module on the path; as an unblock, wrap __init__ to ignore unknown
        # kwargs so the fitted detector still loads (with a warning). The fitted
        # forecasters/baselines are restored after __init__, so loading still
        # works — only the dropped constructor option falls back to its default.
        if "unexpected keyword argument" not in str(e):
            raise
        import inspect
        orig_init = klass.__init__
        sig = inspect.signature(orig_init)
        has_var_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        accepted = {p.name for p in sig.parameters.values()}

        def _tolerant_init(self, *args, **kwargs):
            if not has_var_kw:
                dropped = [k for k in list(kwargs) if k not in accepted]
                for k in dropped:
                    kwargs.pop(k, None)
                if dropped:
                    print(f"[aif-anomaly] WARNING: detector class drift — dropping "
                          f"unknown constructor kwargs {dropped}. Put the matching "
                          f"{cfg['detector_module']}.py (the version that trained "
                          f"{cfg['detector_path']}) on the path to avoid this.")
            return orig_init(self, *args, **kwargs)

        klass.__init__ = _tolerant_init
        try:
            return klass.load(cfg["detector_path"])
        finally:
            klass.__init__ = orig_init


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


def _gt_from_info(info):
    """Parse the injector ground-truth the bridge ships in obs['info'] (the
    list-valued keys arrive JSON-stringified). Returns (is_active, kinds, ids)."""
    if not isinstance(info, dict):
        return False, [], []
    def _list(k):
        v = info.get(k)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                v = [v] if v else []
        return list(v) if isinstance(v, (list, tuple)) else ([] if v is None else [v])
    kinds = _list("anomaly_kinds")
    ids   = _list("anomaly_event_ids")
    active_labels = _list("anomalies_active")
    is_active = bool(active_labels or ids or kinds or
                     info.get("hvac_fault_active") or info.get("sensor_drift_active"))
    return is_active, kinds, ids


def _episode_ttl(cfg, epi):
    a_iri = f"sgy:agent_{_esc(cfg['agent_name'])}"
    iri = (f"sgy:alert_{_esc(cfg['agent_name'])}_{_esc(cfg['env_id'])}"
           f"_ep{epi['env_episode']}_s{epi['start_step']}")
    kind = ",".join(sorted(epi["kinds"])) or "anomaly"
    # Typed as sgy:Alert (so anything discovering alerts finds it) AND
    # sgy:AlertEpisode (carrying the collapsed-burst detail).
    lines = [
        f"{iri} a sgy:Alert, sgy:AlertEpisode ;",
        f'  sgy:envId "{_esc(cfg["env_id"])}" ;',
        f"  sgy:episode {int(epi['env_episode']) if epi['env_episode'] is not None else 0} ;",
        f"  sgy:step {int(epi['last_step'])} ;",
        f"  sgy:startStep {int(epi['start_step'])} ;",
        f"  sgy:endStep {int(epi['last_step'])} ;",
        f"  sgy:alertCount {int(epi['count'])} ;",
        f"  sgy:severity {float(epi['peak_sev']):.4f} ;",
        f"  sgy:peakSeverity {float(epi['peak_sev']):.4f} ;",
        f'  sgy:kindGuess "{_esc(kind)}" ;',
        f'  sgy:sources "{_esc(",".join(sorted(epi["sources"])))}" ;',
        f'  sgy:detector "forecast" ;',
        f'  sgy:message "collapsed episode: {int(epi["count"])} alerts over '
        f'steps {int(epi["start_step"])}-{int(epi["last_step"])}" ;',
        f'  sgy:label "{"TP" if epi["gt_hit"] else "FP"}" ;',
        f"  prov:wasAttributedTo {a_iri} .",
    ]
    return "\n".join(lines)


async def _close_episode(agent, cfg):
    """Classify the open alert episode TP/FP, update the scoreboard, and emit it
    (MQTT episode topic + Fuseki). Called when an episode's burst window lapses,
    on episode boundary, or at shutdown."""
    epi = agent.state.get("cur_episode")
    if not epi:
        return
    agent.state["cur_episode"] = None

    sb = agent.state["score"]
    sb["episodes"] += 1
    if epi["gt_hit"]:
        sb["tp_episodes"] += 1
        for eid in epi["gt_ids"]:
            sb["detected_ids"].add(eid)
    else:
        sb["fp_episodes"] += 1
    for s in epi["sources"]:
        sb["source_breakdown"][s] = sb["source_breakdown"].get(s, 0) + 1

    kind = ",".join(sorted(epi["kinds"])) or "anomaly"
    rec = {
        # ── backward-compatible alert keys (same shape the old detector used) ──
        "step":        epi["last_step"],
        "episode":     epi["env_episode"],
        "kind_guess":  kind,
        "severity":    round(epi["peak_sev"], 4),
        "zone_idx":    None,
        "sources":     sorted(epi["sources"]),
        "message":     (f"collapsed episode: {epi['count']} alerts over "
                        f"steps {epi['start_step']}-{epi['last_step']}"),
        "detector":    "forecast",
        "agent":       cfg["agent_name"],
        # ── episode extras ──
        "type":        "anomaly_episode",
        "label":       "TP" if epi["gt_hit"] else "FP",
        "start_step":  epi["start_step"],
        "end_step":    epi["last_step"],
        "alert_count": epi["count"],
        "duration":    epi["last_step"] - epi["start_step"] + 1,
        "peak_severity": round(epi["peak_sev"], 4),
        "kinds":       sorted(epi["kinds"]),
        "gt_kinds":    sorted(epi["gt_kinds"]),
        "scoreboard":  _scoreboard_view(agent),
    }
    topic = agent.state.get("episode_topic") or agent.state.get("alert_topic")
    try:
        await agent.publish(topic, rec)
    except Exception as e:
        await agent.log(f"[aif-anomaly] episode publish failed: {e}", level="warning")
    try:
        await asyncio.to_thread(_fuseki_update, cfg, _episode_ttl(cfg, epi))
    except Exception:
        pass
    await agent.log(f"[aif-anomaly] EPISODE {rec['label']} "
                    f"steps {epi['start_step']}-{epi['last_step']} "
                    f"({epi['count']} alerts) kinds={sorted(epi['kinds'])} "
                    f"| live P={rec['scoreboard']['precision']} "
                    f"R={rec['scoreboard']['recall']}")


def _scoreboard_view(agent):
    sb = agent.state["score"]
    tp, fp = sb["tp_episodes"], sb["fp_episodes"]
    seen = len(sb["seen_ids"]); det = len(sb["detected_ids"])
    prec = round(tp / (tp + fp), 3) if (tp + fp) else None
    rec  = round(det / seen, 3) if seen else None
    return {
        "episodes":     sb["episodes"],
        "tp_episodes":  tp,
        "fp_episodes":  fp,
        "raw_alerts":   sb["raw_alerts"],
        "gt_events_seen":     seen,
        "gt_events_detected": det,
        "precision":    prec,     # TP / (TP+FP) at the EPISODE level
        "recall":       rec,      # distinct GT events detected / seen
        "source_breakdown": dict(sb["source_breakdown"]),
    }


async def setup(agent):
    cfg = _cfg(agent)
    agent.state["cfg"]        = cfg
    agent.state["det"]        = None
    agent.state["last_ep"]    = None
    agent.state["n_steps"]    = 0
    agent.state["n_alerts"]   = 0
    agent.state["last_alert"] = None
    agent.state["fuseki_fail"] = 0
    # episode collapsing + live scoring state
    agent.state["cur_episode"]   = None
    agent.state["last_gt_step"]  = None    # last step ground-truth was active
    agent.state["score"] = {
        "episodes": 0, "tp_episodes": 0, "fp_episodes": 0, "raw_alerts": 0,
        "seen_ids": set(), "detected_ids": set(), "source_breakdown": {},
    }

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
    agent.state["alert_topic"]   = f"sinergym/env/{cfg['env_id']}/anomaly"
    # Collapsed episodes go to the original detector topic by default.
    agent.state["episode_topic"] = cfg.get("episode_topic") or agent.state["alert_topic"]

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

        # Episode boundary -> reset detector window AND close any open alert episode.
        last = agent.state.get("last_ep")
        if last is not None and ep != last:
            await _close_episode(agent, cfg)
            agent.state["last_gt_step"] = None
            try:
                det.reset_episode()
            except Exception:
                pass
        agent.state["last_ep"] = ep

        # ── Ground truth (for live TP/FP) ────────────────────────────────────
        gt_active, gt_kinds, gt_ids = (False, [], [])
        if cfg.get("score_live", True):
            gt_active, gt_kinds, gt_ids = _gt_from_info(info)
            if gt_active:
                agent.state["last_gt_step"] = step
                for _id in gt_ids:
                    agent.state["score"]["seen_ids"].add(_id)

        # ── Close the open episode if its burst window lapsed (no alert) ─────
        cur = agent.state.get("cur_episode")
        if cur is not None and step is not None and \
           (step - cur["last_step"]) > int(cfg["burst_steps"]):
            await _close_episode(agent, cfg)

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

        agent.state["score"]["raw_alerts"] += 1
        agent.state["n_alerts"] += 1
        sources = list(getattr(alert, "sources", []) or [])
        rec = {
            "step":       step,
            "episode":    ep,
            "kind_guess": alert.kind_guess,
            "severity":   float(alert.severity),
            "zone_idx":   getattr(alert, "zone_idx", None),
            "sources":    sources,
            "message":    alert.message,
            "detector":   alert.detector_name,
            "agent":      cfg["agent_name"],
        }
        agent.state["last_alert"] = rec

        # ── Episode collapsing: open or extend the current alert episode ─────
        # A detection is TP if ground truth is active now OR was active within
        # gt_grace steps before (credits the injector ramp-out + detector lag).
        last_gt = agent.state.get("last_gt_step")
        near_gt = gt_active or (last_gt is not None and step is not None and
                                (step - last_gt) <= int(cfg["gt_grace"]))
        cur = agent.state.get("cur_episode")
        if cur is None:
            agent.state["cur_episode"] = {
                "start_step": step, "last_step": step, "count": 1,
                "peak_sev": float(alert.severity),
                "kinds": set([alert.kind_guess]) if alert.kind_guess else set(),
                "sources": set(sources),
                "gt_hit": bool(near_gt),
                "gt_kinds": set(gt_kinds), "gt_ids": set(gt_ids),
                "env_episode": ep,
            }
        else:
            cur["last_step"] = step
            cur["count"]    += 1
            cur["peak_sev"]  = max(cur["peak_sev"], float(alert.severity))
            if alert.kind_guess:
                cur["kinds"].add(alert.kind_guess)
            cur["sources"].update(sources)
            if near_gt:
                cur["gt_hit"] = True
                cur["gt_kinds"].update(gt_kinds)
                cur["gt_ids"].update(gt_ids)

        # ── Optional raw per-step alert stream (off by default) ──────────────
        if cfg.get("publish_raw", False):
            try:
                await agent.publish(agent.state["alert_topic"], rec)
            except Exception:
                pass
            try:
                await asyncio.to_thread(_fuseki_update, cfg, _alert_ttl(cfg, alert, step, ep))
            except Exception:
                agent.state["fuseki_fail"] += 1

    agent.subscribe(obs_topic, on_obs)
    await agent.log(f"aif-anomaly ready; watching {obs_topic}. Publishing COLLAPSED "
              f"episodes (burst={cfg['burst_steps']} steps, live TP/FP "
              f"{'on' if cfg.get('score_live', True) else 'off'}) to "
              f"{agent.state['episode_topic']} + Fuseki <{G_ANOM}> as sgy:Alert. "
              f"Raw per-step alerts {'ON' if cfg.get('publish_raw', False) else 'off'}.")


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

    if action in ("report", "scoreboard"):
        # Close any in-flight episode so the report is current, then dump it.
        cfg = agent.state.get("cfg", {})
        await _close_episode(agent, cfg)
        return {"ok": True, "scoreboard": _scoreboard_view(agent),
                "burst_steps": cfg.get("burst_steps"), "gt_grace": cfg.get("gt_grace")}

    # default: status
    det = agent.state.get("det")
    fitted = getattr(det, "is_fitted", False) if det is not None else False
    la = agent.state.get("last_alert")
    cfg = agent.state.get("cfg", {})
    sb = _scoreboard_view(agent)
    return (f"aif-anomaly status:\n"
            f"  detector loaded: {det is not None} (is_fitted={fitted})\n"
            f"  detector path:   {cfg.get('detector_path')}\n"
            f"  module/class:    {cfg.get('detector_module')}.{cfg.get('detector_class')}\n"
            f"  obs steps seen:  {agent.state.get('n_steps', 0)}\n"
            f"  raw alerts:      {sb['raw_alerts']}\n"
            f"  episodes:        {sb['episodes']}  (TP {sb['tp_episodes']} / FP {sb['fp_episodes']})\n"
            f"  precision:       {sb['precision']}   recall: {sb['recall']} "
            f"({sb['gt_events_detected']}/{sb['gt_events_seen']} GT events)\n"
            f"  sources:         {sb['source_breakdown']}\n"
            f"  collapse window: {cfg.get('burst_steps')} steps   gt_grace: {cfg.get('gt_grace')}\n"
            f"  raw alerts:      {'on' if cfg.get('publish_raw', False) else 'off'}\n"
            f"  episode topic:   {agent.state.get('episode_topic')}\n"
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
#                           "(detector_aif_v6.pkl). Subscribes to the global obs "
#                           "topic, publishes alerts on .../anomaly, and records "
#                           "them in Fuseki with provenance (agent-scoped IRIs) so "
#                           "it can be scored alongside the forecast detector.",
#           "capabilities": ["sinergym", "anomaly_detection", "fault_detection",
#                            "monitoring", "active_inference", "aif", "hvac_fault",
#                            "sensor_drift"],
#           "install":      ["torch", "numpy"],
#           "input_schema": {
#               "action":          "str — optional: status | reset | config",
#               "detector_path":   "str — path to detector_aif_v6.pkl",
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
