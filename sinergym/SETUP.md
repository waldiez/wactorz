# MADDPG → wactorz Deployment Runbook

Deploy a trained 15-zone MADDPG building-HVAC policy as inference-only agents inside
the **wactorz** actor framework, bridging **Sinergym (EnergyPlus)** ↔ **MQTT** ↔
**Fuseki (RDF triplestore)**, with live anomaly injection + detection and a
natural-language Q&A agent over the data.

This document is a complete, from-scratch setup guide. Follow the parts in order.

> **Prefer a plain container (no VS Code devcontainer)?** This runbook builds a
> VS Code devcontainer, but the container-only path works the same on Linux,
> macOS, and Docker-on-Windows — see [`docker/LOCAL_SETUP.md`](docker/LOCAL_SETUP.md).
> That path uses a prebuilt **Sinergym 3.12.0 / EnergyPlus 25.1.0** image instead
> of the from-source build below. (This runbook targets **24.1.0 / 3.11.0**, the
> versions the bundled policy was trained on — see §3.)

---

## 1. Architecture at a glance

```
                 ┌─────────────────────────────────────────────────────────┐
                 │  Sinergym container (EnergyPlus 24.1.0 + Sinergym 3.11.0) │
                 │                                                           │
   anomalies ───►│  AnomalyEnvWrapper → EnergyPlus env → CustomRewardWrapper │
                 │                          │                                │
                 │                  sinergym_bridge (the bridge)             │
                 └───────────┬───────────────────────────┬──────────────────┘
                             │ publishes obs              │ writes triples
                             ▼                            ▼
        ┌──────────────────────────────┐        ┌──────────────────────┐
        │        MQTT broker            │        │      Fuseki           │
        │  sinergym/env/<env_id>/...    │        │  dataset "sinergym"   │
        └───────┬───────────────┬───────┘        └──────────┬───────────┘
                │ obs           │ actions                   │ SPARQL
                ▼               ▲                            ▼
        ┌───────────────────────────────────────────────────────────────┐
        │                       wactorz (host)                           │
        │  maddpg-fleet   (15 zone agents: obs → action)                 │
        │  sinergym-labeler (obs → keyed/flat obs on .../observation/labeled) │
        │  sinergym-anomaly (obs → detector → alerts on .../anomaly + Fuseki) │
        │  sinergym-hsml    (natural language → SPARQL → answer)          │
        │  sinergym-schema  (optional: obs index↔name map)               │
        └───────────────────────────────────────────────────────────────┘
```

**Two hosts, one critical networking rule** (see §10):
- The **bridge** runs *inside the Sinergym container* → reaches Fuseki/MQTT at `host.docker.internal`.
- **wactorz agents** run *on the Windows host* → must reach Fuseki at `localhost`.

---

## 2. Prerequisites — three Docker services

Start these first (any order):

1. **MQTT broker** (e.g. Eclipse Mosquitto) on port **1883**.
2. **Fuseki** on port **3030**, with a dataset named **`sinergym`**, user/pass `admin`/`admin`.
   - Use a **persistent (TDB2)** dataset if you want data to survive a Fuseki restart;
     an in-memory dataset is wiped on restart.
3. **Sinergym container** — the dev container described in Part 3.

> Sanity: open `http://localhost:3030` and confirm the `sinergym` dataset exists.

---

## 3. Sinergym container — exact versions (critical)

The policy was **trained on EnergyPlus 24.1.0 / Sinergym 3.11.0**. The epJSON and the
checkpoint will silently misbehave on other EnergyPlus versions. Pin them.

### 3a. `.devcontainer/devcontainer.json`
```jsonc
"build": {
    "args": {
        "ENERGYPLUS_VERSION": "24.1.0",
        "ENERGYPLUS_INSTALL_VERSION": "24-1-0",
        "ENERGYPLUS_SHA": "9d7789a3ac",
        "WANDB_API_KEY": "${localEnv:WANDB_API_KEY}"
    }
},
```

