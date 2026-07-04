"""
Factored Active Inference Agent — PyTorch batched version (v10)
  v9  adds TOU-aware energy preference (time-of-use price in the C/DRIVE term)
  v10 adds congestion inference: each agent infers a latent 'others are
      peaking' signal from the shared per-floor power, penalises its own
      HVAC drive when congestion is high, and selects actions stochastically
      so identical floors can desynchronise (lower coincidence factor).
===============================================================

Same generative model and behaviour as pymdp_office_v6 (numpy), rewritten so
ALL agents run as one batch of tensors on CPU or GPU:

  B_T   (N, T', T, O, W, H, A)   per-agent factored thermal transitions
  qT    (N, T)                   per-agent beliefs, etc.

One env step = a handful of batched einsums for inference + planning across
every agent and every action simultaneously — no per-agent Python loop in the
hot path (the reactive safety-net check is the only small loop left).

Numerics: float32 on GPU by default (float64 via --dtype double). Plans match
the numpy v6 implementation to ~1e-4; action choices are identical except for
exact EFE ties.

Usage:
  python pymdp_office_v10_torch.py --mode zone --congestion_weight 0.5 --action_temp 0.3
  python pymdp_office_v9_torch.py --mode zone --device cpu     # fallback
"""

import argparse
import os
import pickle
import time
import warnings
from datetime import date

# --- Determinism pins (MUST be set before numpy/torch import to take effect) ---
# Different thread counts make BLAS reduce floats in a different order across
# machines, which can flip a near-tied EFE argmax and desync the trajectory.
# Single-threaded == identical reduction order everywhere (small speed cost).
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

import numpy as np
import torch
torch.set_num_threads(1)
warnings.filterwarnings('ignore')


# =============================================================================
# ZONE CONFIGURATION (identical to v6)
# =============================================================================
OCCUPIED_ZONES = [
    "Core_bottom", "Core_mid", "Core_top",
    "Perimeter_bot_ZN_1", "Perimeter_bot_ZN_2",
    "Perimeter_bot_ZN_3", "Perimeter_bot_ZN_4",
    "Perimeter_mid_ZN_1", "Perimeter_mid_ZN_2",
    "Perimeter_mid_ZN_3", "Perimeter_mid_ZN_4",
    "Perimeter_top_ZN_1", "Perimeter_top_ZN_2",
    "Perimeter_top_ZN_3", "Perimeter_top_ZN_4",
]
N_ZONES = len(OCCUPIED_ZONES)

ZONE_META = {
    0:  {"name": "Core_bottom",        "floor": "bottom", "type": "core",      "orient": "none"},
    1:  {"name": "Core_mid",           "floor": "mid",    "type": "core",      "orient": "none"},
    2:  {"name": "Core_top",           "floor": "top",    "type": "core",      "orient": "none"},
    3:  {"name": "Perimeter_bot_ZN_1", "floor": "bottom", "type": "perimeter", "orient": "south"},
    4:  {"name": "Perimeter_bot_ZN_2", "floor": "bottom", "type": "perimeter", "orient": "east"},
    5:  {"name": "Perimeter_bot_ZN_3", "floor": "bottom", "type": "perimeter", "orient": "north"},
    6:  {"name": "Perimeter_bot_ZN_4", "floor": "bottom", "type": "perimeter", "orient": "west"},
    7:  {"name": "Perimeter_mid_ZN_1", "floor": "mid",    "type": "perimeter", "orient": "south"},
    8:  {"name": "Perimeter_mid_ZN_2", "floor": "mid",    "type": "perimeter", "orient": "east"},
    9:  {"name": "Perimeter_mid_ZN_3", "floor": "mid",    "type": "perimeter", "orient": "north"},
    10: {"name": "Perimeter_mid_ZN_4", "floor": "mid",    "type": "perimeter", "orient": "west"},
    11: {"name": "Perimeter_top_ZN_1", "floor": "top",    "type": "perimeter", "orient": "south"},
    12: {"name": "Perimeter_top_ZN_2", "floor": "top",    "type": "perimeter", "orient": "east"},
    13: {"name": "Perimeter_top_ZN_3", "floor": "top",    "type": "perimeter", "orient": "north"},
    14: {"name": "Perimeter_top_ZN_4", "floor": "top",    "type": "perimeter", "orient": "west"},
}

FLOOR_ZONES = {
    "bottom": [i for i, m in ZONE_META.items() if m["floor"] == "bottom"],
    "mid":    [i for i, m in ZONE_META.items() if m["floor"] == "mid"],
    "top":    [i for i, m in ZONE_META.items() if m["floor"] == "top"],
}
FLOORS = ["bottom", "mid", "top"]

# Ground-truth intra-floor adjacency (from the epJSON surfaces): each core
# couples to its 4 perimeter zones; each perimeter to the core + 2 ring
# neighbours. Floors are coupled only via return-air plenums (not controlled),
# so the occupied zones form three independent per-floor cliques.
ZONE_NEIGHBORS = {
    0:[3,4,5,6], 1:[7,8,9,10], 2:[11,12,13,14],
    3:[0,4,6], 4:[0,3,5], 5:[0,4,6], 6:[0,3,5],
    7:[1,8,10], 8:[1,7,9], 9:[1,8,10], 10:[1,7,9],
    11:[2,12,14], 12:[2,11,13], 13:[2,12,14], 14:[2,11,13],
}

COMFORT_WINTER = (20.0, 23.5)
COMFORT_SUMMER = (23.0, 26.0)


def get_seasonal_comfort(month: int, day: int) -> tuple:
    is_summer = ((month > 6) or (month == 6 and day >= 1)) and \
                ((month < 10) or (month == 9 and day <= 30))
    return COMFORT_SUMMER if is_summer else COMFORT_WINTER


def tou_multiplier(month: int, day: int, hour: int) -> float:
    """Time-of-use price multiplier (PG&E B-19 style), matches metrics_utils.
    on-peak 16-21 weekday = 3.0, mid-peak 9-16 & 21-23 weekday = 1.5, else 1.0."""
    try:
        is_weekend = date(2024, int(month), int(day)).weekday() >= 5
    except Exception:
        is_weekend = False
    if is_weekend:
        return 1.0
    h = int(hour)
    if 16 <= h < 21:
        return 3.0
    if (9 <= h < 16) or (21 <= h < 23):
        return 1.5
    return 1.0


class ObsIndex:
    MONTH = 0
    DAY = 1
    HOUR = 2
    OUTDOOR_TEMP = 3
    DIRECT_SOLAR = 8
    ZONE_TEMPS_START = 9
    HVAC_DEMAND = 90

    @staticmethod
    def get_zone_temp(obs, i): return float(obs[ObsIndex.ZONE_TEMPS_START + i])

    @staticmethod
    def get_all_zone_temps(obs):
        return np.array([ObsIndex.get_zone_temp(obs, i) for i in range(N_ZONES)])


def get_agent_occupancy_state(month: int, day: int, hour: int) -> str:
    try:
        is_weekend = date(2024, month, day).weekday() >= 5
    except Exception:
        is_weekend = False
    if is_weekend:
        return 'unoccupied'
    if 6 <= hour <= 20:
        return 'occupied'
    if 4 <= hour < 6:
        return 'pre_occupied'
    return 'unoccupied'


def occ_rates(hour: float, is_weekend: bool) -> tuple:
    if is_weekend:
        return (0.005, 0.30)
    h = hour % 24.0
    if h < 4.0:    return (0.005, 0.30)
    if h < 5.0:    return (0.05,  0.10)
    if h < 6.0:    return (0.25,  0.02)
    if h < 9.0:    return (0.45,  0.01)
    if h < 17.0:   return (0.08,  0.02)
    if h < 19.0:   return (0.02,  0.20)
    if h < 21.0:   return (0.01,  0.35)
    return (0.005, 0.40)


