# `models/` — trained policies & detectors for the Sinergym OfficeMedium demo

These are the **inference-only** artifacts the wactorz catalog agents load to
control (and monitor) the 15-zone `officeMedium-multiagent` EnergyPlus building.
There are two interchangeable control backends — pick **one**:

| Backend | Folder | What it is | Fleet agent |
|---|---|---|---|
| **MADDPG** | [`maddpg/`](maddpg/) | Multi-Agent DDPG deep-RL policy (trained actor checkpoint) | `maddpg-fleet` |
| **AIF** | [`aif/`](aif/) | Factored **Active Inference** policy (`pymdp`-style generative model) | `aif-fleet` |

Both run the *same* deployment shape: **one decentralized agent per zone** (15
children), each subscribing to the global observation topic, slicing out its own
zone, inferring an action, and publishing it to its zone's action topic. The
anomaly detector runs alongside either backend.

> **These paths are canonical.** The catalog agents default to `models/maddpg/…`
> and `models/aif/…` (relative to the repo root, which is the wactorz working
> dir). You do **not** need to copy them anywhere first — launch straight from
> here. (An older layout staged copies under `sinergym/maddpg_office/` and
> `state/maddpg_office/`; that is gone — `models/` is the single source of truth.)

---

## `maddpg/` — MADDPG deep-RL backend

| File | Used by | Purpose |
|---|---|---|
| `model.pt` | `maddpg-fleet` | trained actor checkpoint (all 15 zones) |
| `normalizer.npz` | `maddpg-fleet` | frozen observation normalizer (must match training) |
| `maddpg_infer.py` | `maddpg-fleet` | training-free inference module (model + feature builder + action squashing, extracted verbatim from training) |
| `metadata.json` | — | training provenance (episodes, steps, per-episode metrics) |
| `forecast_anomaly_detector.py` | `sinergym-anomaly` | detector class |
| `detector_forecast_v9_mixed.pkl` | `sinergym-anomaly` | trained forecast detector |

## `aif/` — Active Inference backend

| File | Used by | Purpose |
|---|---|---|
| `aif_model.pkl` | `aif-fleet` | trained AIF checkpoint (N=15 generative model, sliced per zone) |
| `aif_infer.py` | `aif-fleet` | training-free per-zone inference (AIF analog of `maddpg_infer.py`) |
| `pymdp_office_v14_torch.py` | `aif-fleet` | the generative model `aif_infer.py` imports (**must sit next to it**) |
| `forecast_anomaly_detector.py` | `aif-anomaly` | detector class (same class as MADDPG's) |
| `detector_aif_v6.pkl` | `aif-anomaly` | trained AIF detector |

> **`aif_infer.py` imports `pymdp_office_v14_torch`** — both files must live in the
> same folder (they do, here). If you move `aif_model.pkl` elsewhere, keep
> `aif_infer.py` + `pymdp_office_v14_torch.py` beside it, or pass `infer_dir`.

---

## How the agents load these

The full run flow (services → spawn agents → launch fleet → start bridge) is in
[`../sinergym/RUN.md`](../sinergym/RUN.md) (quick) and
[`../sinergym/SETUP.md`](../sinergym/SETUP.md) (from-scratch). The two fleet
launch commands — the part that spawns the 15 zone agents — are below.

### MADDPG fleet (15 zone agents)

```
@maddpg-fleet {"action":"launch","env_id":"officeMedium-multiagent","model_path":"models/maddpg/model.pt","normalizer_path":"models/maddpg/normalizer.npz","zones":["Core_bottom","Core_mid","Core_top","Perimeter_bot_ZN_1","Perimeter_bot_ZN_2","Perimeter_bot_ZN_3","Perimeter_bot_ZN_4","Perimeter_mid_ZN_1","Perimeter_mid_ZN_2","Perimeter_mid_ZN_3","Perimeter_mid_ZN_4","Perimeter_top_ZN_1","Perimeter_top_ZN_2","Perimeter_top_ZN_3","Perimeter_top_ZN_4"]}
```

### AIF fleet (15 zone agents)

`model_path` is the `.pkl` (no `normalizer_path`); the extra fields are the
comfort/energy bands and EFE weights — **they must match training**.

```
@aif-fleet {"action":"launch","env_id":"officeMedium-multiagent","model_path":"models/aif/aif_model.pkl","heat_low":15.0,"heat_high":22.5,"cool_low":22.5,"cool_high":30.0,"policy_len":8,"energy_weight":0.2,"comfort_weight":1.0,"epistemic_weight":0.2,"unocc_gate":0.1,"deadband_weight":8.0,"override":"safety","freeze_B":true,"lr_pB":1.0,"zones":["Core_bottom","Core_mid","Core_top","Perimeter_bot_ZN_1","Perimeter_bot_ZN_2","Perimeter_bot_ZN_3","Perimeter_bot_ZN_4","Perimeter_mid_ZN_1","Perimeter_mid_ZN_2","Perimeter_mid_ZN_3","Perimeter_mid_ZN_4","Perimeter_top_ZN_1","Perimeter_top_ZN_2","Perimeter_top_ZN_3","Perimeter_top_ZN_4"]}
```

**Notes on both:**
- One line, no spaces inside keys or zone names.
- `env_id` **must equal** the bridge's `--env` (`officeMedium-multiagent`).
- The 15 zones must be in **this exact order** — it is the actor index order the
  policies expect.
- Spawn the agents and launch the fleet **before** starting the bridge:
  `env_info` and the first observations are published at episode start and are
  **not retained**.

The matching anomaly detector is spawned separately — `@catalog spawn
sinergym-anomaly` (MADDPG) or `@catalog spawn aif-anomaly` (AIF) — and picks up
its detector from this folder automatically.