### 3b. `.devcontainer/Dockerfile`
Two edits to the EnergyPlus download URL (the default points at the 25.1 release):
```dockerfile
# Drop the "-WithDSOASpaceListFixes" suffix (only exists for 25.1):
ENV ENERGYPLUS_DOWNLOAD_BASE_URL=https://github.com/NREL/EnergyPlus/releases/download/$ENERGYPLUS_TAG

# 24.1.0 ships an Ubuntu 22.04 installer (runs fine on the 24.04 base image):
ENV ENERGYPLUS_DOWNLOAD_FILENAME=EnergyPlus-$ENERGYPLUS_VERSION-$ENERGYPLUS_SHA-Linux-Ubuntu22.04-x86_64.sh
```
These resolve to the working URL:
`https://github.com/NREL/EnergyPlus/releases/download/v24.1.0/EnergyPlus-24.1.0-9d7789a3ac-Linux-Ubuntu22.04-x86_64.sh`

### 3c. Sinergym 3.11.0
Sinergym is installed by `poetry install` from the repo checkout, **not** the build args.
If this folder is the Sinergym source repo, check out the matching tag **before** rebuilding:
```bash
git checkout v3.11.0
# (re-apply your Dockerfile/devcontainer edits if the checkout reverts them)
```

### 3d. Rebuild + verify
VS Code → **Dev Containers: Rebuild Container**, then inside the container:
```bash
energyplus --version    # expect: EnergyPlus, Version 24.1.0-9d7789a3ac
pip show sinergym        # expect: Version: 3.11.0
```

> **What survives a rebuild?** Anything under the bind-mounted workspace
> (`/workspaces/sinergym` ↔ your Windows folder) is safe. Things written only inside
> the container (extra `pip install`s, `/tmp`, `/root`) are lost — keep important files
> in the workspace.

---

## 4. File placement map

### 4a. Inside the Sinergym container (workspace, next to `register_env.py`)
| File | Purpose | Location |
|---|---|---|
| `ASHRAE901_OfficeMedium_STD2019_Denver_MultiAgent.epJSON` | the 15-zone building model | sinergym docker - /sinergym/data/buildings/ |
| `officeMedium_multiagent.yaml` | Sinergym env definition | sinergym docker - /sinergym/data/default_configuration/ |
| `register_env.py` | builds the training-parity env (`make_custom_env`) | outside the folders |
| `sinergym_bridge_anomalies.py` | **the bridge** (patched `sinergym_bridge_mas.py`) | outside the folders next to  `sinergym_bridge_anomalies.py`|

> The bridge run command below uses the name `sinergym_bridge_anomalies.py`. That is the
> patched bridge — rename `sinergym_bridge_mas.py` to that, or adjust the command.

### 4b. Model/detector directory — `…/state/maddpg_office/`
`<WACTORZ_DIR>/state/maddpg_office/` (replace `<WACTORZ_DIR>` with your absolute wactorz path)
| File | Used by |
|---|---|
| `model.pt` | maddpg-fleet (the trained actor checkpoint) |
| `normalizer.npz` | maddpg-fleet (frozen obs normalizer) |
| `maddpg_infer.py` | maddpg-fleet (training-free inference module) |
| `forecast_anomaly_detector.py` | sinergym-anomaly (detector class) |
| `detector_forecast_v9_mixed.pkl` | sinergym-anomaly (trained detector, version `forecast_v8`) |
| `anomaly_injector.py` | *(also here is fine; the bridge needs it container-side)* |

### 4c. wactorz package
| File | Location |
|---|---|
| `maddpg_fleet_agent.py` | `wactorz/.../catalogue_agents/` |
| `sinergym_labeler_agent.py` | `wactorz/.../catalogue_agents/` |
| `sinergym_anomaly_agent.py` | `wactorz/.../catalogue_agents/` |
| `sinergym_hsml_agent.py` | `wactorz/.../catalogue_agents/` |
| `sinergym_schema_agent.py` | `wactorz/.../catalogue_agents/` *(optional)* |
| `catalog_agent.py` | `wactorz/.../agents/` — patched with the REGISTER blocks |

---

## 5. Register the recipes in the catalog

