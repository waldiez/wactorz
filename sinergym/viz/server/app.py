"""
Sinergym viz server — the one command that serves the visualization.

Dependency-free (Python stdlib only) so the whole viz bundle is portable. It does
three small things:

  GET  /api/geometry          → building meshes parsed live from the epJSON
                                 (?building=/path.epJSON to override)
  POST /api/sparql            → proxy to Fuseki (adds CORS; browsers can't hit
                                 Fuseki cross-origin directly)
  GET  /*                     → static files (the built web app, or web/ in dev)

Live obs/action/anomaly data does NOT flow through here — the browser subscribes
to mosquitto over WebSocket (:9001) directly. This server is only geometry +
history proxy + static hosting.

Env:
  VIZ_PORT        (default 8200)   port to listen on
  EPJSON          (default ../../ASHRAE901_OfficeMedium…epJSON)  building model
  FUSEKI_URL      (default http://localhost:3030)
  FUSEKI_DATASET  (default sinergym)
  FUSEKI_USER / FUSEKI_PASSWORD   (default admin/admin)
  WEB_ROOT        (default ../web/dist if present else ../web)
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from geometry import extract

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("VIZ_PORT", "8200"))
EPJSON = os.environ.get(
    "EPJSON",
    os.path.join(HERE, "..", "..", "ASHRAE901_OfficeMedium_STD2019_Denver_MultiAgent.epJSON"),
)
FUSEKI_URL = os.environ.get("FUSEKI_URL", "http://localhost:3030").rstrip("/")
FUSEKI_DATASET = os.environ.get("FUSEKI_DATASET", "sinergym")
FUSEKI_USER = os.environ.get("FUSEKI_USER", "admin")
FUSEKI_PASSWORD = os.environ.get("FUSEKI_PASSWORD", "admin")

_dist = os.path.join(HERE, "..", "web", "dist")
WEB_ROOT = os.path.abspath(
    os.environ.get("WEB_ROOT", _dist if os.path.isdir(_dist) else os.path.join(HERE, "..", "web"))
)

_MIME = {
    ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
    ".ico": "image/x-icon", ".map": "application/json", ".png": "image/png",
    ".woff2": "font/woff2",
}


_SGY = "https://waldiez.github.io/wactorz/sinergym#"


def _sparql(query: str) -> list[dict]:
    url = f"{FUSEKI_URL}/{FUSEKI_DATASET}/sparql"
    req = urllib.request.Request(url, data=query.encode(), method="POST")
    req.add_header("Content-Type", "application/sparql-query")
    req.add_header("Accept", "application/sparql-results+json")
    token = base64.b64encode(f"{FUSEKI_USER}:{FUSEKI_PASSWORD}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["results"]["bindings"]


def build_history(episode: str, frames: int) -> dict:
    """Decimated zone-temperature replay timeline for one episode, from Fuseki.

    Duplicate (step,zone) rows (overlapping runs in the persistent dataset) are
    collapsed with AVG. Weather is live-only (the observations graph is retained
    for the tail only), so the scrubber focuses on zone temps + setpoints.
    """
    # resolve episode + step range
    eps = _sparql(f"""PREFIX sgy:<{_SGY}>
      SELECT ?ep (MAX(?s) AS ?hi) (COUNT(DISTINCT ?s) AS ?n) WHERE {{
        GRAPH <urn:sinergym:zones> {{ ?x sgy:episode ?ep ; sgy:step ?s }}
      }} GROUP BY ?ep ORDER BY DESC(?ep)""")
    if not eps:
        return {"episode": None, "steps": [], "zones": {}, "anomalies": []}
    chosen = eps[-1] if episode == "latest" else next((e for e in eps if e["ep"]["value"] == episode), eps[-1])
    ep = int(chosen["ep"]["value"])
    hi = int(float(chosen["hi"]["value"]))
    stride = max(3, round((hi / frames) / 3) * 3)  # keep a multiple of the 3-step sampling

    # Key off action_htg/action_clg — present for all 15 agent zones. airTemperature
    # is a bonus (only the 3 core zones persisted it correctly for this env).
    rows = _sparql(f"""PREFIX sgy:<{_SGY}>
      SELECT ?step ?zone (AVG(?h) AS ?htg) (AVG(?c) AS ?clg) (AVG(?t) AS ?temp) WHERE {{
        GRAPH <urn:sinergym:zones> {{
          ?s sgy:episode {ep} ; sgy:step ?step ; sgy:zone ?zone ;
             sgy:action_htg ?h ; sgy:action_clg ?c .
          OPTIONAL {{ ?s sgy:airTemperature ?t }}
        }}
        FILTER(?step - floor(?step / {stride}) * {stride} = 0)
      }} GROUP BY ?step ?zone ORDER BY ?step""")

    steps_set = sorted({int(r["step"]["value"]) for r in rows})
    idx = {s: i for i, s in enumerate(steps_set)}
    n = len(steps_set)
    htg: dict[str, list] = {}
    clg: dict[str, list] = {}
    temp: dict[str, list] = {}
    for r in rows:
        z = r["zone"]["value"]
        htg.setdefault(z, [None] * n); clg.setdefault(z, [None] * n); temp.setdefault(z, [None] * n)
        i = idx[int(r["step"]["value"])]
        htg[z][i] = round(float(r["htg"]["value"]), 2)
        clg[z][i] = round(float(r["clg"]["value"]), 2)
        if "temp" in r and r["temp"].get("value") is not None:
            temp[z][i] = round(float(r["temp"]["value"]), 2)
    zones = list(htg.keys())
    temp_zones = [z for z, series in temp.items() if any(v is not None for v in series)]

    # anomalies (may be empty if the detector agent didn't run)
    anomalies = []
    try:
        arows = _sparql(f"""PREFIX sgy:<{_SGY}>
          SELECT ?step ?kind ?sev ?zi WHERE {{
            GRAPH <urn:sinergym:anomalies> {{
              ?a a sgy:Alert ; sgy:episode {ep} ; sgy:step ?step ; sgy:kindGuess ?kind .
              OPTIONAL {{ ?a sgy:severity ?sev }} OPTIONAL {{ ?a sgy:zoneIndex ?zi }}
            }} }} ORDER BY ?step""")
        for r in arows:
            anomalies.append({
                "step": int(r["step"]["value"]),
                "kind": r["kind"]["value"],
                "severity": float(r["sev"]["value"]) if "sev" in r else 0.0,
                "zoneIdx": int(r["zi"]["value"]) if "zi" in r else None,
            })
    except Exception:  # noqa: BLE001
        pass

    return {"episode": ep, "steps": steps_set, "stride": stride,
            "zones": zones, "tempZones": temp_zones,
            "htg": htg, "clg": clg, "temp": temp, "anomalies": anomalies}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logs
        pass

    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/geometry":
            building = EPJSON
            if "?" in self.path:
                from urllib.parse import parse_qs
                q = parse_qs(self.path.split("?", 1)[1])
                building = q.get("building", [EPJSON])[0]
            try:
                geo = extract(building)
                self._send(200, json.dumps(geo).encode())
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}).encode())
            return
        if path == "/api/health":
            self._send(200, json.dumps({"ok": True, "building": os.path.basename(EPJSON),
                                        "fuseki": FUSEKI_URL}).encode())
            return
        if path == "/api/history":
            self._history()
            return
        self._serve_static(path)

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/api/sparql":
            self._proxy_sparql()
            return
        self._send(404, b'{"error":"not found"}')

    # ── history replay (decimated timeline from Fuseki) ───────────────────────
    def _history(self):
        from urllib.parse import parse_qs
        q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        episode = q.get("episode", ["latest"])[0]
        frames = max(60, min(2000, int(q.get("frames", ["720"])[0])))
        try:
            data = build_history(episode, frames)
            self._send(200, json.dumps(data).encode())
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}).encode())

    # ── helpers ──────────────────────────────────────────────────────────────
    def _proxy_sparql(self):
        length = int(self.headers.get("Content-Length", 0))
        query = self.rfile.read(length)
        url = f"{FUSEKI_URL}/{FUSEKI_DATASET}/sparql"
        req = urllib.request.Request(url, data=query, method="POST")
        req.add_header("Content-Type", "application/sparql-query")
        req.add_header("Accept", "application/sparql-results+json")
        token = base64.b64encode(f"{FUSEKI_USER}:{FUSEKI_PASSWORD}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                self._send(200, r.read())
        except Exception as e:  # noqa: BLE001
            self._send(502, json.dumps({"error": str(e)}).encode())

    def _serve_static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.abspath(os.path.join(WEB_ROOT, rel))
        if not full.startswith(WEB_ROOT):
            self._send(403, b'{"error":"forbidden"}')
            return
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            # SPA fallback
            full = os.path.join(WEB_ROOT, "index.html")
            if not os.path.isfile(full):
                self._send(404, b'{"error":"not found"}', "application/json")
                return
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            body = f.read()
        self._send(200, body, _MIME.get(ext, "application/octet-stream"))


def main():
    print(f"[viz] building : {EPJSON}")
    print(f"[viz] web root : {WEB_ROOT}")
    print(f"[viz] fuseki   : {FUSEKI_URL}/{FUSEKI_DATASET}")
    print(f"[viz] serving  : http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
