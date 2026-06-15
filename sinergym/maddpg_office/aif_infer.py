"""
aif_infer.py — training-free, per-zone inference for the Factored Active
Inference OfficeMedium policy (pymdp_office_v7_torch).

This is the AIF analog of maddpg_infer.py. Deploy it on the wactorz process's
import path ALONGSIDE pymdp_office_v7_torch.py and the trained checkpoint
(aif_model.pkl).

Why per-zone slicing is exact
-----------------------------
pymdp_office_v7_torch runs all 15 agents as ONE tensor batch, but only for
vectorization: there is NO cross-agent coupling in _infer / _plan / _learn —
every einsum keeps N as a pure batch dimension. Each row n depends solely on
its own beliefs (qT, qO, qW, qH) and its own B_T[n] / struct_ctx[n]. So one
zone's generative model can be sliced out of the N=15 checkpoint and run as an
N=1 batch with byte-identical numerics. (Verified: action from the N=1 slice
equals the corresponding row of the full batch to 0.0 .)

That is what lets the AIF policy be deployed exactly like MADDPG: one
decentralized agent per zone, each subscribing to the global observation topic
and publishing its own zone action.

The trained checkpoint is the PyMDPOffice.save() pickle:

    {'mode': 'zone', 'structural': True, 'version': 4, 'backend': 'torch',
     'state': {'B_T': (15,O,W,H,X,T,A), 'pB_T': (15,...),
               'transition_counts': (15,T,A), 'step_count': int,
               'b_updates': int}}

Per-zone usage (one AIFZonePolicy per zone, decentralized execution):

    from aif_infer import AIFZonePolicy, load_aif_state

    state = load_aif_state("aif_model.pkl")          # load once, share across zones
    pol = AIFZonePolicy(state, zone_index=k,
                        heat_low=15.0, heat_high=22.5,
                        cool_low=22.5, cool_high=30.0)

    # on each GLOBAL observation message (full Sinergym obs vector):
    norm_action, setpoints = pol.step(obs_raw)
    #   norm_action : np.array([htg, clg]) in [-1, 1]   (MAS-bridge contract)
    #   setpoints   : np.array([htg, clg]) in degrees C  (for provenance/logging)

    # on episode boundary (done / episode change):
    pol.reset()

IMPORTANT — action bounds
-------------------------
heat_low/heat_high/cool_low/cool_high MUST equal the env.action_space bounds the
model was TRAINED on (the same bounds the MAS bridge uses to denormalize). They
drive the action->setpoint decode (heat_sp/cool_sp) and the energy/deadband
terms of the EFE, so a wrong value silently shifts action selection away from
training. Pass them explicitly; the defaults below are the standard Sinergym
OfficeMedium setpoint-control ranges and are only a fallback.
"""

import os as _os
import pickle
import sys as _sys

import numpy as np
import torch


# ---------------------------------------------------------------------------
# Locate pymdp_office_v7_torch.py before importing it.
# Unlike maddpg_infer.py (self-contained), this module imports the AIF model
# from pymdp_office_v7_torch. When run inside a spawned child agent, only
# `infer_dir` is guaranteed on sys.path — so we add the likely locations of
# pymdp_office_v7_torch.py here, making "drop both files in the same folder"
# (or "run wactorz from the repo root") work without further wiring.
#   Override search explicitly with $WACTORZ_AIF_SRC (a dir or the file itself).
# ---------------------------------------------------------------------------
def _ensure_pymdp_on_path():
    import importlib.util as _ilu
    if _ilu.find_spec("pymdp_office_v7_torch") is not None:
        return
    here = _os.path.dirname(_os.path.abspath(__file__))
    env  = _os.environ.get("WACTORZ_AIF_SRC", "")
    if env and env.endswith(".py"):
        env = _os.path.dirname(env)
    candidates = [here, _os.getcwd(), env, _os.path.dirname(here)]
    for d in candidates:
        if d and _os.path.exists(_os.path.join(d, "pymdp_office_v7_torch.py")):
            if d not in _sys.path:
                _sys.path.insert(0, d)
            return


_ensure_pymdp_on_path()