Each recipe file ends with a commented **REGISTER block**. Paste each one inside
`_build_catalog()` in `catalog_agent.py` (before `return catalog`). After editing,
**restart wactorz** — recipe code is read at startup.

Agents registered: `maddpg-fleet`, `sinergym-labeler`, `sinergym-anomaly`,
`sinergym-hsml`, and optionally `sinergym-schema`. The AIF (custom
active-inference) variants `aif-fleet` and `aif-anomaly` are drop-in
alternatives to `maddpg-fleet` / `sinergym-anomaly` — see §11.

---

## 6. Startup sequence (order matters)

1. Start MQTT, Fuseki, Sinergym containers (Part 2).
2. Start **wactorz** (loads the recipes).
3. Spawn the agents:
   ```
   @catalog spawn sinergym-labeler
   @catalog spawn maddpg-fleet
   @catalog spawn sinergym-anomaly
   @catalog spawn sinergym-hsml
   ```
4. Point the **host-side** agents at Fuseki on `localhost` (see §10), not sure if this works properly!:
   ```
   @sinergym-hsml    {"action":"config","fuseki_url":"http://localhost:3030"}
   @sinergym-anomaly {"action":"config","fuseki_url":"http://localhost:3030"}
   ```
5. Launch the MADDPG fleet (15 children):
   ```
   @maddpg-fleet {"action":"launch","env_id":"officeMedium-multiagent","model_path":"<WACTORZ_DIR>/state/maddpg_office/model.pt","normalizer_path":"<WACTORZ_DIR>/state/maddpg_office/normalizer.npz","zones":["Core_bottom","Core_mid","Core_top","Perimeter_bot_ZN_1","Perimeter_bot_ZN_2","Perimeter_bot_ZN_3","Perimeter_bot_ZN_4","Perimeter_mid_ZN_1","Perimeter_mid_ZN_2","Perimeter_mid_ZN_3","Perimeter_mid_ZN_4","Perimeter_top_ZN_1","Perimeter_top_ZN_2","Perimeter_top_ZN_3","Perimeter_top_ZN_4"]}
   ```
   The 15 zones must be in **this order** (it is the actor index order the policy expects).
6. Run the bridge inside the Sinergym container (Part 7).

> **Why this order?** The agents must be subscribed *before* the bridge starts an episode,
> because `env_info` (and the first observations) are published at episode start and are
> **not retained**. Spawn/launch agents first, then start the bridge.

---

## 7. Run the bridge

From the directory containing `register_env.py`, inside the Sinergym container:

```bash
python sinergym_bridge_anomalies.py \
  --env officeMedium-multiagent --mode deploy --episodes 1 \
  --zones Core_bottom,Core_mid,Core_top,Perimeter_bot_ZN_1,Perimeter_bot_ZN_2,Perimeter_bot_ZN_3,Perimeter_bot_ZN_4,Perimeter_mid_ZN_1,Perimeter_mid_ZN_2,Perimeter_mid_ZN_3,Perimeter_mid_ZN_4,Perimeter_top_ZN_1,Perimeter_top_ZN_2,Perimeter_top_ZN_3,Perimeter_top_ZN_4 \
  --fuseki-url http://host.docker.internal:3030 --fuseki-dataset sinergym \
  --fuseki-user admin --fuseki-password admin \
  --inject-anomalies --anomaly-seed 5
```

Notes:
- `--env` **must equal** the fleet launch `env_id` (`officeMedium-multiagent`), or the
  MQTT topics won't line up.
- `--zones` is required (the default env is a 5-zone env — wrong for this model).
- **`host.docker.internal`** is correct *here* (the bridge is inside the container).
- `--inject-anomalies --anomaly-seed 5` turns on reproducible anomaly injection. **Omit
  both** for a clean run that reproduces the eval metrics exactly.

---

## 8. MQTT topic reference

Pattern: `sinergym/env/<env_id>/…` (here `<env_id>` = `officeMedium-multiagent`).