# =============================================================================
# BATCHED FACTORED AIF AGENTS  (one tensor batch = all agents)
# =============================================================================
class BatchedFactoredAgents:
    TEMP_CENTERS = np.array(
        [15.0, 17.0, 18.5, 19.5, 20.0, 20.5, 21.0, 21.5, 22.0, 22.5,
         23.0, 23.5, 24.0, 24.5, 25.0, 25.5, 26.0, 27.0, 29.0], dtype=np.float64)
    N_TEMP = len(TEMP_CENTERS)
    N_OCC = 2
    N_W = 5
    W_CENTERS = np.array([-5.0, 5.0, 15.0, 25.0, 35.0])
    N_H = 5
    H_CENTERS = np.array([-0.8, -0.3, 0.0, 0.3, 0.8])
    N_HEAT = 5
    N_COOL = 5
    N_ACTIONS = N_HEAT * N_COOL
    STEP_HOURS = 0.25
    SETBACK_MARGIN = 4.0

    def __init__(self, n_agents: int, heat_low: float, heat_high: float,
                 cool_low: float, cool_high: float,
                 struct_ctxs: list,
                 comfort_weight: float = 1.0,
                 energy_weight:  float = 0.5,
                 tou_weight:     float = 1.0,
                 congestion_weight: float = 0.0,
                 action_temp:    float = 0.0,
                 cong_alpha:     float = 0.3,
                 learn_C:        bool  = False,
                 c_lr:           float = 0.02,
                 energy_w_min:   float = 0.0,
                 energy_w_max:   float = 1.5,
                 couple:         bool  = False,
                 couple_k:       float = 0.12,
                 couple_learn:   bool  = True,
                 couple_lr:      float = 0.01,
                 couple_k_max:   float = 0.6,
                 couple_neighbors=None,
                 policy_len:     int   = 4,
                 lr_pB:          float = 1.0,
                 pB_prior_scale: float = 2.0,
                 epistemic_weight: float = 0.2,
                 unocc_gate:     float = 0.1,
                 deadband_weight: float = 4.0,
                 freeze_B:       bool  = False,
                 override:       str   = "safety",
                 device:         str   = "auto",
                 dtype:          str   = "float"):

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(device)
        self.dt = torch.float64 if dtype == "double" else torch.float32

        self.N = n_agents
        self.struct_ctxs = [c or {} for c in struct_ctxs]
        self.comfort_weight = comfort_weight
        self.energy_weight  = energy_weight
        self.tou_weight     = float(tou_weight)   # 0 => TOU off, 1 => full TOU
        self.tou_mult       = 1.0                 # updated each step in update_context
        # v10 congestion inference: latent "others are peaking" belief in [0,1]
        # per agent, filtered (EMA = 1-D Bayesian posterior mean) from the shared
        # per-floor power. congestion_weight gates an extra drive penalty so the
        # agent defers its own load when it infers the others are ramping.
        self.congestion_weight = float(congestion_weight)
        self.action_temp       = float(action_temp)   # 0 => argmax, >0 => sample
        self.cong_alpha        = float(cong_alpha)     # belief filter rate
        self.cong              = torch.zeros(self.N, dtype=self.dt, device=self.device)
        # v12: learnable per-agent energy/comfort tradeoff. energy_w starts equal
        # for all agents and is adapted online toward each agent's own comfort
        # outcome (slow integral control). Heterogeneous weights break the shared-C
        # symmetry, so drive desynchronises without a hand-set congestion term.
        self.learn_C        = bool(learn_C)
        self.c_lr           = float(c_lr)
        self.c_beta         = 0.05          # comfort-error EMA rate
        self.c_tol          = 0.20          # degC: 'comfortable' threshold
        self.energy_w_min   = float(energy_w_min)
        self.energy_w_max   = float(energy_w_max)
        self.energy_w       = torch.full((self.N,), float(energy_weight),
                                         dtype=self.dt, device=self.device)
        self.cerr_ema       = torch.zeros(self.N, dtype=self.dt, device=self.device)
        # v14: inter-zone thermal coupling. Each agent folds a learned
        # conductance k against the mean neighbour temperature into its temp
        # prediction. Neighbours are agent (=zone) indices; coupling is
        # prediction-only (no communication) and zone-mode only.
        self.couple = bool(couple) and (couple_neighbors is not None)
        self.couple_h = 0.5      # interp bandwidth (degC) for the temp-space shift
        self._cpl_delta = torch.zeros(self.N, dtype=self.dt, device=self.device)
        self._cpl_g = torch.zeros(self.N, dtype=self.dt, device=self.device)
        self._cpl_mu0 = None
        if self.couple:
            maxnb = max(len(x) for x in couple_neighbors)
            nb = np.zeros((self.N, maxnb), dtype=np.int64)
            mk = np.zeros((self.N, maxnb), dtype=np.float32)
            for i, lst in enumerate(couple_neighbors):
                for j, z in enumerate(lst):
                    nb[i, j] = z; mk[i, j] = 1.0
            self.nb_idx  = torch.as_tensor(nb, device=self.device)
            self.nb_mask = torch.as_tensor(mk, dtype=self.dt, device=self.device)
            self.kc = torch.full((self.N,), float(couple_k),
                                 dtype=self.dt, device=self.device)
            self.couple_learn = bool(couple_learn)
            self.couple_lr    = float(couple_lr)
            self.couple_k_max = float(couple_k_max)
            # v14: per-zone prediction-error accumulators (coupled vs no-coupling
            # prediction of the same step) to show the conductance earns its place.
            self._pred_err_couple   = torch.zeros(self.N, dtype=self.dt, device=self.device)
            self._pred_err_nocouple = torch.zeros(self.N, dtype=self.dt, device=self.device)
            self._pred_count = 0
        self.policy_len     = max(1, int(policy_len))
        self.lr_pB          = lr_pB
        self.epistemic_weight = epistemic_weight
        self.unocc_gate     = unocc_gate
        self.deadband_weight = deadband_weight
        self.freeze_B       = freeze_B
        self.override_mode  = override

        self.heat_sp = np.linspace(heat_low, heat_high, self.N_HEAT)
        self.cool_sp = np.linspace(cool_low, cool_high, self.N_COOL)

        # per-agent envelope coupling from structure
        ks = []
        for ctx in self.struct_ctxs:
            k = 0.015 if ctx.get("type", "perimeter") == "core" else 0.035
            if ctx.get("floor", "mid") == "top":
                k += 0.010
            ks.append(k)
        self.k_env = np.array(ks)

        # context
        self.comfort_low, self.comfort_high = 20.0, 23.5
        self.comfort_mid = 21.75
        self.hour = 12.0
        self.is_weekend = False
        self.outdoor_temp = 15.0
        self.direct_solar = 0.0
        self.is_occupied = False

        # ---- generative model (built in numpy float64, moved to device) ----
        t = lambda x: torch.as_tensor(x, dtype=self.dt, device=self.device)
        # context-major layout (N, O, W, H, X, T, A): planning folds the whole
        # (O,W,H) context with ONE batched matmul over a flat contiguous view.
        B_T_np = np.stack([self._build_B_T_prior_np(k) for k in self.k_env])
        B_T_np = np.ascontiguousarray(B_T_np.transpose(0, 3, 4, 5, 1, 2, 6))
        self.B_T  = t(B_T_np)                                   # (N,O,W,H,X,T,A)
        self.pB_T = self.B_T * (pB_prior_scale * self.N_TEMP)
        self.initial_B_T = self.B_T.clone()
        self._XTA = self.N_TEMP * self.N_TEMP * self.N_ACTIONS
        self._OWH = self.N_OCC * self.N_W * self.N_H
        self.B_H = t(self._build_sticky_np(self.N_H, 0.90, 0.05))
        self.B_W = t(self._build_sticky_np(self.N_W, 0.92, 0.04))
        self.A_T = t(self._build_A_T_np())                      # (X_obs, T)
        self.TC  = t(self.TEMP_CENTERS)
        self.DRIVE, self.DB_PEN = self._build_drive_np()
        self.DRIVE  = t(self.DRIVE)                             # (T, A)
        self.DB_PEN = t(self.DB_PEN)                            # (A,)

        # ---- beliefs (N, dim) ----
        self.qT = torch.full((self.N, self.N_TEMP), 1.0 / self.N_TEMP,
                             dtype=self.dt, device=self.device)
        self.qO = torch.tensor([[0.65, 0.35]], dtype=self.dt,
                               device=self.device).repeat(self.N, 1)
        self.qW = torch.full((self.N, self.N_W), 1.0 / self.N_W,
                             dtype=self.dt, device=self.device)
        self.qH = torch.full((self.N, self.N_H), 1.0 / self.N_H,
                             dtype=self.dt, device=self.device)
        self.prev_action = None  # (N,) long, or None at episode start

        # bookkeeping
        self.step_count = 0
        self.b_updates = 0
        self.transition_counts = np.zeros((self.N, self.N_TEMP, self.N_ACTIONS))
        self.occ_belief_sum = np.zeros(self.N)
        self.H_belief_sum = np.zeros(self.N)
        self.belief_count = 0
        self.H_trace = []  # list of (N,) arrays

    # ------------------------------------------------------------------ #
    # numpy builders (run once at init)
    # ------------------------------------------------------------------ #
    def _t2s(self, T): return int(np.argmin(np.abs(self.TEMP_CENTERS - T)))
    def _w2s(self, T): return int(np.argmin(np.abs(self.W_CENTERS - T)))

    def _spread(self, es):
        p = np.empty(self.N_TEMP)
        for sn in range(self.N_TEMP):
            d = abs(sn - es)
            p[sn] = 0.65 if d == 0 else 0.20 if d == 1 else 0.08 if d == 2 else 0.02
        return p / p.sum()

    def _build_B_T_prior_np(self, k_env):
        B = np.zeros((self.N_TEMP, self.N_TEMP, self.N_OCC,
                      self.N_W, self.N_H, self.N_ACTIONS))
        for s in range(self.N_TEMP):
            T = self.TEMP_CENTERS[s]
            for a in range(self.N_ACTIONS):
                hs = self.heat_sp[a // self.N_COOL]
                cs = self.cool_sp[a %  self.N_COOL]
                if   T < hs - 0.5: d_hvac =  min(0.5 + 0.1*(hs - T), 2.0)
                elif T > cs + 0.5: d_hvac = -min(0.5 + 0.1*(T - cs), 2.0)
                else:              d_hvac = 0.0
                for o in range(self.N_OCC):
                    d_occ = 0.15 if o == 1 else 0.0
                    for w in range(self.N_W):
                        d_env = k_env * (self.W_CENTERS[w] - T)
                        for h in range(self.N_H):
                            delta = np.clip(d_hvac + d_occ + d_env +
                                            self.H_CENTERS[h], -2.5, 2.5)
                            B[:, s, o, w, h, a] = self._spread(self._t2s(T + delta))
        return B

    @staticmethod
    def _build_sticky_np(n, stay, step):
        B = np.zeros((n, n))
        for i in range(n):
            B[i, i] = stay
            if i > 0:     B[i-1, i] += step
            else:         B[i,   i] += step
            if i < n - 1: B[i+1, i] += step
            else:         B[i,   i] += step
        return B / B.sum(axis=0, keepdims=True)

    def _build_A_T_np(self):
        A = np.zeros((self.N_TEMP, self.N_TEMP))
        for s in range(self.N_TEMP):
            p = np.full(self.N_TEMP, 0.01)
            p[s] = 0.85
            if s > 0:             p[s-1] += 0.065
            if s < self.N_TEMP-1: p[s+1] += 0.065
            A[:, s] = p / p.sum()
        return A

    def _build_drive_np(self):
        D = np.zeros((self.N_TEMP, self.N_ACTIONS))
        P = np.zeros(self.N_ACTIONS)
        for a in range(self.N_ACTIONS):
            hs = self.heat_sp[a // self.N_COOL]
            cs = self.cool_sp[a %  self.N_COOL]
            P[a] = max(0.0, 2.5 - (cs - hs))
            for s in range(self.N_TEMP):
                T = self.TEMP_CENTERS[s]
                if   T < hs - 0.5: D[s, a] = min(0.5 + 0.1*(hs - T), 2.0)
                elif T > cs + 0.5: D[s, a] = min(0.5 + 0.1*(T - cs), 2.0) * 1.25
        return D, P

    def _B_occ_t(self, hour: float, is_weekend: bool) -> torch.Tensor:
        p_arr, p_dep = occ_rates(hour, is_weekend)
        return torch.tensor([[1.0 - p_arr, p_dep],
                             [p_arr,       1.0 - p_dep]],
                            dtype=self.dt, device=self.device)

    # ------------------------------------------------------------------ #
    def update_context(self, month, day, hour, is_occ, outdoor_temp, direct_solar):
        cl, ch = get_seasonal_comfort(month, day)
        self.comfort_low, self.comfort_high = cl, ch
        self.comfort_mid = 0.5 * (cl + ch)
        self.is_occupied = is_occ
        self.hour = float(hour)
        try:
            self.is_weekend = date(2024, month, day).weekday() >= 5
        except Exception:
            self.is_weekend = False
        self.outdoor_temp = outdoor_temp
        self.direct_solar = direct_solar
        # Time-of-use price signal for the energy preference (see _plan). The
        # effective multiplier is 1 + tou_weight*(tou_mult-1), so tou_weight=0
        # disables TOU and tou_weight=1 applies the full 1.0/1.5/3.0 schedule.
        self.tou_mult = tou_multiplier(month, day, hour)

    def observe_congestion(self, cong_obs):
        """Update the latent congestion belief from a noisy per-agent observation
        of how loaded the *other* floors are (normalised to [0,1]). The EMA is
        the posterior mean of a slow random-walk latent under Gaussian noise --
        i.e. a 1-D Bayesian filter, the partially-observed analogue of the other
        belief states. cong_obs: array-like (N,) in [0,1]."""
        c = torch.as_tensor(cong_obs, dtype=self.dt, device=self.device).clamp(0.0, 1.0)
        self.cong = (1.0 - self.cong_alpha) * self.cong + self.cong_alpha * c

    # ------------------------------------------------------------------ #
    def _C_T_pair_t(self):
        C_occ = np.empty(self.N_TEMP); C_un = np.empty(self.N_TEMP)
        lo_u = self.comfort_low - self.SETBACK_MARGIN
        hi_u = self.comfort_high + self.SETBACK_MARGIN
        for s in range(self.N_TEMP):
            T = self.TEMP_CENTERS[s]
            if self.comfort_low <= T <= self.comfort_high:
                C_occ[s] = self.comfort_weight * (2.0 - 0.6*abs(T - self.comfort_mid))
            else:
                v = max(self.comfort_low - T, 0) + max(T - self.comfort_high, 0)
                C_occ[s] = -self.comfort_weight * (1.5*v + v*v)
            if lo_u <= T <= hi_u:
                C_un[s] = 0.0
            else:
                v = max(lo_u - T, 0) + max(T - hi_u, 0)
                C_un[s] = -self.comfort_weight * self.unocc_gate * (1.5*v + v*v)
        t = lambda x: torch.as_tensor(x, dtype=self.dt, device=self.device)
        return t(C_occ), t(C_un)

    @torch.no_grad()
    def _adapt_C(self, T_arr, is_occ_actual):
        """v12: slow integral control on each agent's energy weight, driven by
        its OWN comfort error (only while occupied). Comfortable agents raise the
        weight (save more); agents drifting out of band lower it (protect comfort).
        Bounded, so it cannot run away; lr 0 / learn_C False recovers v10."""
        if not is_occ_actual:
            return
        T = torch.as_tensor(T_arr, dtype=self.dt, device=self.device)
        err = (self.comfort_low - T).clamp_min(0.0) + (T - self.comfort_high).clamp_min(0.0)
        self.cerr_ema = (1 - self.c_beta) * self.cerr_ema + self.c_beta * err
        # delta > 0 when comfortable (cerr < tol) -> weight rises; < 0 otherwise
        delta = self.c_lr * (self.c_tol - self.cerr_ema)
        self.energy_w = (self.energy_w + delta).clamp(self.energy_w_min,
                                                       self.energy_w_max)

    @torch.no_grad()
    def _couple_prep(self, T_arr):
        """v14: compute the per-agent coupling drift (degC) for this step from
        the observed neighbour temperatures and current belief mean."""
        T = torch.as_tensor(T_arr, dtype=self.dt, device=self.device)
        T_self = (self.qT * self.TC).sum(1)
        T_nb = (T[self.nb_idx] * self.nb_mask).sum(1) \
               / self.nb_mask.sum(1).clamp_min(1e-6)
        self._cpl_g = T_nb - T_self
        self._cpl_delta = self.kc * self._cpl_g

    @torch.no_grad()
    def _couple_shift(self, P):
        """Drift a temp distribution P (N,X) or (N,X,A) by self._cpl_delta degC,
        in TEMPERATURE space (handles the non-uniform TEMP_CENTERS grid)."""
        xt = self.TC.view(1, self.N_TEMP, 1)
        xs = self.TC.view(1, 1, self.N_TEMP) + self._cpl_delta.view(self.N, 1, 1)
        S = (1.0 - (xt - xs).abs() / self.couple_h).clamp_min(0.0)   # (N,Xt,Xs)
        S = S / S.sum(1, keepdim=True).clamp_min(1e-8)
        if P.dim() == 2:
            return torch.einsum('nts,ns->nt', S, P)
        return torch.einsum('nts,nsa->nta', S, P)

    # ------------------------------------------------------------------ #
    # INFERENCE — batched over agents
    # ------------------------------------------------------------------ #
    @torch.no_grad()
    def _infer(self, T_arr: np.ndarray, is_occ_actual: bool):
        t_idx = torch.tensor([self._t2s(T) for T in T_arr],
                             device=self.device, dtype=torch.long)   # (N,)
        w_idx = self._w2s(self.outdoor_temp)
        o_idx = 1 if is_occ_actual else 0

        like_T = self.A_T[t_idx, :]                                  # (N, T)
        like_O = torch.full((2,), 0.05, dtype=self.dt, device=self.device)
        like_O[o_idx] = 0.95
        like_W = torch.full((self.N_W,), 0.02, dtype=self.dt, device=self.device)
        like_W[w_idx] = 1.0

        if self.prev_action is None:
            self.qT = like_T / like_T.sum(1, keepdim=True)
            self.qO = like_O.expand(self.N, -1) / like_O.sum()
            self.qW = (like_W / like_W.sum()).expand(self.N, -1).clone()
            self.qH = torch.full_like(self.qH, 1.0 / self.N_H)
            return t_idx, o_idx, w_idx

        a = self.prev_action                                          # (N,)
        ar = torch.arange(self.N, device=self.device)
        B_a = self.B_T.permute(0, 6, 1, 2, 3, 4, 5)[ar, a]            # (N,O,W,H,X,T)

        # latent H: evidence through the transition model
        pred = torch.einsum('nowhxt,nt,no,nw->nxh', B_a,
                            self.qT, self.qO, self.qW)                # (N,X,H)
        like_H = torch.einsum('nx,nxh->nh', like_T, pred).clamp_min(1e-12)
        prior_H = self.qH @ self.B_H.T
        qH = prior_H * like_H
        qH = qH / qH.sum(1, keepdim=True)
        self.qH = 0.97 * qH + 0.03 / self.N_H

        # temperature: predict-then-correct
        prior_T = torch.einsum('nxh,nh->nx', pred, self.qH)
        if self.couple:
            self._cpl_mu0 = (prior_T * self.TC).sum(1)   # no-coupling prediction
            prior_T = self._couple_shift(prior_T)
        qT = prior_T * like_T
        self.qT = qT / qT.sum(1, keepdim=True).clamp_min(1e-12)

        # occupancy & weather (observed)
        prior_O = self.qO @ self._B_occ_t(self.hour, self.is_weekend).T
        qO = prior_O * like_O
        self.qO = qO / qO.sum(1, keepdim=True)
        prior_W = self.qW @ self.B_W.T
        qW = prior_W * like_W
        self.qW = qW / qW.sum(1, keepdim=True)

        return t_idx, o_idx, w_idx

    # ------------------------------------------------------------------ #
    # LEARNING — batched Dirichlet update on B_T
    # ------------------------------------------------------------------ #
    @torch.no_grad()
    def _learn(self, qT_prev, qH_prev, a, t_idx, o_idx, w_idx):
        qT_post = self.A_T[t_idx, :]
        qT_post = qT_post / qT_post.sum(1, keepdim=True)              # (N,X)
        upd = torch.einsum('nx,nt,nh->nhxt', qT_post, qT_prev, qH_prev)
        ar = torch.arange(self.N, device=self.device)
        # advanced indexing: (N, H, X, T) view of the (o, w, a) slices
        self.pB_T[ar, o_idx, w_idx, :, :, :, a] += self.lr_pB * upd
        sl = self.pB_T[ar, o_idx, w_idx, :, :, :, a]
        self.B_T[ar, o_idx, w_idx, :, :, :, a] = sl / sl.sum(2, keepdim=True)
        self.b_updates += self.N
        prev_s = qT_prev.argmax(1).cpu().numpy()
        a_np = a.cpu().numpy()
        for n in range(self.N):
            self.transition_counts[n, prev_s[n], a_np[n]] += 1

    # ------------------------------------------------------------------ #
    # PLANNING — batched EFE over agents AND actions
    # ------------------------------------------------------------------ #
    @torch.no_grad()
    def _plan(self) -> torch.Tensor:
        C_occ, C_un = self._C_T_pair_t()
        qT_a = self.qT.unsqueeze(2).expand(-1, -1, self.N_ACTIONS).clone()  # (N,T,A)
        qO, qW, qH = self.qO, self.qW, self.qH
        G = torch.zeros((self.N, self.N_ACTIONS), dtype=self.dt, device=self.device)

        B_flat = self.B_T.reshape(self.N, self._OWH, self._XTA)
        for k in range(self.policy_len):
            future_hour = self.hour + (k + 1) * self.STEP_HOURS
            qO = qO @ self._B_occ_t(future_hour, self.is_weekend).T
            qW = qW @ self.B_W.T
            qH = qH @ self.B_H.T
            # fold the entire (O,W,H) context with one batched matmul
            q_ctx = (qO[:, :, None, None] * qW[:, None, :, None]
                     * qH[:, None, None, :]).reshape(self.N, 1, self._OWH)
            M = torch.bmm(q_ctx, B_flat).reshape(
                self.N, self.N_TEMP, self.N_TEMP, self.N_ACTIONS)     # (N,X,T,A)
            qT_a = (M * qT_a.unsqueeze(1)).sum(2)                     # (N,X,A)
            if self.couple:
                qT_a = self._couple_shift(qT_a)
            Ck = qO[:, 1:2] * C_occ.unsqueeze(0) + (1 - qO[:, 1:2]) * C_un.unsqueeze(0)
            G += torch.einsum('nx,nxa->na', Ck, qT_a)
            # TOU-aware energy preference: HVAC drive is penalised more during
            # expensive hours, so the agent pre-conditions off-peak and coasts
            # on-peak. eff_tou = 1 + tou_weight*(tou_mult - 1).
            eff_tou = 1.0 + self.tou_weight * (self.tou_mult - 1.0)
            ew = self.energy_w.view(self.N, 1) if self.learn_C else self.energy_weight
            G -= (ew * eff_tou) * torch.einsum('nxa,xa->na', qT_a, self.DRIVE)
            # v10: congestion-gated drive penalty. When the agent infers the
            # other floors are peaking (self.cong high), its own high-drive
            # actions become less preferred, so it defers load and the floors
            # desynchronise. Gated per-agent by the congestion belief.
            if self.congestion_weight > 0.0:
                G -= (self.congestion_weight * self.cong.view(self.N, 1)) \
                     * torch.einsum('nxa,xa->na', qT_a, self.DRIVE)

        if self.epistemic_weight > 0:
            q_ow = (self.qO[:, :, None] * self.qW[:, None, :]).reshape(
                self.N, 1, self.N_OCC * self.N_W)
            B_ow = self.B_T.reshape(self.N, self.N_OCC * self.N_W,
                                    self.N_H * self._XTA)
            M = torch.bmm(q_ow, B_ow).reshape(
                self.N, self.N_H, self.N_TEMP, self.N_TEMP, self.N_ACTIONS)
            pred = torch.einsum('nhxta,nt->nxha', M, self.qT)         # (N,X,H,A)
            mix = torch.einsum('nxha,nh->nxa', pred, self.qH)
            Hmix = -(mix * (mix + 1e-12).log()).sum(1)
            Hcond = -torch.einsum('nh,nxha->na', self.qH,
                                  pred * (pred + 1e-12).log())
            G += self.epistemic_weight * (Hmix - Hcond).clamp_min(0.0)

        G -= self.deadband_weight * self.DB_PEN.unsqueeze(0)
        return G

    # ------------------------------------------------------------------ #
    def _reactive_action(self, T: float, ctx: dict) -> int:
        if T < self.comfort_low:
            hi, ci = self.N_HEAT - 1, self.N_COOL - 1
        elif T > self.comfort_high:
            hi, ci = 0, 0
        else:
            hi = int(np.argmin(np.abs(self.heat_sp - (self.comfort_low + 0.5))))
            ci = int(np.argmin(np.abs(self.cool_sp - (self.comfort_high - 0.5))))
            if T < self.comfort_mid: hi = min(hi + 1, self.N_HEAT - 1)
            else:                    ci = max(ci - 1, 0)
        if ctx:
            orient = ctx.get("orient", "none"); floor = ctx.get("floor", "mid")
            ztype  = ctx.get("type", "perimeter")
            if ztype == "core" and T > self.comfort_low + 0.5: hi = max(hi - 1, 0)
            if floor == "top" and self.outdoor_temp < 5.0 and T < self.comfort_mid:
                hi = min(hi + 1, self.N_HEAT - 1)
            if floor == "top" and self.outdoor_temp > 22.0 and T > self.comfort_mid:
                ci = max(ci - 1, 0)
            if orient == "south" and self.direct_solar > 200:
                if T > self.comfort_mid: ci = max(ci - 1, 0)
                hi = max(hi - 1, 0)
            if orient == "east" and self.hour < 13 and self.direct_solar > 200:
                if T > self.comfort_mid: ci = max(ci - 1, 0)
            if orient == "west" and self.hour >= 12 and self.direct_solar > 200:
                if T > self.comfort_mid: ci = max(ci - 1, 0)
            if orient == "north" and self.outdoor_temp < 15.0 and T < self.comfort_mid:
                hi = min(hi + 1, self.N_HEAT - 1)
        return hi * self.N_COOL + ci

    # ------------------------------------------------------------------ #
    # MAIN STEP — all agents at once. T_arr: (N,) zone temps.
    # ------------------------------------------------------------------ #
    def step(self, T_arr: np.ndarray, is_occ_actual: bool):
        self.step_count += 1
        qT_prev, qH_prev = self.qT.clone(), self.qH.clone()
        a_prev = self.prev_action

        if self.couple:
            self._couple_prep(T_arr)
        t_idx, o_prev_idx, w_prev_idx = self._infer(T_arr, is_occ_actual)
        if self.couple and self.couple_learn and self._cpl_mu0 is not None:
            T_obs = self.TC[t_idx]
            resid = T_obs - (self._cpl_mu0 + self._cpl_delta)  # vs coupled pred
            self.kc = (self.kc + self.couple_lr * self._cpl_g * resid)\
                          .clamp(0.0, self.couple_k_max)
        if self.couple and self._cpl_mu0 is not None:
            T_obs = self.TC[t_idx]
            self._pred_err_nocouple += (T_obs - self._cpl_mu0).abs()
            self._pred_err_couple   += (T_obs - (self._cpl_mu0 + self._cpl_delta)).abs()
            self._pred_count += 1
        if a_prev is not None and not self.freeze_B and self.lr_pB > 0:
            # context indices of the PREVIOUS step were stored alongside
            self._learn(qT_prev, qH_prev, a_prev,
                        t_idx, self._o_prev, self._w_prev)

        if self.learn_C:
            self._adapt_C(T_arr, is_occ_actual)
        self.occ_belief_sum += self.qO[:, 1].cpu().numpy()
        EH = (self.qH @ torch.as_tensor(self.H_CENTERS, dtype=self.dt,
                                        device=self.device)).cpu().numpy()
        self.H_belief_sum += EH
        self.H_trace.append(EH)
        self.belief_count += 1

        G = self._plan()
        if self.action_temp > 0.0:
            # Stochastic selection: sample each agent's action from softmax(G/temp).
            # Independent per-agent draws break the symmetry of identical floors,
            # so they can desynchronise instead of all picking the same action.
            probs = torch.softmax(G / self.action_temp, dim=1)            # (N,A)
            a = torch.multinomial(probs, 1).squeeze(1)                    # (N,)
        else:
            a = G.argmax(1)                                               # (N,)

        # safety-net override (tiny CPU loop — N is 15 at most)
        a_np = a.cpu().numpy()
        for n in range(self.N):
            T = float(T_arr[n])
            viol = max(self.comfort_low - T, 0) + max(T - self.comfort_high, 0)
            if self.override_mode == "aggressive":
                if   viol > 2.0:  a_np[n] = self._reactive_action(T, self.struct_ctxs[n])
                elif viol > 0.5 and np.random.random() < 0.8:
                    a_np[n] = self._reactive_action(T, self.struct_ctxs[n])
                elif viol > 0.0 and np.random.random() < 0.9:
                    a_np[n] = self._reactive_action(T, self.struct_ctxs[n])
            else:
                if viol > 2.0:
                    a_np[n] = self._reactive_action(T, self.struct_ctxs[n])
        a = torch.as_tensor(a_np, device=self.device, dtype=torch.long)

        self.prev_action = a
        self._o_prev = 1 if is_occ_actual else 0
        self._w_prev = self._w2s(self.outdoor_temp)

        htg = self.heat_sp[a_np // self.N_COOL]
        clg = self.cool_sp[a_np %  self.N_COOL]
        return htg, clg

    # ------------------------------------------------------------------ #
    def reset_episode(self):
        self.qT.fill_(1.0 / self.N_TEMP)
        self.qO = torch.tensor([[0.65, 0.35]], dtype=self.dt,
                               device=self.device).repeat(self.N, 1)
        self.qW.fill_(1.0 / self.N_W)
        self.qH.fill_(1.0 / self.N_H)
        self.prev_action = None
        self.H_trace = []

    def reset_occ_stats(self):
        self.occ_belief_sum[:] = 0.0
        self.H_belief_sum[:] = 0.0
        self.belief_count = 0

    def get_state(self) -> dict:
        return {
            'B_T':  self.B_T.cpu().numpy(),
            'pB_T': self.pB_T.cpu().numpy(),
            'transition_counts': self.transition_counts.copy(),
            'step_count': self.step_count,
            'b_updates':  self.b_updates,
            'energy_w': self.energy_w.cpu().numpy(),                  # v12 learned tradeoff
            'kc': (self.kc.cpu().numpy() if getattr(self, 'couple', False) else None),  # v14 conductance
        }

    def set_state(self, state: dict):
        self.B_T  = torch.as_tensor(state['B_T'],  dtype=self.dt, device=self.device)
        self.pB_T = torch.as_tensor(state['pB_T'], dtype=self.dt, device=self.device)
        self.transition_counts = state['transition_counts']
        self.step_count = state['step_count']
        self.b_updates  = state['b_updates']
        if state.get('energy_w') is not None:
            self.energy_w = torch.as_tensor(state['energy_w'], dtype=self.dt, device=self.device)
        if state.get('kc') is not None and getattr(self, 'couple', False):
            self.kc = torch.as_tensor(state['kc'], dtype=self.dt, device=self.device)

    def get_stats(self) -> dict:
        b_change = float((self.B_T - self.initial_B_T).abs().mean().cpu())
        coverage = float((self.transition_counts > 0).mean())
        c = max(self.belief_count, 1)
        return {'mean_b_change': b_change,
                'mean_coverage': coverage,
                'total_b_updates': self.b_updates,
                'mean_occ_belief': float(self.occ_belief_sum.mean() / c),
                'mean_H': float(self.H_belief_sum.mean() / c),
                'per_agent_H': (self.H_belief_sum / c).tolist()}


# =============================================================================
# OFFICE WRAPPER — same external API as v6's PyMDPOffice
# =============================================================================
class PyMDPOffice:
    MODES = ('zone', 'floor', 'single')

    def __init__(self, env, mode='zone', structural=True,
                 comfort_weight=1.0, energy_weight=0.5, tou_weight=1.0,
                 congestion_weight=0.0, action_temp=0.0, cong_alpha=0.3, policy_len=4,
                 learn_C=False, c_lr=0.02, energy_w_min=0.0, energy_w_max=1.5,
                 couple=False, couple_k=0.12, couple_learn=True,
                 couple_lr=0.01, couple_k_max=0.6,
                 lr_pB=1.0, pB_prior_scale=2.0, epistemic_weight=0.2,
                 unocc_gate=0.1, deadband_weight=4.0, freeze_B=False,
                 override="safety", device="auto", dtype="float"):
        if mode not in self.MODES:
            raise ValueError(f"mode must be one of {self.MODES}")
        self.mode = mode
        self.structural = structural

        heat_low  = float(env.action_space.low[0])
        heat_high = float(env.action_space.high[0])
        cool_low  = float(env.action_space.low[N_ZONES])
        cool_high = float(env.action_space.high[N_ZONES])

        _floor_idx = {"bottom": 0, "mid": 1, "top": 2}
        if mode == 'zone':
            self.agent_to_zones = [[i] for i in range(N_ZONES)]
            ctxs = [dict(ZONE_META[i]) if structural else {} for i in range(N_ZONES)]
            # which of the 3 air-loop floors each agent sits on (for congestion)
            self.agent_floor = [_floor_idx[ZONE_META[i]["floor"]] for i in range(N_ZONES)]
        elif mode == 'floor':
            self.agent_to_zones = [FLOOR_ZONES[f] for f in FLOORS]
            ctxs = [({"floor": f, "type": "perimeter", "orient": "mixed"}
                     if structural else {}) for f in FLOORS]
            self.agent_floor = [_floor_idx.get(f, 1) for f in FLOORS]
        else:
            self.agent_to_zones = [list(range(N_ZONES))]
            ctxs = [({"floor": "mid", "type": "perimeter", "orient": "mixed"}
                     if structural else {})]
            self.agent_floor = None   # single agent spans all floors -> no congestion

        self.batch = BatchedFactoredAgents(
            n_agents=len(self.agent_to_zones),
            heat_low=heat_low, heat_high=heat_high,
            cool_low=cool_low, cool_high=cool_high,
            struct_ctxs=ctxs,
            comfort_weight=comfort_weight, energy_weight=energy_weight,
            tou_weight=tou_weight,
            congestion_weight=congestion_weight, action_temp=action_temp,
            cong_alpha=cong_alpha,
            learn_C=learn_C, c_lr=c_lr,
            energy_w_min=energy_w_min, energy_w_max=energy_w_max,
            couple=couple, couple_k=couple_k, couple_learn=couple_learn,
            couple_lr=couple_lr, couple_k_max=couple_k_max,
            couple_neighbors=([ZONE_NEIGHBORS[i] for i in range(N_ZONES)]
                              if (couple and mode == 'zone') else None),
            policy_len=policy_len, lr_pB=lr_pB,
            pB_prior_scale=pB_prior_scale,
            epistemic_weight=epistemic_weight, unocc_gate=unocc_gate,
            deadband_weight=deadband_weight, freeze_B=freeze_B,
            override=override, device=device, dtype=dtype)

        self.htg_sp = np.full(N_ZONES, 21.0, dtype=np.float32)
        self.clg_sp = np.full(N_ZONES, 24.0, dtype=np.float32)
        self._cong_ref = 1e-6   # running peak of total floor power (normaliser)

    def observe_floor_power(self, floor_power):
        """Feed the 3 per-floor powers [bot,mid,top]. For each agent we form a
        congestion observation = (load of the OTHER floors) / (running peak
        total), i.e. how hard the floors this agent does NOT control are pushing
        the shared electric service right now. High => others are peaking =>
        defer. No direct view of others' actions; only the shared power signal."""
        if self.agent_floor is None:
            return
        fp = np.asarray(floor_power, dtype=np.float64)
        total = float(fp.sum())
        self._cong_ref = max(self._cong_ref, total)
        others = total - fp                      # (3,) load of the other two floors
        denom = max(self._cong_ref, 1e-6)
        cong_by_floor = np.clip(others / denom, 0.0, 1.0)   # (3,)
        cong_per_agent = np.array([cong_by_floor[f] for f in self.agent_floor],
                                  dtype=np.float32)
        self.batch.observe_congestion(cong_per_agent)

    def get_action(self, obs, info=None) -> np.ndarray:
        month = int(obs[ObsIndex.MONTH]); day = int(obs[ObsIndex.DAY])
        hour  = int(obs[ObsIndex.HOUR])
        outdoor_temp = float(obs[ObsIndex.OUTDOOR_TEMP])
        direct_solar = float(obs[ObsIndex.DIRECT_SOLAR])
        is_occ = get_agent_occupancy_state(month, day, hour) == 'occupied'

        T_arr = np.array([
            ObsIndex.get_zone_temp(obs, z[0]) if len(z) == 1
            else float(np.mean([ObsIndex.get_zone_temp(obs, zi) for zi in z]))
            for z in self.agent_to_zones])

        self.batch.update_context(month, day, hour, is_occ,
                                  outdoor_temp, direct_solar)
        htg, clg = self.batch.step(T_arr, is_occ)

        for n, zones in enumerate(self.agent_to_zones):
            for z in zones:
                self.htg_sp[z] = htg[n]
                self.clg_sp[z] = clg[n]
        return np.concatenate([self.htg_sp, self.clg_sp]).astype(np.float32)

    def reset(self):           self.batch.reset_episode()
    def reset_occ_stats(self): self.batch.reset_occ_stats()
    def get_stats(self):       return self.batch.get_stats()

    def save(self, path: str):
        with open(path, 'wb') as f:
            pickle.dump({'mode': self.mode, 'structural': self.structural,
                         'version': 4, 'backend': 'torch',
                         'state': self.batch.get_state()}, f)
        print(f"  [save] -> {path}  (total steps: {self.batch.step_count})")

    def load(self, path: str):
        if not os.path.exists(path):
            print(f"  [load] No checkpoint at '{path}' - starting fresh.")
            return
        with open(path, 'rb') as f:
            data = pickle.load(f)
        if data.get('mode') != self.mode or data.get('version') != 4:
            print(f"  [load] WARNING: mode/version mismatch - skipping.")
            return
        if data.get('structural') != self.structural:
            print(f"  [load] WARNING: structural-flag mismatch - skipping.")
            return
        self.batch.set_state(data['state'])
        print(f"  [load] <- {path}  (total steps: {self.batch.step_count})")


# =============================================================================
# SIMULATION RUNNER  (same harness as v6; lazy Sinergym/MADDPG imports)
# =============================================================================
def robust_reset(env, seed, first_obs_timeout=120.0):
    """Reset once; if EnergyPlus loses Sinergym 3.11's hardcoded 10s startup race
    and returns the random-sample fallback observation, DON'T re-race (that just
    loses the same 10s window again). The real first observation lands in the
    underlying EplusEnv queues moments later (right after 'System is ready'), so
    block on those queues with a longer timeout to recover it, reconstructing the
    return exactly as EplusEnv.reset does:
        np.fromiter(obs_dict.values(), dtype=np.float32)
    Assumes no observation-transforming wrapper sits above EplusEnv (v7 uses only
    CustomRewardWrapper, which passes obs through unchanged). The seed is applied
    on the single reset so the run stays deterministic."""
    from queue import Empty

    obs, info = env.reset(seed=seed)
    m, d, h = float(obs[0]), float(obs[1]), float(obs[2])
    if (1 <= m <= 12) and (1 <= d <= 31) and (0 <= h <= 23):
        return obs, info

    print(f"[reset] fallback obs (m={m:.0f} d={d:.0f} h={h:.0f}) — EnergyPlus lost "
          f"the 10s init race. Waiting up to {first_obs_timeout:.0f}s for the real "
          f"first observation instead of re-racing...")
    raw = env.unwrapped                       # the EplusEnv (queues live here)
    try:
        obs_dict = raw.obs_queue.get(timeout=first_obs_timeout)
        info     = raw.info_queue.get(timeout=first_obs_timeout)
    except (Empty, AttributeError) as e:
        print(f"[reset] WARNING: real observation never arrived ({type(e).__name__}); "
              "proceeding with the fallback. Cross-machine results are unreliable.")
        return obs, info

    info.update({'timestep': raw.timestep})
    raw.last_obs, raw.last_info = obs_dict, info
    real_obs = np.fromiter(obs_dict.values(), dtype=np.float32)
    print(f"[reset] recovered real observation "
          f"(m={real_obs[0]:.0f} d={real_obs[1]:.0f} h={real_obs[2]:.0f}) "
          f"after the slow init.")
    return real_obs, info


def run_simulation(weather="mixed", episodes=1, mode='zone', structural=True,
                   comfort_weight=1.0, energy_weight=0.5, tou_weight=1.0,
                   congestion_weight=0.0, action_temp=0.0, cong_alpha=0.3, policy_len=4,
                   learn_C=False, c_lr=0.02, energy_w_min=0.0, energy_w_max=1.5,
                   couple=False, couple_k=0.12, couple_learn=True,
                   couple_lr=0.01, couple_k_max=0.6,
                   lr_pB=1.0, pB_prior_scale=2.0, epistemic_weight=0.2,
                   unocc_gate=0.1, deadband_weight=4.0, freeze_B=False,
                   override="safety", device="auto", dtype="float",
                   checkpoint=None, floor_power_idx=None, floor_power_scale=1.0) -> dict:

    from register_env import make_custom_env
    # NOTE: metrics + CustomRewardWrapper now come from metrics_utils (the SAME
    # module the deployment bridge uses) so v7 and the bridge score an identical
    # trajectory identically. This changes two reported numbers vs the old
    # maddpg_v3 path: (a) mean custom_reward — metrics_utils uses a 1.5 unoccupied
    # energy multiplier vs maddpg_v3's 1.1; (b) CSAccumulator.mean — metrics_utils
    # averages active steps flat, maddpg_v3 state-weighted them. original_reward,
    # compute_zone_comfort_rate and compute_mean_deviation are unchanged.
    from metrics_utils import (
        CustomRewardWrapper, CSAccumulator,
        compute_zone_comfort_rate as maddpg_compute_zone_comfort_rate,
        compute_mean_deviation as maddpg_compute_mean_deviation,
        compute_hourly_linear_reward,
        load_factor,
        floor_coincidence_factor,
    )

    # Per-floor power obs indices for the game-theoretic coincidence factor.
    # The 3 floors share the building electric service (the demand charge), so
    # the coincidence factor measures whether they peak together. Requires the
    # per-floor power outputs (see OfficeMedium_MultiAgent_perfloor.epJSON) to
    # be surfaced in the observation; pass their obs indices as [bot, mid, top].
    if isinstance(floor_power_idx, str):
        floor_power_idx = [int(x) for x in floor_power_idx.split(",") if x.strip()]
    if floor_power_idx is not None and len(floor_power_idx) != 3:
        print(f"[coincidence] WARNING: expected 3 floor-power indices, got "
              f"{floor_power_idx}; disabling coincidence metric.")
        floor_power_idx = None

    def compute_zone_comfort_rate(obs):
        return maddpg_compute_zone_comfort_rate(obs, occupied_only=True)

    def compute_mean_deviation(obs):
        return maddpg_compute_mean_deviation(obs, occupied_only=True)

    base_env = make_custom_env(weather=weather, real_world=False)
    env = CustomRewardWrapper(base_env)
    assert env.action_space.shape[0] == N_ZONES * 2

    agent = PyMDPOffice(env, mode=mode, structural=structural,
                        comfort_weight=comfort_weight,
                        energy_weight=energy_weight, tou_weight=tou_weight,
                        congestion_weight=congestion_weight, action_temp=action_temp,
                        cong_alpha=cong_alpha,
                        learn_C=learn_C, c_lr=c_lr,
                        energy_w_min=energy_w_min, energy_w_max=energy_w_max,
                        couple=couple, couple_k=couple_k, couple_learn=couple_learn,
                        couple_lr=couple_lr, couple_k_max=couple_k_max,
                        policy_len=policy_len,
                        lr_pB=lr_pB, pB_prior_scale=pB_prior_scale,
                        epistemic_weight=epistemic_weight,
                        unocc_gate=unocc_gate,
                        deadband_weight=deadband_weight, freeze_B=freeze_B,
                        override=override, device=device, dtype=dtype)
    if checkpoint:
        agent.load(checkpoint)

    dev = agent.batch.device
    print(f"{'=' * 80}")
    print(f"Factored AIF Office v10 (PyTorch) | Weather: {weather} | Mode: {mode}")
    print(f"  Device:       {dev}  ({torch.cuda.get_device_name(dev) if dev.type=='cuda' else 'CPU'})")
    print(f"  dtype:        {agent.batch.dt}")
    print(f"  Agents:       {agent.batch.N} (batched)   B_T: {tuple(agent.batch.B_T.shape)}")
    print(f"  Horizon:      {policy_len} steps   Override: {override}   "
          f"Deadband wt: {deadband_weight}   Learning: "
          f"{'FROZEN' if freeze_B else f'on (lr_pB={lr_pB})'}")
    print(f"  Episodes:     {episodes}   Checkpoint: {checkpoint or 'disabled'}")
    print(f"{'=' * 80}\n")

    all_rewards=[]; all_energy=[]; all_zcr=[]; all_dev=[]; all_custom=[]; all_cost=[]
    all_orig=[]; all_db=[]; all_cs=[]; all_cs_occ=[]; all_hlr=[]

    # --- clean per-zone (state, action) log -----------------------------------
    # One row per timestep: the temperature each zone was at when the agent
    # decided, plus the heating/cooling setpoint it commanded for that zone.
    # Truncated fresh each run (unlike actions_v7.csv/obs_v7.csv which append),
    # with a labelled header so it drops straight into pandas.
    import csv as _csv
    ZONE_LOG_PATH = "zone_temps_actions_v7.csv"
    _zlog_f = open(ZONE_LOG_PATH, "w", newline="")
    _zlog = _csv.writer(_zlog_f)
    _zlog.writerow(
        ["episode", "step", "month", "day", "hour", "outdoor_temp"]
        + [f"T_{z}"   for z in OCCUPIED_ZONES]
        + [f"HTG_{z}" for z in OCCUPIED_ZONES]
        + [f"CLG_{z}" for z in OCCUPIED_ZONES])
    print(f"[log] per-zone temps+actions -> {ZONE_LOG_PATH}")

    RUN_SEED = 0  # fixed base seed -> reproducible across machines/runs
    for ep in range(episodes):
        agent.reset()
        # Seed reset so the RANDOM-FALLBACK observation (drawn from the env's
        # np_random when EnergyPlus isn't ready yet — the "queue empty, returning
        # a random observation" warning) is identical across machines instead of
        # OS-entropy random. Deterministic weather is unaffected; this only pins
        # the fallback RNG so the two machines start from the same obs.
        obs, info = robust_reset(env, seed=RUN_SEED + ep)
        if floor_power_idx is not None and max(floor_power_idx) >= len(obs):
            print(f"[coincidence] WARNING: floor_power_idx {floor_power_idx} exceeds "
                  f"observation size {len(obs)} (valid indices 0-{len(obs)-1}). "
                  f"The per-floor power is NOT in the observation yet -- add the "
                  f"per-floor meters/variables to the Sinergym variables/meters "
                  f"config first, then pass their real obs indices. "
                  f"Disabling the coincidence metric for this run.")
            floor_power_idx = None
        rewards=[]; energy_list=[]; step_idx=0
        if getattr(agent.batch, "couple", False):
            agent.batch._pred_err_couple.zero_()
            agent.batch._pred_err_nocouple.zero_()
            agent.batch._pred_count = 0
        floor_power_rows=[]   # (T,3) per-floor power for the coincidence factor
        _diag_rows=[]   # [month,day,hour,Pbot_kW,Pmid_kW,Ptop_kW] for peak_diagnostic
        terminated=truncated=False; current_month=0
        total_zok=total_zc=0; total_dev=total_dc=0.0
        total_custom_r=0.0; total_orig_r=0.0
        db_violations=0; db_total=0; total_hlr=0.0
        cs_ep=CSAccumulator(); cs_m=CSAccumulator()
        m_energy=m_zok=m_zc=m_dev=m_dc=0.0
        m_custom_r=0.0; m_orig_r=0.0; m_hlr=0.0
        m_cost_energy=0.0; m_cost_demand=0.0          # per-month $ cost
        prev_ce=0.0; prev_cd=0.0                       # last cumulative $ seen
        m_t_min=np.full(N_ZONES,999.0); m_t_max=np.full(N_ZONES,-999.0)

        print(f"Episode {ep + 1}/{episodes}")
        print("-" * 80)
        t0=time.time()

        while not (terminated or truncated):
            if floor_power_idx is not None and congestion_weight > 0.0:
                agent.observe_floor_power([float(obs[i]) * floor_power_scale
                                           for i in floor_power_idx])
            action = agent.get_action(obs, info)
            # --- clean per-zone (state, action) row -----------------------
            # obs here is the observation the agent reacted to (pre-step), so
            # T_<zone> is the temperature that produced this action. action is
            # [15 heating setpoints, 15 cooling setpoints] in OCCUPIED_ZONES order.
            step_idx += 1
            _zlog.writerow(
                [ep + 1, step_idx,
                 int(obs[ObsIndex.MONTH]), int(obs[ObsIndex.DAY]),
                 int(obs[ObsIndex.HOUR]), f"{float(obs[ObsIndex.OUTDOOR_TEMP]):.3f}"]
                + [f"{ObsIndex.get_zone_temp(obs, i):.3f}" for i in range(N_ZONES)]
                + [f"{float(action[i]):.3f}"          for i in range(N_ZONES)]
                + [f"{float(action[N_ZONES + i]):.3f}" for i in range(N_ZONES)])
            # --- ACTION LOG (for cross-machine / cross-pipeline diffing) ---
            with open("actions_v7.csv", "a") as _f:
                _o = np.asarray(obs).ravel(); _a = np.asarray(action).ravel()
                _f.write(",".join(f"{x:.6f}" for x in [_o[0], _o[1], _o[2], *_a]) + "\n")
            # --- FULL OBS LOG (to check whether EnergyPlus feeds the agent the
            #     SAME observation on both machines; higher precision so we can
            #     see tiny physics differences, not just discretised actions) ---
            with open("obs_v7.csv", "a") as _f:
                _f.write(",".join(f"{x:.9g}" for x in np.asarray(obs).ravel()) + "\n")
            obs, reward, terminated, truncated, info = env.step(action)
            rewards.append(reward)
            custom_r=info.get('custom_reward',0.0); orig_r=info.get('original_reward',0.0)
            total_custom_r+=custom_r; m_custom_r+=custom_r
            total_orig_r+=orig_r; m_orig_r+=orig_r
            # --- $ cost: info carries cumulative totals; take per-step deltas --
            ce=info.get('cost_energy_usd',0.0); cd=info.get('cost_demand_usd',0.0)
            m_cost_energy+=ce-prev_ce; prev_ce=ce
            m_cost_demand+=cd-prev_cd; prev_cd=cd
            cs_ep.update(obs); cs_m.update(obs)
            hlr,_=compute_hourly_linear_reward(obs)
            total_hlr+=hlr; m_hlr+=hlr
            for i in range(N_ZONES):
                db_total+=1
                if action[i]>=action[N_ZONES+i]-2.0: db_violations+=1
            pwr=float(obs[ObsIndex.HVAC_DEMAND]); energy_list.append(pwr); m_energy+=pwr
            if floor_power_idx is not None:
                floor_power_rows.append([float(obs[i]) * floor_power_scale for i in floor_power_idx])
                _diag_rows.append(
                    [float(obs[ObsIndex.MONTH]), float(obs[ObsIndex.DAY]),
                     float(obs[ObsIndex.HOUR])]
                    + [float(obs[i]) * floor_power_scale / 1000.0 for i in floor_power_idx])
            zok,zc=compute_zone_comfort_rate(obs)
            total_zok+=zok; total_zc+=zc; m_zok+=zok; m_zc+=zc
            dv,dn=compute_mean_deviation(obs)
            total_dev+=dv*dn; total_dc+=dn; m_dev+=dv*dn; m_dc+=dn
            for i in range(N_ZONES):
                zt=ObsIndex.get_zone_temp(obs,i)
                if -10.0<=zt<=50.0:
                    m_t_min[i]=min(m_t_min[i],zt); m_t_max[i]=max(m_t_max[i],zt)
            month_now=int(obs[ObsIndex.MONTH])
            if month_now!=current_month:
                if current_month>0:
                    ekwh=m_energy*0.25/1000
                    avg_t=float(np.mean(ObsIndex.get_all_zone_temps(obs)))
                    t_min=float(np.min(m_t_min)) if np.any(m_t_min<999.0) else avg_t
                    t_max=float(np.max(m_t_max)) if np.any(m_t_max>-999.0) else avg_t
                    season="S" if get_seasonal_comfort(current_month,15)==COMFORT_SUMMER else "W"
                    m_zcr=m_zok/m_zc*100 if m_zc>0 else float('nan')
                    m_dv=m_dev/m_dc if m_dc>0 else 0.0
                    m_cost=m_cost_energy+m_cost_demand
                    stats=agent.get_stats()
                    print(f"  Month {current_month:2d} [{season}] | "
                          f"CR: {m_custom_r:8.2f} | OR: {m_orig_r:10.0f} | "
                          f"HLR: {m_hlr:9.2f} | E: {ekwh:7.0f}kWh | "
                          f"Cost: ${m_cost:8,.0f} (en ${m_cost_energy:,.0f}/dem ${m_cost_demand:,.0f}) | "
                          f"CS: {cs_m.mean:.3f} (occ: {cs_m.mean_occ:.3f}) | "
                          f"ZCR(occ): {m_zcr:5.1f}% | Dev: {m_dv:.3f}C | "
                          f"T: {avg_t:.1f}C [{t_min:.1f}-{t_max:.1f}] | "
                          f"OccBel: {stats['mean_occ_belief']:.2f} | "
                          f"E[H]: {stats['mean_H']:+.3f} | "
                          f"B_chg: {stats['mean_b_change']:.4f} | "
                          f"{time.time()-t0:.0f}s")
                current_month=month_now
                m_energy=m_zok=m_zc=m_dev=m_dc=0.0
                m_custom_r=0.0; m_orig_r=0.0; m_hlr=0.0
                m_cost_energy=0.0; m_cost_demand=0.0
                cs_m.reset()
                m_t_min=np.full(N_ZONES,999.0); m_t_max=np.full(N_ZONES,-999.0)
                agent.reset_occ_stats()

        ekwh=sum(energy_list)*0.25/1000
        zcr=total_zok/total_zc*100 if total_zc>0 else float('nan')
        mean_dev=total_dev/total_dc if total_dc>0 else 0.0
        db_rate=(db_violations/db_total*100) if db_total>0 else 0.0
        all_rewards.extend(rewards); all_energy.append(ekwh)
        all_zcr.append(zcr); all_dev.append(mean_dev); all_db.append(db_rate)
        all_custom.append(total_custom_r); all_orig.append(total_orig_r)
        all_cs.append(cs_ep.mean); all_cs_occ.append(cs_ep.mean_occ)
        all_hlr.append(total_hlr)

        stats=agent.get_stats()
        print("-"*80)
        print(f"  Episode {ep+1} Summary:")
        print(f"    Custom Reward:       {total_custom_r:.2f}")
        print(f"    Original Reward:     {total_orig_r:.2f}")
        print(f"    Hourly Reward:       {total_hlr:.2f}")
        print(f"    Total Energy:        {ekwh:,.0f} kWh")
        _peak_kW = (max(energy_list) / 1000.0) if energy_list else float('nan')
        _lf = load_factor(energy_list)
        print(f"    Peak Demand:         {_peak_kW:,.1f} kW")
        print(f"    Load Factor:         {_lf:.3f}   (avg/peak; higher=flatter=cheaper demand)")
        if floor_power_idx is not None and len(floor_power_rows) > 0:
            _fp = np.asarray(floor_power_rows, dtype=np.float64)        # (T,3)
            _cf = floor_coincidence_factor(_fp)
            _floor_peaks = _fp.max(axis=0) / 1000.0                     # kW per floor
            _coincident = _fp.sum(axis=1).max() / 1000.0               # kW summed peak
            print(f"    Floor Peaks (kW):    bot={_floor_peaks[0]:.1f}  "
                  f"mid={_floor_peaks[1]:.1f}  top={_floor_peaks[2]:.1f}  "
                  f"(Σ individual={_floor_peaks.sum():.1f})")
            print(f"    Coincident Peak:     {_coincident:.1f} kW")
            print(f"    Coincidence Factor:  {_cf:.3f}   "
                  f"(1=floors peak together/expensive, lower=staggered/cheaper)")
            if floor_power_idx is not None and len(_diag_rows) > 0:
                _dpath = f"floorlog_ep{ep+1}.npy"
                np.save(_dpath, np.asarray(_diag_rows, dtype=np.float64))
                print(f"    [diag] saved {_dpath}  ({len(_diag_rows)} steps)")
        ep_cost_energy=info.get('cost_energy_usd',0.0)
        ep_cost_demand=info.get('cost_demand_usd',0.0)
        ep_cost_total =info.get('cost_total_usd', ep_cost_energy+ep_cost_demand)
        print(f"    Energy Cost:         ${ep_cost_energy:,.2f}")
        print(f"    Demand Cost:         ${ep_cost_demand:,.2f}")
        print(f"    Total Cost:          ${ep_cost_total:,.2f}")
        all_cost.append(ep_cost_total)
        print(f"    CS (weighted/occ):   {cs_ep.mean:.4f} / {cs_ep.mean_occ:.4f}")
        print(f"    Zone-Comfort Rate:   {zcr:.1f}%")
        print(f"    Mean Deviation:      {mean_dev:.3f} C")
        print(f"    Deadband Violations: {db_rate:.1f}%")
        print(f"    B change:            {stats['mean_b_change']:.4f}   "
              f"E[H]: {stats['mean_H']:+.3f}")
        print(f"    Per-agent E[H]:      "
              + " ".join(f"{h:+.2f}" for h in stats['per_agent_H']))
        if getattr(agent.batch, "couple", False):
            kc = agent.batch.kc.detach().cpu().numpy()
            print(f"    Per-zone conductance k: "
                  + " ".join(f"{v:.3f}" for v in kc))
            _cores = [0, 1, 2]
            _perim = [i for i in range(N_ZONES) if i not in _cores]
            print(f"      core k (4 neighbours): {kc[_cores].mean():.3f}   "
                  f"perimeter k (3 neighbours): {kc[_perim].mean():.3f}")
            if agent.batch._pred_count > 0:
                en = agent.batch._pred_err_nocouple.cpu().numpy() / agent.batch._pred_count
                ec = agent.batch._pred_err_couple.cpu().numpy() / agent.batch._pred_count
                print(f"    Pred-err degC (no-cpl/cpl): "
                      f"{en.mean():.4f} / {ec.mean():.4f}")
                _hi = kc > 0.05
                if _hi.any():
                    print(f"      coupled zones (k>0.05, n={int(_hi.sum())}): "
                          f"{en[_hi].mean():.4f} / {ec[_hi].mean():.4f}  "
                          f"(improvement {(en[_hi]-ec[_hi]).mean():+.4f})")
        print()

    env.close()
    _zlog_f.close()
    print(f"[log] wrote per-zone temps+actions -> {ZONE_LOG_PATH}")
    if checkpoint:
        agent.save(checkpoint)

    print(f"\n{'='*80}")
    print(f"FINAL SUMMARY  [mode={mode}, device={agent.batch.device}]")
    print(f"{'='*80}")
    print(f"  Custom Reward total:   {sum(all_custom):.2f}")
    print(f"  Original Reward total: {sum(all_orig):.2f}")
    print(f"  Hourly Reward (sched): {sum(all_hlr):.2f}")
    print(f"  Env reward (mean):     {np.mean(all_rewards):.4f}")
    print(f"  Total Energy:          {sum(all_energy):,.0f} kWh")
    print(f"  Total Cost:            ${sum(all_cost):,.2f}")
    if episodes>1:
        print(f"  Cost per episode:      ${np.mean(all_cost):,.2f} (mean)")
    print(f"  CS (active, weighted): {np.nanmean(all_cs):.4f}")
    print(f"  CS (occupied only):    {np.nanmean(all_cs_occ):.4f}")
    print(f"  Zone-Comfort Rate:     {np.mean(all_zcr):.1f}%")
    print(f"  Mean Deviation:        {np.mean(all_dev):.3f} C")
    print(f"  Deadband Violations:   {np.mean(all_db):.1f}%")
    if episodes>1:
        print(f"  Ep1->Ep{episodes} CustomR: {all_custom[0]:.2f} -> {all_custom[-1]:.2f}")
        print(f"  Ep1->Ep{episodes} CS:      {all_cs[0]:.4f} -> {all_cs[-1]:.4f}")
        print(f"  Ep1->Ep{episodes} ZCR:     {all_zcr[0]:.1f}% -> {all_zcr[-1]:.1f}%")
    print(f"{'='*80}")

    return {'rewards': all_rewards, 'custom_reward_total': sum(all_custom),
            'original_reward_total': sum(all_orig),
            'hourly_reward_total': sum(all_hlr),
            'comfort_score': float(np.nanmean(all_cs)),
            'comfort_score_occ': float(np.nanmean(all_cs_occ)),
            'total_energy_kwh': sum(all_energy),
            'zone_comfort_rate': np.mean(all_zcr),
            'mean_deviation': np.mean(all_dev),
            'deadband_violation_rate': np.mean(all_db),
            'per_episode_custom': all_custom, 'per_episode_cs': all_cs,
            'per_episode_zcr': all_zcr, 'per_episode_energy': all_energy,
            'agent': agent}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Factored Active Inference v7 (PyTorch) - Sinergym Office")
    parser.add_argument("--mode", choices=PyMDPOffice.MODES, default="zone")
    parser.add_argument("--structural", dest="structural", action="store_true", default=True)
    parser.add_argument("--no-structural", dest="structural", action="store_false")
    parser.add_argument("--weather", default="mixed")
    parser.add_argument("--episodes", type=int, default=1)
    parser.add_argument("--policy_len", type=int, default=4)
    parser.add_argument("--lr_pB", type=float, default=1.0)
    parser.add_argument("--comfort_weight", type=float, default=1.0)
    parser.add_argument("--energy_weight", type=float, default=0.5)
    parser.add_argument("--tou_weight", type=float, default=1.0,
        help="Time-of-use strength for the energy preference. 0 = TOU off, "
             "1 = full PG&E B-19 schedule (energy up to 3x less preferred on-peak).")
    parser.add_argument("--floor_power_idx", type=str, default=None,
        help="Comma-separated obs indices of per-floor HVAC power [bot,mid,top] "
             "(e.g. '92,93,94'). Enables the per-floor coincidence-factor metric. "
             "Also required for congestion inference (--congestion_weight).")
    parser.add_argument("--congestion_weight", type=float, default=0.0,
        help="v10: weight on the congestion-gated drive penalty. 0 = off (= v9). "
             ">0 makes an agent defer its HVAC load when it infers the OTHER "
             "floors are peaking. Requires --floor_power_idx.")
    parser.add_argument("--action_temp", type=float, default=0.0,
        help="v10: softmax temperature for stochastic action selection. 0 = argmax "
             "(deterministic). >0 lets identical floors desynchronise (e.g. 0.3).")
    parser.add_argument("--cong_alpha", type=float, default=0.3,
        help="v10: congestion belief filter rate (EMA). Higher = faster, noisier.")
    parser.add_argument("--learn_C", action="store_true",
        help="v12: adapt each agent's energy/comfort tradeoff online from its own "
             "comfort error. Off => identical to v10.")
    parser.add_argument("--c_lr", type=float, default=0.02,
        help="v12: learning rate for the per-agent energy-weight integral control.")
    parser.add_argument("--energy_w_min", type=float, default=0.0,
        help="v12: lower bound on a per-agent adapted energy weight.")
    parser.add_argument("--energy_w_max", type=float, default=1.5,
        help="v12: upper bound on a per-agent adapted energy weight.")
    parser.add_argument("--couple", action="store_true",
        help="v14: inter-zone thermal coupling -- each agent folds a learned "
             "conductance against neighbour temps into its prediction. zone mode only.")
    parser.add_argument("--couple_k", type=float, default=0.12,
        help="v14: initial per-agent conductance (per-step gain on T_nb - T_self).")
    parser.add_argument("--no_couple_learn", action="store_true",
        help="v14: freeze the conductance at --couple_k (no online adaptation).")
    parser.add_argument("--couple_lr", type=float, default=0.01,
        help="v14: learning rate for the online conductance update.")
    parser.add_argument("--couple_k_max", type=float, default=0.6,
        help="v14: upper bound on the learned conductance.")
    parser.add_argument("--floor_power_scale", type=float, default=1.0,
        help="Multiplier applied to the per-floor obs values. Sinergym meters "
             "report Joules/timestep, so pass 0.0011111 (=1/900s) to display kW. "
             "The coincidence factor is scale-invariant (unaffected by this).")
    parser.add_argument("--pB_prior_scale", type=float, default=2.0)
    parser.add_argument("--epistemic_weight", type=float, default=0.2)
    parser.add_argument("--unocc_gate", type=float, default=0.1)
    parser.add_argument("--deadband_weight", type=float, default=4.0)
    parser.add_argument("--freeze_B", action="store_true",
        help="disable online Dirichlet learning of B_T (run with the prior "
             "or with whatever a loaded checkpoint contains)")
    parser.add_argument("--override", choices=["safety", "aggressive"], default="safety")
    parser.add_argument("--device", default="auto",
        help="auto | cuda | cuda:N | cpu")
    parser.add_argument("--dtype", choices=["float", "double"], default="float")
    parser.add_argument("--checkpoint", default=None, metavar="PATH")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    checkpoint = args.checkpoint
    if checkpoint is None and not args.no_save:
        struct_tag = "struct" if args.structural else "nostruct"
        checkpoint = f"pymdp_office_v10_{args.mode}_{struct_tag}_{args.weather}.pkl"

    run_simulation(weather=args.weather, episodes=args.episodes,
                   mode=args.mode, structural=args.structural,
                   comfort_weight=args.comfort_weight,
                   energy_weight=args.energy_weight,
                   tou_weight=args.tou_weight,
                   congestion_weight=args.congestion_weight,
                   action_temp=args.action_temp, cong_alpha=args.cong_alpha,
                   learn_C=args.learn_C, c_lr=args.c_lr,
                   energy_w_min=args.energy_w_min, energy_w_max=args.energy_w_max,
                   couple=args.couple, couple_k=args.couple_k,
                   couple_learn=not args.no_couple_learn,
                   couple_lr=args.couple_lr, couple_k_max=args.couple_k_max,
                   policy_len=args.policy_len, lr_pB=args.lr_pB,
                   pB_prior_scale=args.pB_prior_scale,
                   epistemic_weight=args.epistemic_weight,
                   unocc_gate=args.unocc_gate,
                   deadband_weight=args.deadband_weight,
                   freeze_B=args.freeze_B,
                   override=args.override, device=args.device,
                   dtype=args.dtype,
                   floor_power_idx=args.floor_power_idx,
                   floor_power_scale=args.floor_power_scale,
                   checkpoint=checkpoint if not args.no_save else None)