# pymdp_office_v7_torch only does light top-level imports (numpy/torch/stdlib);
# the Sinergym/MADDPG imports live inside run_simulation(), so importing the
# model here pulls in NO simulation dependencies.
try:
    from pymdp_office_v7_torch import (
        BatchedFactoredAgents,
        ZONE_META,
        ObsIndex,
        get_agent_occupancy_state,
        N_ZONES,
    )
except ModuleNotFoundError as _e:  # pragma: no cover - clearer deploy error
    raise ModuleNotFoundError(
        "aif_infer.py could not import 'pymdp_office_v7_torch'. Put "
        "pymdp_office_v7_torch.py next to aif_infer.py (e.g. in your infer_dir / "
        "state/maddpg_office), or set $WACTORZ_AIF_SRC to the directory holding "
        "it, or pass aif_src_dir in the aif-fleet launch params."
    ) from _e

# Sinergym OfficeMedium setpoint action ranges. Override at construction (or via
# the fleet's launch params / env_info) to match YOUR trained env exactly.
DEFAULT_HEAT_LOW, DEFAULT_HEAT_HIGH = 15.0, 22.0
DEFAULT_COOL_LOW, DEFAULT_COOL_HIGH = 24.0, 30.0


def load_aif_state(path):
    """Load + validate the PyMDPOffice.save() pickle. Returns the full dict."""
    with open(path, "rb") as f:
        data = pickle.load(f)
    if data.get("version") != 4 or data.get("backend") != "torch":
        raise ValueError(
            f"unexpected AIF checkpoint at {path}: "
            f"version={data.get('version')!r} backend={data.get('backend')!r} "
            f"(expected version=4, backend='torch')"
        )
    if data.get("mode") != "zone":
        raise ValueError(
            f"AIF per-zone deployment needs a 'zone'-mode checkpoint; "
            f"got mode={data.get('mode')!r}. Re-train/save with mode='zone'."
        )
    if "state" not in data or "B_T" not in data["state"]:
        raise ValueError(f"checkpoint at {path} has no state['B_T']")
    return data