| What | Topic | Direction |
|---|---|---|
| Global obs (full 92-dim) | `…/observation` | bridge → agents (fleet consumes this) |
| Per-zone obs slice | `…/zone/<zone>/observation` | bridge → (unused by fleet) |
| **Labeled flat obs** | `…/observation/labeled` | labeler → any consumer (read by key) |
| Per-zone action | `…/zone/<zone>/action` | fleet → bridge |
| Global action | `…/action` | only in `--single-agent` |
| **Anomaly alerts** | `…/anomaly` | sinergym-anomaly → consumers |
| Episode events | `…/episode` | bridge → |
| Env schema | `…/env_info` | bridge → (at episode start, not retained) |

**Anomaly ground truth** rides the obs message `info` (when injecting): scalar keys
`hvac_fault_active`, `hvac_efficiency`, `anomaly_step`, and JSON-string keys
`anomalies_active`, `anomaly_kinds`, `anomaly_event_ids` (use `json.loads`).

Watch on the wire:
```bash
mosquitto_sub -t "sinergym/env/officeMedium-multiagent/observation" -v        # raw obs (+ info ground truth)
mosquitto_sub -t "sinergym/env/officeMedium-multiagent/observation/labeled" -v # keyed flat obs
mosquitto_sub -t "sinergym/env/officeMedium-multiagent/zone/+/action" -v       # the 15 agents' actions
mosquitto_sub -t "sinergym/env/officeMedium-multiagent/anomaly" -v             # detector alerts
```

---

## 9. Fuseki — named graphs and working SPARQL

The bridge writes to dataset **`sinergym`**, query endpoint `…/sinergym/sparql`.
Always declare PREFIXes and wrap patterns in the right `GRAPH`.

| Graph | Contents |
|---|---|
| `urn:sinergym:observations` | global per-step record (whole-building / outdoor) |
| `urn:sinergym:zones` | per-zone per-step `sgy:ZoneStep` (sampled every 3rd step) |
| `urn:sinergym:episodes` | episode metadata |
| `urn:sinergym:hourly` | downsampled hourly averages of old episodes |
| `urn:sinergym:anomalies` | `sgy:Alert` nodes from sinergym-anomaly |

**Prefixes:**
```sparql
PREFIX sgy:  <https://waldiez.github.io/wactorz/sinergym#>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
```

### Predicate naming — important
- **Live per-step `urn:sinergym:zones`** uses **`sgy:action_htg` / `sgy:action_clg`**.
- **Downsampled `urn:sinergym:hourly`** uses `sgy:action_heatingSetpoint` /
  `sgy:action_coolingSetpoint`.
- ⚠ Querying `sgy:action_heatingSetpoint` against the **zones** graph returns **nothing**.

### Actions a zone's agent chose (CORRECT for the zones graph):
```sparql
SELECT ?step ?htg ?clg ?agent WHERE {
  GRAPH <urn:sinergym:zones> {
    ?s sgy:zone "Perimeter_top_ZN_1" ; sgy:step ?step ;
       sgy:action_htg ?htg ; sgy:action_clg ?clg .
    OPTIONAL { ?s prov:wasAttributedTo ?agent }
  }
} ORDER BY ?step LIMIT 50
```

### Per-agent decision count (sanity: all 15 writing):
```sparql
SELECT ?agent (COUNT(*) AS ?n) WHERE {
  GRAPH <urn:sinergym:zones> { ?s sgy:action_htg ?h ; prov:wasAttributedTo ?agent }
} GROUP BY ?agent ORDER BY ?agent
```

### Detected anomalies:
```sparql
SELECT ?step ?episode ?kind ?severity ?zone WHERE {
  GRAPH <urn:sinergym:anomalies> {
    ?a a sgy:Alert ; sgy:step ?step ; sgy:episode ?episode ;
       sgy:kindGuess ?kind ; sgy:severity ?severity .
    OPTIONAL { ?a sgy:zoneIndex ?zone }
  }
} ORDER BY ?episode ?step LIMIT 200
```

### Quick "is anything there?" checks:
```sparql
SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g
```

> **Integer literals:** `sgy:step` / `sgy:episode` are `xsd:integer` — write them bare
> (`sgy:step 18000`), never quoted.

---

## 10. The host.docker.internal vs localhost rule (read this)

