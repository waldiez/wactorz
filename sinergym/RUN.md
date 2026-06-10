# Sinergym demo — quick run

The happy-path cheat sheet. For the full explanation see [`SETUP.md`](SETUP.md);
for the Linux/Docker specifics see [`docker/LOCAL_SETUP.md`](docker/LOCAL_SETUP.md).

## 0. Prerequisites (must already be up)

- **wactorz** running (the `@…` lines below go in its **chat**, not a shell)
- **mosquitto** (MQTT :1883, WS :9001) and **Fuseki** (:3030, `sinergym` dataset)
- Bridge image built (one-time, from this `sinergym/` dir):

```sh
# docker build -f docker/Dockerfile.bridge -t wactorz-sinergym-bridge:3.11.0-ep24.1.0 .
# Fuseki dataset (only needed once / after a `docker prune` — it is persistent TDB2):
# curl -s -u admin:admin -X POST "http://localhost:3030/\$/datasets" --data "dbType=tdb2&dbName=sinergym"
```

## 1. wactorz chat — spawn agents, then launch the fleet

Order matters: agents must be subscribed **before** the bridge starts (episode-start
`env_info` is not retained).

```
@catalog spawn sinergym-labeler
@catalog spawn maddpg-fleet
@catalog spawn sinergym-anomaly
@catalog spawn sinergym-hsml
```

Launch the 15-zone fleet — **one line**, no spaces inside keys/zone names, Linux paths,
`env_id` must equal the bridge's `--env`:

```
@maddpg-fleet {"action":"launch","env_id":"officeMedium-multiagent","model_path":"/home/tam/Projects/waldiez/wactorz/state/maddpg_office/model.pt","normalizer_path":"/home/tam/Projects/waldiez/wactorz/state/maddpg_office/normalizer.npz","zones":["Core_bottom","Core_mid","Core_top","Perimeter_bot_ZN_1","Perimeter_bot_ZN_2","Perimeter_bot_ZN_3","Perimeter_bot_ZN_4","Perimeter_mid_ZN_1","Perimeter_mid_ZN_2","Perimeter_mid_ZN_3","Perimeter_mid_ZN_4","Perimeter_top_ZN_1","Perimeter_top_ZN_2","Perimeter_top_ZN_3","Perimeter_top_ZN_4"]}
```

Sanity check (after the bridge starts, `env_info_seen` should be `true`):

```
@maddpg-fleet     {"action":"status"}
@sinergym-anomaly {"action":"status"}
```

The host-side agents default to `http://localhost:3030`, so no `config` step is needed
for a local Fuseki.

## 2. Shell — start the bridge, then the viz

```sh
# from sinergym/ — --clean wipes the persistent dataset so runs don't accumulate
./docker/run-bridge.sh --clean --inject-anomalies --anomaly-seed 5

# digital-twin viz → http://localhost:8200  (separate terminal, long-running)
cd ./viz && ./serve.sh
```

`serve.sh` builds the web app on first run (if `dist/` is missing). After editing
`viz/web/src`, rebuild the static bundle:

```sh
cd viz/web && bun run build      # or: ./viz/serve.sh --dev  (Vite hot-reload on :5180)
```

`run-bridge.sh` already sets `--env officeMedium-multiagent`, the 15 `--zones`, and the
Fuseki/MQTT endpoints; extra flags are appended.

## Notes

- **Watch live** at http://localhost:8200 — occupancy, rain, day/night and per-zone temps
  are **live-only** (not stored in Fuseki, so replay shows temps + day/night only).
- **Rain** is currently **pinned to Port Angeles WA** in `officeMedium_multiagent.yaml`
  (rainy site) so the rain view fires. Revert the commented lines there for the
  3-climate rotation.
- If a zone keeps timing out or the sim crawls: the fleet `env_id` ≠ the bridge `--env`,
  a zone name has a typo, or `torch` failed to load in the wactorz process (restart it).