class AIFZonePolicy:
    """
    Inference wrapper for a single zone's Active-Inference agent, sliced from the
    shared N=15 checkpoint and run as an independent N=1 batch. Returns the
    normalized [-1, 1] action the MAS bridge expects (so AIF agents are wire-
    compatible with MADDPG agents) plus the real-degree setpoints for logging.

    `model` may be a path (loaded here) or a dict already returned by
    load_aif_state() (preferred when many zones share one file).
    """

    def __init__(self, model, zone_index,
                 heat_low=DEFAULT_HEAT_LOW, heat_high=DEFAULT_HEAT_HIGH,
                 cool_low=DEFAULT_COOL_LOW, cool_high=DEFAULT_COOL_HIGH,
                 comfort_weight=1.0, energy_weight=0.5, policy_len=4,
                 lr_pB=1.0, pB_prior_scale=2.0, epistemic_weight=0.2,
                 unocc_gate=0.1, deadband_weight=4.0, freeze_B=False,
                 override="safety", device="cpu", dtype="float"):
        self.zone_index = int(zone_index)
        self.heat_low,  self.heat_high = float(heat_low),  float(heat_high)
        self.cool_low,  self.cool_high = float(cool_low),  float(cool_high)

        data = load_aif_state(model) if isinstance(model, str) else model
        structural = bool(data.get("structural", True))
        state = data["state"]

        n_ck = int(np.asarray(state["B_T"]).shape[0])
        if not (0 <= self.zone_index < n_ck):
            raise IndexError(
                f"zone_index {self.zone_index} out of range for a checkpoint "
                f"with {n_ck} agents"
            )

        ctx = dict(ZONE_META[self.zone_index]) if structural else {}

        # One independent agent = one batch row.
        #   freeze_B=True  -> frozen inference (B_T fixed exactly as trained).
        #   freeze_B=False -> keep adapting B_T online (per-zone Dirichlet
        #                     learning), matching v7's run_simulation default,
        #                     which is how the deploy/eval baseline is produced.
        # Per-zone online learning is mathematically independent across zones, so
        # each child learning its own slice reproduces the full batch's per-agent
        # learning (modulo floating-point argmax ties — see module note).
        # NOTE: these hyperparameters MUST match the v7 run that produced the
        # checkpoint, because they shape the Expected-Free-Energy planning at
        # inference time (energy/comfort/epistemic/deadband terms + horizon) and
        # therefore the action chosen each step — independently of the loaded
        # B_T. pB_prior_scale only sets the initial pseudo-counts, which set_state
        # then overwrites with the checkpoint's pB_T, so it is a no-op for deploy;
        # it is forwarded only for completeness.
        self.batch = BatchedFactoredAgents(
            n_agents=1,
            heat_low=self.heat_low, heat_high=self.heat_high,
            cool_low=self.cool_low, cool_high=self.cool_high,
            struct_ctxs=[ctx],
            comfort_weight=comfort_weight,
            energy_weight=energy_weight,
            policy_len=policy_len,
            lr_pB=lr_pB,
            pB_prior_scale=pB_prior_scale,
            epistemic_weight=epistemic_weight,
            unocc_gate=unocc_gate,
            deadband_weight=deadband_weight,
            freeze_B=freeze_B,
            override=override,
            device=device,
            dtype=dtype,
        )

        # Slice this zone out of the batched checkpoint into the N=1 model.
        # __init__ built a prior B_T; set_state overwrites it with the trained one.
        self.batch.set_state({
            "B_T":  np.asarray(state["B_T"])[self.zone_index:self.zone_index + 1],
            "pB_T": np.asarray(state["pB_T"])[self.zone_index:self.zone_index + 1],
            "transition_counts":
                np.asarray(state["transition_counts"])[self.zone_index:self.zone_index + 1],
            "step_count": int(state.get("step_count", 0)),
            "b_updates":  int(state.get("b_updates", 0)),
        })

    # ------------------------------------------------------------------ #
    def reset(self):
        """Clear beliefs + prev_action + H-trace — call on episode boundaries."""
        self.batch.reset_episode()

    # ------------------------------------------------------------------ #
    def _normalize(self, htg, clg):
        """Real degrees C -> [-1, 1] using the SAME bounds the bridge denormalizes
        with, so the round-trip is exact."""
        nh = 2.0 * (htg - self.heat_low) / (self.heat_high - self.heat_low) - 1.0
        nc = 2.0 * (clg - self.cool_low) / (self.cool_high - self.cool_low) - 1.0
        return (float(np.clip(nh, -1.0, 1.0)), float(np.clip(nc, -1.0, 1.0)))

    # ------------------------------------------------------------------ #
    def step(self, obs_raw):
        """
        Consume the FULL Sinergym observation vector, run one AIF step for this
        zone, and return (norm_action, setpoints):
            norm_action : np.float32[2]  htg, clg in [-1, 1]
            setpoints   : np.float32[2]  htg, clg in degrees C

        Context (month/day/hour/outdoor_temp/direct_solar) is read from the global
        obs exactly as PyMDPOffice.get_action does; the zone temperature is read
        with the model's own ObsIndex (obs[9 + zone_index]) so the per-zone input
        is byte-identical to the corresponding row of the full batch.
        """
        obs = np.asarray(obs_raw, dtype=np.float64)
        month = int(obs[ObsIndex.MONTH])
        day   = int(obs[ObsIndex.DAY])
        hour  = int(obs[ObsIndex.HOUR])
        outdoor_temp = float(obs[ObsIndex.OUTDOOR_TEMP])
        direct_solar = float(obs[ObsIndex.DIRECT_SOLAR])
        is_occ = get_agent_occupancy_state(month, day, hour) == "occupied"

        zone_temp = ObsIndex.get_zone_temp(obs, self.zone_index)

        self.batch.update_context(month, day, hour, is_occ,
                                  outdoor_temp, direct_solar)
        htg, clg = self.batch.step(np.array([zone_temp], dtype=np.float64), is_occ)
        htg, clg = float(htg[0]), float(clg[0])

        nh, nc = self._normalize(htg, clg)
        return (np.array([nh, nc], dtype=np.float32),
                np.array([htg, clg], dtype=np.float32))