- The **bridge** runs **inside the Sinergym container** → use `host.docker.internal:3030`
  (and the broker hostname that resolves inside the container). This is in the bridge
  run command and is correct as written.
- **wactorz agents** (`sinergym-hsml`, `sinergym-anomaly`) run **on the Windows host** →
  `host.docker.internal` does **not** resolve there; use **`http://localhost:3030`**.
  Set it once per spawn:
  ```
  @sinergym-hsml    {"action":"config","fuseki_url":"http://localhost:3030"}
  @sinergym-anomaly {"action":"config","fuseki_url":"http://localhost:3030"}
  ```

Symptom if you forget: agent reports `WinError 10060` / timeout, and Fuseki/HSML show 0
rows / `write failures` climb in `@sinergym-anomaly {"action":"status"}`.

---

## 11. Using the agents

### maddpg-fleet
`launch` (params above) | `stop` | `status`. Spawns 15 supervised children
(`maddpg-zone-00..14`), each subscribing to global obs, inferring, and publishing a
normalized `[-1,1]` action to its zone's action topic with provenance
(`agent`, `policy`).

### aif-fleet (custom active inference — alternative to maddpg-fleet)
Same `launch` / `stop` / `status` interface and 15 supervised children
(`aif-zone-00..14`), but driven by a custom active-inference policy instead of the
DRL model. `model_path` is the `aif_model.pkl` (no `normalizer_path`); the launch
also takes the comfort/energy band + weight params (`heat_low/high`, `cool_low/high`,
`policy_len`, `energy_weight`, `comfort_weight`, `epistemic_weight`, …) — see the
launch line in [`RUN.md`](RUN.md) / [`docker/LOCAL_SETUP.md`](docker/LOCAL_SETUP.md).

### aif-anomaly (alternative to sinergym-anomaly)
Same role as `sinergym-anomaly` (obs-watching detector → `…/anomaly` + Fuseki
`sgy:Alert` triples) but loads the AIF detector (`detector_aif_v6.pkl`). Use it with
`aif-fleet`.

### sinergym-labeler
Republishes obs as a **flat keyed dict** on `…/observation/labeled` — read variables by
name (`payload["air_temperature_Core_mid"]`). No task delegation; pure MQTT.
`@sinergym-labeler {"action":"status"}` shows whether the schema is cached.

### sinergym-anomaly
Loads `detector_forecast_v9_mixed.pkl`, watches global obs, calls `detector.update(obs, info)`,
publishes alerts to `…/anomaly`, and writes `sgy:Alert` triples (with provenance) to Fuseki.
```
@sinergym-anomaly {"action":"status"}   # detector loaded? steps seen, alerts, last alert, write failures
@sinergym-anomaly {"action":"reset"}    # reset the detector's episode window
```

### sinergym-hsml
Natural-language → SPARQL → answer over Fuseki (zones / observations / **anomalies**).
Returns prose with the generated SPARQL appended.
```
@sinergym-hsml {"action":"refresh"}     # re-discover schema (do after first injected episode)
@sinergym-hsml what setpoints did maddpg-zone-11 choose near step 20000?
@sinergym-hsml which agent controls Perimeter_top_ZN_1?
@sinergym-hsml what anomalies were detected, and of what kind?
```
Requires an LLM provider configured on main.

### sinergym-schema (optional)
Reports the obs index↔name map / action bounds / reward components from `env_info`.
The labeler covers most needs; keep schema if you want a planner-discoverable schema source.

---

## 12. Expected results (tolerance)

Run the bridge **without** `--inject-anomalies` to reproduce the script eval.

| Metric | Script (maddpg_v3) | wactorz (observed) | Match |
|---|---|---|---|
| Custom Reward | -232.53 | reported in summary | ≈ (see note) |
| Original Reward | -121,265.84 | ≈ | ✓ |
| Energy | 110,530 kWh | 111,584 kWh (+0.95%) | ✓ |
| Zone-Comfort Rate | 59.8% | 59.7% | ✓ |
| Mean Deviation | 0.300 °C | 0.299 °C | ✓ |
| Deadband Violations | 17.3% | 18.4% | ✓ |
| Total steps | 35,040 | 35,039 | ✓ |

