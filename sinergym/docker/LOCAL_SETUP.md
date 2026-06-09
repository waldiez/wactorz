# Linux setup — container-only alternative to `../SETUP.md`

`../SETUP.md` is the **Windows** runbook: it builds a VS Code **devcontainer** because
that's how you get a Linux EnergyPlus on Windows. On **Linux** you don't need the
devcontainer — the wactorz agents run natively on the host, and only the
Sinergym/EnergyPlus **bridge** needs a (plain) container.

This doc is that Linux path, end to end, with the commands used and real logs proving it
ran. It changes *nothing* about the policy, the agents, or the data contracts — only the
environment plumbing differs.

## What changed vs the original branch

| Topic | `../SETUP.md` (Windows) | This path (Linux) |
|---|---|---|
| EnergyPlus + Sinergym | hand-built devcontainer: 3.11.0 source, Dockerfile edited to force EnergyPlus 24.1.0 (§3a/§3b) | `docker/Dockerfile.bridge`: base `sailugr/sinergym:v3.10.0-lite` (already EnergyPlus 24.1.0) + `pip install sinergym==3.11.0` — same end state, no from-source EnergyPlus build |
| Image size | full devcontainer | `-lite` base (0.7 GB pull) — the DRL/torch stack is dropped because inference runs host-side in wactorz |
| MQTT + Fuseki | three Docker services started by hand | already running via repo `compose.yaml` (`wactorz-mosquitto`, `wactorz-fuseki`); only the `sinergym` dataset had to be created |
| Model/detector dir | `C:/Users/pkasn/.../state/maddpg_office/` | `state/maddpg_office/` (host paths; relative paths work in the launch payload) |
| Host Python deps | n/a (host = Windows GUI) | `torch==2.12.0+cpu` + `numpy` added to the wactorz `.venv` (py3.14) |
| `host.docker.internal` | automatic on Docker Desktop | mapped explicitly on Linux via `--add-host=host.docker.internal:host-gateway` (in `run-bridge.sh`) |
| Bridge `--env` / topics | unchanged | unchanged (`officeMedium-multiagent`) |

Files added by this path (everything else is the original branch):
`docker/Dockerfile.bridge`, `docker/run-bridge.sh`, `docker/LOCAL_SETUP.md` (this file).

---

## One-time setup

```bash
# 0. (services already up via compose.yaml: wactorz-mosquitto :1883, wactorz-fuseki :3030)

# 1. Create the persistent Fuseki dataset the bridge writes to
curl -s -u admin:admin -X POST "http://localhost:3030/\$/datasets" \
  --data "dbType=tdb2&dbName=sinergym"

# 2. Host-side ML deps for the wactorz agents (CPU-only torch — inference, no GPU needed)
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://pypi.org/simple "torch==2.12.0+cpu" numpy

# 3. Model/detector files where the fleet expects them
mkdir -p state/maddpg_office && cp sinergym/maddpg_office/* state/maddpg_office/

# 4. Build the bridge image (from wactorz/sinergym/)
cd sinergym
docker build -f docker/Dockerfile.bridge -t wactorz-sinergym-bridge:3.11.0-ep24.1.0 .
```

Verified versions inside the built image / host venv:

```
EnergyPlus, Version 24.1.0-9d7789a3ac
Sinergym 3.11.0
host torch 2.12.0+cpu | numpy 2.4.6
```

---

## Run sequence (order matters — agents before bridge; SETUP §6)

In the wactorz console:

```
@catalog spawn sinergym-labeler
@catalog spawn maddpg-fleet
@catalog spawn sinergym-anomaly
@catalog spawn sinergym-hsml
@sinergym-hsml    {"action":"config","fuseki_url":"http://localhost:3030"}
@sinergym-anomaly {"action":"config","fuseki_url":"http://localhost:3030"}
```

Launch the 15-zone fleet — `env_id` MUST be explicit (the recipe default differs), paths
may be relative to the repo root, `infer_dir` is auto-derived from `model_path`:

```
@maddpg-fleet {"action":"launch","env_id":"officeMedium-multiagent","model_path":"state/maddpg_office/model.pt","normalizer_path":"state/maddpg_office/normalizer.npz","zones":["Core_bottom","Core_mid","Core_top","Perimeter_bot_ZN_1","Perimeter_bot_ZN_2","Perimeter_bot_ZN_3","Perimeter_bot_ZN_4","Perimeter_mid_ZN_1","Perimeter_mid_ZN_2","Perimeter_mid_ZN_3","Perimeter_mid_ZN_4","Perimeter_top_ZN_1","Perimeter_top_ZN_2","Perimeter_top_ZN_3","Perimeter_top_ZN_4"]}
```