# ===========================================================================
# AIFBatchController — all zones in ONE batch (exact v7 parity)
# ===========================================================================
class AIFBatchController:
    """
    Runs ALL zones as a single N-agent batch — the very same BatchedFactoredAgents
    object v7's PyMDPOffice drives — and emits one normalized action per zone.

    Use this (deployed as a SINGLE agent) when you need bit-exact parity with a
    full-batch v7 run. The decentralized per-zone AIFZonePolicy fleet is
    equivalent EXCEPT for discrete-argmax tie-breaking: an N=15 batched matmul and
    an N=1 matmul round flat-EFE ties differently, which a single batch avoids.

    step(obs_raw) -> list of (zone_name, norm_action[2], setpoints[2]) for all
    zones, in checkpoint/ZONE_META order. Publish each to its zone action topic.
    """

    def __init__(self, model,
                 heat_low=DEFAULT_HEAT_LOW, heat_high=DEFAULT_HEAT_HIGH,
                 cool_low=DEFAULT_COOL_LOW, cool_high=DEFAULT_COOL_HIGH,
                 zones=None,
                 comfort_weight=1.0, energy_weight=0.5, policy_len=4,
                 lr_pB=1.0, pB_prior_scale=2.0, epistemic_weight=0.2,
                 unocc_gate=0.1, deadband_weight=4.0, freeze_B=False,
                 override="safety", device="cpu", dtype="float"):
        self.heat_low,  self.heat_high = float(heat_low),  float(heat_high)
        self.cool_low,  self.cool_high = float(cool_low),  float(cool_high)

        data = load_aif_state(model) if isinstance(model, str) else model
        structural = bool(data.get("structural", True))
        state = data["state"]
        self.n = int(np.asarray(state["B_T"]).shape[0])

        ctxs = [dict(ZONE_META[i]) if structural else {} for i in range(self.n)]
        self.zone_names = list(zones) if zones else [ZONE_META[i]["name"]
                                                     for i in range(self.n)]
        if len(self.zone_names) != self.n:
            raise ValueError(
                f"zones list length {len(self.zone_names)} != checkpoint agents {self.n}"
            )

        self.batch = BatchedFactoredAgents(
            n_agents=self.n,
            heat_low=self.heat_low, heat_high=self.heat_high,
            cool_low=self.cool_low, cool_high=self.cool_high,
            struct_ctxs=ctxs,
            comfort_weight=comfort_weight, energy_weight=energy_weight,
            policy_len=policy_len, lr_pB=lr_pB, pB_prior_scale=pB_prior_scale,
            epistemic_weight=epistemic_weight, unocc_gate=unocc_gate,
            deadband_weight=deadband_weight, freeze_B=freeze_B,
            override=override, device=device, dtype=dtype,
        )
        # Load the full checkpoint, no slicing — identical to PyMDPOffice.load.
        self.batch.set_state(state)

    def reset(self):
        self.batch.reset_episode()

    def _normalize(self, htg, clg):
        nh = 2.0 * (htg - self.heat_low) / (self.heat_high - self.heat_low) - 1.0
        nc = 2.0 * (clg - self.cool_low) / (self.cool_high - self.cool_low) - 1.0
        return (float(np.clip(nh, -1.0, 1.0)), float(np.clip(nc, -1.0, 1.0)))

    def step(self, obs_raw):
        obs = np.asarray(obs_raw, dtype=np.float64)
        month = int(obs[ObsIndex.MONTH]); day = int(obs[ObsIndex.DAY])
        hour  = int(obs[ObsIndex.HOUR])
        outdoor_temp = float(obs[ObsIndex.OUTDOOR_TEMP])
        direct_solar = float(obs[ObsIndex.DIRECT_SOLAR])
        is_occ = get_agent_occupancy_state(month, day, hour) == "occupied"

        T_arr = np.array([ObsIndex.get_zone_temp(obs, i) for i in range(self.n)],
                         dtype=np.float64)
        self.batch.update_context(month, day, hour, is_occ,
                                  outdoor_temp, direct_solar)
        htg, clg = self.batch.step(T_arr, is_occ)

        out = []
        for i in range(self.n):
            nh, nc = self._normalize(float(htg[i]), float(clg[i]))
            out.append((self.zone_names[i],
                        np.array([nh, nc], dtype=np.float32),
                        np.array([float(htg[i]), float(clg[i])], dtype=np.float32)))
        return out