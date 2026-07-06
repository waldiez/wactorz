# `sinergym/` — the EnergyPlus building simulator & bridge

This folder is the **simulation half** of the Sinergym demo: the EnergyPlus
building model, the env definition, and the **bridge** that runs the simulation
and streams it over MQTT into Fuseki. The **agents** that control and monitor the
building live in [`../wactorz/catalogue_agents/`](../wactorz/catalogue_agents/);
the **trained policies/detectors** they load live in [`../models/`](../models/).

## The big picture

```
  ../models/ (trained policy + detector)         ../wactorz/ (the agents)
         │  loaded by                                   │
         ▼                                              ▼
   ┌──────────────────────┐   MQTT (obs / actions)   ┌────────────────────────┐
   │  bridge  (this dir)  │◄────────────────────────►│  maddpg-fleet / aif-    │
   │  runs EnergyPlus,    │                          │  fleet  (15 zone agents)│
   │  publishes obs,      │─── writes RDF triples ──►│  sinergym-anomaly, hsml,│
   │  applies actions     │        to Fuseki         │  labeler, …             │
   └──────────────────────┘                          └────────────────────────┘
```

Two backends control the same building — **MADDPG** (deep-RL) or **AIF** (active
inference). They are drop-in alternatives; pick one. See
[`../models/README.md`](../models/README.md) for what each backend is and the
exact fleet-launch commands.

## What's in here

| File | What it is |
|---|---|
| `sinergym_bridge_anomalies.py` | **the bridge** — runs EnergyPlus, publishes obs, applies the fleet's actions, writes triples to Fuseki, optionally injects anomalies |
| `register_env.py` | builds the training-parity env (`make_custom_env`) |
| `officeMedium_multiagent.yaml` | Sinergym env definition for the 15-zone office |
| `ASHRAE901_OfficeMedium_STD2019_Denver_MultiAgent.epJSON` | the 15-zone building model |
| `5ZoneAutoDXVAV_MultiAgent.epJSON` | smaller 5-zone building (not used by the OfficeMedium demo) |
| `anomaly_injector.py` | injects reproducible HVAC faults (used by the bridge, container-side) |
| `metrics_utils.py` | evaluation metric helpers |
| `sinergym_bridge_latency.py` | latency-benchmark variant of the bridge |
| `docker/` | container image + `run-bridge.sh` launcher for the Linux/Docker path |
| `viz/` | the digital-twin web visualization (`serve.sh` → http://localhost:8200) |

## How to run it

Read these in order of how much detail you want:

1. **[`RUN.md`](RUN.md)** — the happy-path cheat sheet (spawn agents → launch
   fleet → start bridge → open the viz). Start here.
2. **[`SETUP.md`](SETUP.md)** — the full from-scratch runbook (architecture, MQTT
   topics, SPARQL queries, troubleshooting). The Windows/devcontainer path.
3. **[`docker/LOCAL_SETUP.md`](docker/LOCAL_SETUP.md)** — the Linux/Docker path
   (plain container, no devcontainer).

The one rule that trips everyone up: **spawn the agents and launch the fleet
before starting the bridge** — `env_info` and the first observations are
published at episode start and are **not retained**.
