# Sinergym Digital-Twin Viz

A standalone, read-only visualization of a Sinergym run: a 3D building twin
generated **live from the epJSON** (not hardcoded), with each zone tinted by
temperature, a weather rail, a sim clock, per-zone readouts, and an anomaly feed.

## Architecture (decoupled by design — portable to other projects)

```
browser ──ws:9001 (MQTT)──► mosquitto      live obs / actions / anomalies  (Tier 1)
        ──/api/geometry────► server/app.py  building meshes from the epJSON
        ──/api/sparql──────► server→Fuseki   history (CORS proxy)
        ──ws:8888 (later)──► wactorz monitor hsml "ask the run"            (Tier 2)
```

- **Tier 1** (twin, weather, anomalies, metrics) needs only MQTT + the epJSON —
  *wactorz-independent*.
- **Tier 2** (natural-language Q&A via `sinergym-hsml`) needs a wactorz runtime +
  an LLM. Planned next.

Nothing here imports wactorz or sinergym — it consumes the MQTT topics and the
epJSON, so the whole `viz/` folder travels with the addon bundle.

## Run

```bash
./serve.sh            # build + serve on http://localhost:8200
./serve.sh --dev      # hot-reload dev (Vite :5180, API :8200)
```

Then stream a run into it:
```bash
../docker/run-bridge.sh --inject-anomalies --anomaly-seed 5
```
Spawn the agents first (so the labeler/fleet publish), per ../docker/LOCAL_SETUP.md.

## Data contracts consumed

| Source | Used for |
|---|---|
| `…/observation/labeled` (labeler) | zone temps (`air_temperature_<zone>`), weather, HVAC, clock, fault ground-truth in `info` |
| `…/zone/<zone>/action` | learns `zone_index → zone` map (to flash the right zone on alerts) |
| `…/anomaly` (sinergym-anomaly) | anomaly feed + zone flash |
| `GET /api/geometry` | 3D meshes (zones/surfaces/vertices from the epJSON) |
| `POST /api/sparql` | Fuseki history (not yet wired into a scrubber) |

## Files

- `server/geometry.py` — epJSON → renderable geometry document
- `server/app.py` — stdlib server: `/api/geometry`, `/api/sparql` proxy, static hosting
- `web/` — Vite + TypeScript + Babylon.js app (`src/twin.ts`, `src/data.ts`, `src/ui.ts`)