Start the bridge **last** (env_info + first obs at episode start are not retained):

```bash
./docker/run-bridge.sh                                    # clean eval run
./docker/run-bridge.sh --inject-anomalies --anomaly-seed 5   # with anomaly injection
```

---

## Verification (commands + captured output from a real run)

**Bridge stepping + anomaly injection** (from the bridge stdout):
```
[ENVIRONMENT] (INFO) : Episode 1 started.
[ENV] reward components: [...'hvac_efficiency', 'hvac_fault_active']
⚠ [ANOMALY START step=1492] HVAC eff 65% — winter fault (day 15)
✓ [ANOMALY END   step=1516] hvac_fault resolved
```
> The one-off `Zone '...' timed out — using last known action (htg=18.50, clg=27.00)`
> warnings at **step 0 only** are expected: first-inference warmup before the fleet
> answers the first obs. They must not recur after warmup.

**All 15 zones are actually acting** (not coasting on last-known):
```bash
docker exec wactorz-mosquitto \
  mosquitto_sub -t "sinergym/env/officeMedium-multiagent/zone/+/action" -v -W 3
```
```
.../zone/Core_bottom/action       {"action":[1.0,-1.0],     "agent":"maddpg-zone-00","step":6963,...}
.../zone/Core_mid/action          {"action":[-1.0,-1.0],    "agent":"maddpg-zone-01",...}
.../zone/Perimeter_bot_ZN_3/action{"action":[-1.0,0.9753],  "agent":"maddpg-zone-05",...}
...                                (15 distinct zones, varied per-zone htg/clg)
```

**Fuseki is filling** (host side → `localhost`):
```bash
curl -s -u admin:admin -H 'Content-Type: application/sparql-query' \
  --data 'SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g' \
  http://localhost:3030/sinergym/sparql
```
```
urn:sinergym:observations    36300
urn:sinergym:zones          508367
urn:sinergym:episodes            7
urn:sinergym:catalog           689
```

**All 15 agents writing decisions** (SETUP §9 sanity):
```bash
curl -s -u admin:admin -H 'Content-Type: application/sparql-query' \
  --data 'PREFIX sgy: <https://waldiez.github.io/wactorz/sinergym#>
          PREFIX prov: <http://www.w3.org/ns/prov#>
          SELECT (COUNT(DISTINCT ?agent) AS ?agents) WHERE {
            GRAPH <urn:sinergym:zones> { ?s sgy:action_htg ?h ; prov:wasAttributedTo ?agent } }' \
  http://localhost:3030/sinergym/sparql
#  -> agents = 15
```

**Agent self-checks** (in the wactorz console):
```
@maddpg-fleet     {"action":"status"}   # 15 children maddpg-zone-00..14, env_info_seen=true
@sinergym-anomaly {"action":"status"}   # detector loaded, steps climbing, write failures=0
```

---

## Notes / gotchas (all confirmed in this setup)

- **Custom-Reward disabled is expected.** `maddpg_v3` / `ensemble_controller` aren't in
  the repo, so the bridge prints `[CUSTOM-REWARD] Disabled` — only the Custom-Reward and
  per-month summary lines are omitted; everything else runs (SETUP §13).
- **The bridge writes sim output into `sinergym/` as root.** Running the container as root
  leaves `sinergym/.lock`, `sinergym/eplus-env-*-res*/` owned by root (gitignored — see
  below). To keep them yours, add `--user "$(id -u):$(id -g)"` to the `docker run` in
  `run-bridge.sh`, or `sudo chown -R "$USER" sinergym/` afterwards. Same root-ownership
  pattern can hit `state/*.db` if you ever run the dockerized Python backend — fix with
  `sudo chown -R "$USER" state/`.
- **`localhost` vs `host.docker.internal` (SETUP §10).** Host-side agents (`sinergym-hsml`,
  `sinergym-anomaly`) use `localhost:3030`; the in-container bridge uses
  `host.docker.internal:3030`. `run-bridge.sh` maps the latter for Linux.
- **Multiple runs accumulate in Fuseki — wipe between runs.** The dataset is persistent
  (TDB2) and every run numbers episodes from 1, so back-to-back runs pile up and collide
  on `(step, zone)`; the replay/SPARQL then averages a *mix* of runs. Use
  `./run-bridge.sh --clean` to wipe the dataset before a run (or
  `curl -u admin:admin -X POST http://localhost:3030/sinergym/update --data-urlencode 'update=CLEAR ALL'`).
  The **proper** long-term fix (for the delivered addon) is to stamp every record with a
  unique **run id** (e.g. `sgy:run "<ts-uuid>"`) and have the viz/queries default to the
  latest run — then multiple runs coexist cleanly instead of needing a wipe.