The small residual is **expected**: the bridge primes the first env step and has a
one-step action lag over MQTT, which the standalone script does not. Over ~35k steps
this is a fraction of a percent. The **Custom Reward** and per-month lines come from the
exact training `CustomRewardWrapper` (so the math matches by construction), printed in the
bridge summary as:
```
Month  1 [W] | R:  -88.58 | T:  20.2°C [18.2-23.8] | CS: 0.699 (occ:0.721) | ZCR: 56.8% | Dev: 0.825°C
...
EVALUATION RESULTS:
  Custom Reward:  -232.53 (mean/step: -0.0066)
```

---

## 13. Troubleshooting

**"Observation queue empty / EnergyPlus init failing" at reset**
Normal. Sinergym returns a placeholder obs from `reset()`; the first real obs comes from
the first `step()`. The bridge primes with a midpoint action until a real obs (valid month)
arrives. If it never recovers, you're on the wrong EnergyPlus version (see §3).

**EnergyPlus download 404 on rebuild**
The default URL has a `-WithDSOASpaceListFixes` suffix and an `Ubuntu24.04` filename that
don't exist for 24.1.0. Apply the §3b edits (drop suffix, use `Ubuntu22.04`).

**`ModuleNotFoundError: No module named 'maddpg_infer'`** (fleet children)
`maddpg_infer.py` must sit in the model folder (`…/state/maddpg_office/`) next to `model.pt`.
Optionally pass `"infer_dir":"…/state/maddpg_office"` in the launch payload.

**`[CUSTOM-REWARD] Disabled (could not import training code)`**
The bridge couldn't import `maddpg_v3`. Place them in
the container next to `register_env.py`. Without them the run still works — it just omits the
Custom Reward and per-month lines.

**Fuseki / HSML show 0 rows, or anomaly `write failures` climbing**
Wrong Fuseki URL from a host-side agent → set `localhost:3030` (see §10). Or you restarted an
in-memory Fuseki (data wiped) — use a persistent dataset. Confirm with the
`SELECT ?g (COUNT(*)…)` graph-list query.

**SPARQL returns nothing but data exists**
(a) Wrong predicate: use `sgy:action_htg`/`sgy:action_clg` in the *zones* graph (§9).
(b) Missing/incorrect `GRAPH <…>` clause or PREFIXes. (c) Quoted integer for `sgy:step`.
(d) HSML cached an old/empty schema — run an injected episode first, then
`@sinergym-hsml {"action":"refresh"}`.

**HSML can't answer about anomalies**
Refresh the schema after the anomalies graph has data: `@sinergym-hsml {"action":"refresh"}`.

**Agent doesn't get observations / env_info**
`env_info` and the first obs are published at episode start and are **not retained**. Spawn
and launch all agents **before** starting the bridge (see §6). For the labeler/schema, if
they started mid-episode they'll catch up at the next episode start.

**Numbers differ from the script**
Within ~1% is expected (priming + one-step lag). Larger gaps usually mean injection was left
on (`--inject-anomalies`), a different `--weather`, or the wrong env/zone order.

---

## 14. Quick-start checklist

- [ ] MQTT, Fuseki (`sinergym` dataset), Sinergym container running
- [ ] EnergyPlus `24.1.0-9d7789a3ac` and Sinergym `3.11.0` verified in the container
- [ ] epJSON, yaml, `register_env.py`, bridge, `maddpg_v3/v2`, `ensemble_controller` in container
- [ ] `model.pt`, `normalizer.npz`, `maddpg_infer.py`, detector `.py` + `.pkl`, injector in `state/maddpg_office/`
- [ ] Recipes in `catalogue_agents/`, REGISTER blocks pasted into `catalog_agent.py`
- [ ] wactorz restarted; agents spawned; host-side agents configured to `localhost:3030`
- [ ] Fleet launched (15 zones, correct order)
- [ ] Bridge started with matching `--env` and `--zones`
- [ ] Metrics within tolerance; alerts on `…/anomaly`; HSML answering
