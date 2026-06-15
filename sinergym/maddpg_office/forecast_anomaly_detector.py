"""
forecast_anomaly_detector.py
============================
Action-conditioned, forecast-based anomaly detector for the multi-agent
Sinergym OfficeMedium building (15 occupied + 3 plenum zones, 92-d obs).
 
Core idea
---------
Train a small recurrent forecaster on a CLEAN year of operation. At
every step t, the forecaster predicts the 15 occupied-zone temperatures
at t+1, given (obs_t, setpoints_t). The setpoints in obs[60:90] ARE
MADDPG's action (Sinergym writes them back into the next obs), so the
forecaster is implicitly action-conditioned without us having to plumb
the action vector separately.
 
At inference, residual r_t = T_actual_{t+1} - T_pred_{t+1} is computed
per zone. A calendar-conditioned σ (per zone, per (month, hour-of-day))
normalizes that residual into a z-score. Persistent multi-zone
divergence → hvac_fault. Persistent single-zone divergence → sensor_drift.
 
Why this is better than the observation-only ensemble
-----------------------------------------------------
The biggest source of false positives in the existing detector is that
MADDPG's aggressive control swings inflate the σ of every marginal
baseline. Once you condition the prediction on the action MADDPG just
took, those swings stop being "anomalous-looking" — the forecaster
expects them. The residual distribution becomes much tighter, so the
threshold can be much tighter, so you get higher precision AND higher
recall for the events that actually break physics.
 
Why one model catches both hvac_fault and sensor_drift
------------------------------------------------------
* hvac_fault: setpoint says "cool to 24°C", actual physics fights the
  fault and the zone overshoots → forecaster predicts trajectory
  toward setpoint, actual diverges → large residual, MANY zones
  simultaneously.
* sensor_drift: drift adds bias to T_actual_{t+1} but NOT to the
  forecaster's input at step t (because the forecaster predicted from
  the already-drifted obs at t — so prediction inherits the drift in
  the current state but cannot absorb the additional walk added in the
  next step). The fingerprint is a one-sided residual on a SINGLE zone,
  ramping up smoothly. (Note: a slow drift is harder than an abrupt
  one — we compensate with persistence and a CUSUM-style alert on the
  smoothed residual.)
* occ_spike: occupancy is fed as input. Faking occ=1 in unoccupied
  hours makes the forecaster predict heat from internal gains; real
  physics doesn't have those gains, so the actual is cooler than
  predicted → residual on many zones. Detected as a "structural"
  alarm.
* weather_shock: outdoor temp is fed as input. The shock value enters
  the forecaster's prediction (it expects a colder/hotter day), but
  the real physics responds to the true outdoor temp underneath, so
  there's a residual. Less reliable than for hvac_fault because the
  forecaster compensates, but still detectable when shock is large.
 
The user said they care primarily about hvac_fault and sensor_drift.
This detector is built to optimize for those two while keeping the
other two as "free" bonus signals.
 
Interface
---------
This is a drop-in replacement for SeasonalEnsembleDetector in the
existing pipeline:
  - `fit(obs_seq, info_seq=None, action_seq=None)` — pretraining on
    a clean rollout. Internally trains the GRU and fits per-(month,
    hour) residual baselines.
  - `update(obs, info, action_summary) -> Optional[Alert]`
  - `reset_episode()`
  - `summary() -> dict`
  - `is_fitted: bool`
  - `from_config(cfg)` classmethod for parity with
    SeasonalEnsembleDetector.from_config
  - `save(path)` / `load(path)` classmethod (pickle format,
     `version="forecast_v1"`)
 
The Alert dataclass has the same fields as anomaly_detector.Alert so
downstream code in wellbeing_main.py doesn't change.
"""
 
from __future__ import annotations
 
import math
import pickle
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
 
import numpy as np
 
try:
    import torch
    import torch.nn as nn
    _HAVE_TORCH = True
except ImportError:
    _HAVE_TORCH = False
 
 
# ---------------------------------------------------------------------------
# Alert — matches anomaly_detector.Alert field-for-field
# ---------------------------------------------------------------------------
 
@dataclass
class Alert:
    step: int
    message: str
    kind_guess: str = "unknown"
    sources: List[str] = field(default_factory=list)
    severity: float = 1.0
    zone_idx: Optional[int] = None
    detector_name: str = "forecast"
 
    def __bool__(self) -> bool:
        return True
 
 
# ---------------------------------------------------------------------------
# Obs slicing helpers
# ---------------------------------------------------------------------------
# These mirror the layout in office_medium_multiagent_config.ObsIndex. We
# duplicate them here as constants so the file is self-contained and so a
# wrong cfg can't silently corrupt the forecaster.
 
_IDX_MONTH       = 0
_IDX_DAY         = 1
_IDX_HOUR        = 2
_IDX_OUT_TEMP    = 3
_IDX_OUT_HUM     = 4
_IDX_WIND_SPEED  = 5
_IDX_WIND_DIR    = 6
_IDX_DIFF_SOLAR  = 7
_IDX_DIRECT_SOLAR = 8
_ZONE_TEMP_START  = 9    # 18 zones (15 occ + 3 plenums)
_ZONE_TEMP_END    = 27
_ZONE_HUM_START   = 27
_ZONE_HUM_END     = 45
_OCC_START        = 45   # 15 occupied zones
_OCC_END          = 60
_SETPOINTS_START  = 60   # 30 values, paired htg/clg per occupied zone
_SETPOINTS_END    = 90
_HVAC_DEMAND      = 90
_METER            = 91
 
# 15 occupied zones live at obs indices [9, 10, 11, 15, 16, ..., 26],
# skipping the 3 plenums at [12, 13, 14].
_OCCUPIED_TEMP_IDX = [9, 10, 11, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26]
_N_OCCUPIED = len(_OCCUPIED_TEMP_IDX)  # 15
 
# Floor partition (3 floors × 5 occupied zones each), expressed as
# positions within OCCUPIED_TEMP_IDX. The 15 occupied zones group as
# 1 core + 4 perimeter per floor; see config ZONE_FLOOR. Used to
# average per-zone setpoints into 3 floor-level setpoint pairs (htg,
# clg) instead of 15 — the controller drives each floor more or less
# coherently, so collapsing the setpoints by floor cuts feature noise
# without losing predictive signal.
_FLOOR_OF_OCC_POS = [0, 1, 2, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2]
_N_FLOORS = 3
_FLOOR_GROUPS = [[i for i, f in enumerate(_FLOOR_OF_OCC_POS) if f == g]
                 for g in range(_N_FLOORS)]
 
 
# Scaling constants (shared by state + action builders so train and
# inference agree). Setpoint ranges: HTG ~[15,22], CLG ~[24,30].
_TEMP_CENTER, _TEMP_SPAN = 22.0, 5.0
_HTG_CENTER, _HTG_SPAN   = 18.5, 3.5
_CLG_CENTER, _CLG_SPAN   = 27.0, 3.0
_OUT_CENTER, _OUT_SPAN   = 10.0, 15.0
_OCC_SCALE               = 10.0      # per-zone people count → O(1)
_DEMAND_SCALE            = 12.0      # log1p(demand) / this → O(1)
 
 
def _hour_of(obs: np.ndarray) -> float:
    """Hour-of-day in 0..24, robust to raw (0-23) or normalized (0-1) obs."""
    h = float(obs[_IDX_HOUR])
    if h <= 1.5:
        h *= 24.0
    return h
 
 
def _build_state_features(obs: np.ndarray) -> np.ndarray:
    """STATE features for one step — the trajectory the GRU consumes.
 
    Length _STATE_DIM (65):
      [0..14]   15 occupied zone temperatures           (°C, scaled)
      [15..29]  15 per-zone HEATING setpoints           (°C, scaled)
      [30..44]  15 per-zone COOLING setpoints           (°C, scaled)
      [45..59]  15 per-zone occupancy counts            (people, scaled)
      [60]      outdoor temperature                     (°C, scaled)
      [61]      diffuse solar radiation
      [62]      direct solar radiation
      [63]      hour-of-day  sin
      [64]      hour-of-day  cos
 
    Key change vs the old builder: setpoints are kept PER-ZONE (not
    averaged to 3 floor means). The per-zone variation IS the signal
    that predicts the coupling / flapping drops — averaging it away is
    exactly what made those drops look anomalous. Occupancy (internal
    gains) and a cyclical time encoding are added too; both were already
    in the obs but unseen by the forecaster.
    """
    out = np.empty(_STATE_DIM, dtype=np.float32)
 
    # Zone temperatures (15)
    for i, zi in enumerate(_OCCUPIED_TEMP_IDX):
        out[i] = (float(obs[zi]) - _TEMP_CENTER) / _TEMP_SPAN
 
    # Per-zone setpoints (15 htg + 15 clg). The setpoint block is 30
    # values interleaved (htg, clg) per occupied zone.
    sps = obs[_SETPOINTS_START:_SETPOINTS_END]
    htg = sps[0::2]
    clg = sps[1::2]
    out[15:30] = (htg.astype(np.float32) - _HTG_CENTER) / _HTG_SPAN
    out[30:45] = (clg.astype(np.float32) - _CLG_CENTER) / _CLG_SPAN
 
    # Per-zone occupancy (15)
    occ = obs[_OCC_START:_OCC_END].astype(np.float32)
    out[45:60] = occ / _OCC_SCALE
 
    # Outdoor temperature + solar
    out[60] = (float(obs[_IDX_OUT_TEMP]) - _OUT_CENTER) / _OUT_SPAN
    out[61] = float(obs[_IDX_DIFF_SOLAR])   / 500.0
    out[62] = float(obs[_IDX_DIRECT_SOLAR]) / 1000.0
 
    # Cyclical hour
    h = _hour_of(obs)
    out[63] = math.sin(2.0 * math.pi * h / 24.0)
    out[64] = math.cos(2.0 * math.pi * h / 24.0)
 
    return out
 
 
def _build_action_features(obs: np.ndarray) -> np.ndarray:
    """ACTION / control features for one step — the command being applied
    at THIS step, used to condition the predicted ΔT into this step.
 
    Length _ACTION_DIM (34):
      [0..14]   15 per-zone COOLING setpoints           (°C, scaled)
      [15..29]  15 per-zone HEATING setpoints           (°C, scaled)
      [30..32]  3 per-floor MINIMUM cooling setpoint    (°C, scaled)
                 — explicit coupling handle: the lowest clg on a floor
                   tells the model the shared loop is being driven cold
                   for SOME zone, even if this zone's own clg is high.
      [33]      current whole-building HVAC demand       (log, scaled)
                 — "how hard the system is working right now". This is
                   co-determined with the step's temperatures, so it lets
                   the forecaster EXPECT a coupling/duty-cycle drop. A
                   sensor drift adds bias unexplained by demand, so its
                   residual survives; hvac_fault is still caught by the
                   separate demand-ratio channel.
    """
    out = np.empty(_ACTION_DIM, dtype=np.float32)
    sps = obs[_SETPOINTS_START:_SETPOINTS_END]
    htg = sps[0::2]
    clg = sps[1::2]
    out[0:15]  = (clg.astype(np.float32) - _CLG_CENTER) / _CLG_SPAN
    out[15:30] = (htg.astype(np.float32) - _HTG_CENTER) / _HTG_SPAN
 
    # Per-floor minimum cooling setpoint (coupling handle)
    for f, group in enumerate(_FLOOR_GROUPS):
        floor_min_clg = float(np.min([clg[i] for i in group]))
        out[30 + f] = (floor_min_clg - _CLG_CENTER) / _CLG_SPAN
 
    # Current HVAC demand (log-scaled; demand ≥ 0)
    dem = max(float(obs[_HVAC_DEMAND]), 0.0)
    out[33] = math.log1p(dem) / _DEMAND_SCALE
 
    return out
 
 
_STATE_DIM = 65
_ACTION_DIM = 34
_SEQ_LEN = 8         # 2 hours of history at 15-min steps
_HIDDEN = 512       # wider GRU 
 
 
# ---------------------------------------------------------------------------
# Forecaster model
# ---------------------------------------------------------------------------
 
if _HAVE_TORCH:
 
    class _ForecastGRU(nn.Module):
        """GRU state-encoder + action pathway → 15 zone ΔT next-step.
 
        Two inputs:
          * state_seq (B, T, _STATE_DIM): the trajectory window ending at
            t-1 — temps, per-zone setpoints, occupancy, weather, time.
          * action_vec (B, _ACTION_DIM): the control applied at step t —
            per-zone setpoints, per-floor min cooling SP, current HVAC
            demand. This is the thing that CAUSES the predicted ΔT.
 
        The GRU summarizes where the zones were heading; the action
        pathway tells the model what command is being applied right now.
        Concatenating them before the head makes the prediction genuinely
        action-conditioned, so a coupling / flapping / duty-cycle drop is
        EXPECTED (its residual collapses) while a sensor drift — bias not
        explained by any control input — still produces a residual.
 
        Outputs are DELTAS from the current zone temperatures, in natural
        °C. Predicting the change (not the absolute temp) keeps the
        residual a true physics-divergence signal.
        """
 
        def __init__(self, state_dim: int = _STATE_DIM,
                     action_dim: int = _ACTION_DIM,
                     hidden_size: int = _HIDDEN,
                     n_targets: int = _N_OCCUPIED,
                     dropout: float = 0.1):
            super().__init__()
            # Per-step LayerNorm on the heterogeneous state channels so no
            # single channel (by raw variance) dominates the hidden state.
            self.in_norm = nn.LayerNorm(state_dim)
            self.gru = nn.GRU(
                input_size=state_dim,
                hidden_size=hidden_size,
                num_layers=2,
                batch_first=True,
                dropout=dropout,
            )
            # Dedicated action pathway: normalize then encode to hidden//2.
            act_hidden = hidden_size // 2
            self.act_norm = nn.LayerNorm(action_dim)
            self.act_mlp = nn.Sequential(
                nn.Linear(action_dim, act_hidden),
                nn.GELU(),
                nn.Dropout(dropout),
            )
            # Head consumes [gru_last_hidden ; action_encoding].
            self.head = nn.Sequential(
                nn.Linear(hidden_size + act_hidden, hidden_size),
                nn.GELU(),
                nn.Dropout(dropout),
                nn.Linear(hidden_size, n_targets),
            )
            # Start near "predict zero delta".
            nn.init.zeros_(self.head[-1].weight)
            nn.init.zeros_(self.head[-1].bias)
 
        def forward(self, state_seq, action_vec):
            # state_seq: (B, T, state_dim) ; action_vec: (B, action_dim)
            s = self.in_norm(state_seq)
            h, _ = self.gru(s)
            h_last = h[:, -1, :]                       # (B, hidden)
            a = self.act_mlp(self.act_norm(action_vec))  # (B, hidden//2)
            return self.head(torch.cat([h_last, a], dim=-1))  # (B, n_targets)
else:
    _ForecastGRU = None  # type: ignore
 
 
# ---------------------------------------------------------------------------
# Calendar-conditioned per-zone residual normalization
# ---------------------------------------------------------------------------
 
class _ResidualNormalizer:
    """Per-zone, per-(month, hour-of-day) σ table for residuals.
 
    Builds a 12 × 24 × n_zones grid of robust σ estimates from the
    clean rollout's forecaster residuals. At inference, look up the σ
    for the current (month, hour) and divide.
 
    Why per-(month, hour) and not just per-zone? Because forecaster
    error is heteroskedastic: the GRU is most uncertain at sunrise/
    sunset transitions and on the coldest/hottest days. A flat σ would
    either be tight (firing on transitions) or loose (missing real
    events). Conditioning on (month, hour) flattens this out.
    """
 
    def __init__(self, n_zones: int = _N_OCCUPIED, std_floor: float = 0.10):
        self.n_zones = n_zones
        self.std_floor = std_floor
        # Filled by fit():
        self.sigma_table: Optional[np.ndarray] = None  # (12, 24, n_zones)
        self.mean_table:  Optional[np.ndarray] = None  # (12, 24, n_zones)
        # Empirical per-(month, hour, zone) σ of the DIFFERENTIAL
        # channel (residual minus cross-zone median). Measured directly
        # from clean data rather than assumed as 0.7× of the raw σ —
        # this is critical for precision because the actual
        # cross-zone correlation in the building can be much higher
        # than the heuristic factor assumes.
        self.diff_sigma_table: Optional[np.ndarray] = None  # (12, 24, n_zones)
        # Empirical per-cell σ of the COMMON-MODE channel itself
        # (median across zones of bias-corrected residuals). The
        # common mode has its own variance that depends on how
        # correlated zone-level forecast noise is.
        self.common_sigma_table: Optional[np.ndarray] = None  # (12, 24)
        # Online tracking (per zone, not per cell — too sparse to track
        # all 288 cells online).
        self._online_var = np.ones(n_zones, dtype=np.float64)
        self._online_count = 0
 
    @staticmethod
    def _robust_std(x: np.ndarray) -> float:
        """MAD-based σ estimate. Robust to a few residual outliers."""
        if x.size < 4:
            return 1.0
        mad = float(np.median(np.abs(x - np.median(x))))
        return 1.4826 * mad
 
    def fit(self, residuals: np.ndarray, months: np.ndarray, hours: np.ndarray):
        """Build the (12, 24, n_zones) σ + μ table from clean residuals.
 
        residuals : (N, n_zones)
        months    : (N,) ints in 1..12
        hours     : (N,) ints in 0..23
        """
        n = residuals.shape[0]
        assert residuals.shape[1] == self.n_zones, \
            f"expected {self.n_zones} zones, got {residuals.shape[1]}"
 
        # First, compute a per-zone GLOBAL σ from the full residual
        # pool — this is the principled fallback for any (month, hour)
        # cell that doesn't have enough samples for its own estimate.
        # Without this, sparse cells silently inflate to σ=1.0, which
        # blinds the detector to faults at those times.
        global_sigma = np.array([
            max(self._robust_std(residuals[:, z]), self.std_floor)
            for z in range(self.n_zones)
        ], dtype=np.float64)
 
        # Initialise every cell to the global σ — this is the floor that
        # gets overwritten only where we have enough samples to estimate
        # something tighter.
        sigma = np.tile(global_sigma[None, None, :], (12, 24, 1))
        mean  = np.zeros((12, 24, self.n_zones), dtype=np.float64)
        filled = np.zeros((12, 24), dtype=bool)
 
        # Normalize month/hour inputs
        m_int = np.clip(months.astype(int) - 1, 0, 11)   # 0..11
        h_int = np.clip(hours.astype(int),       0, 23)
 
        # Per-cell MAD over the zone vector
        MIN_CELL_SAMPLES = 8   # need enough to make MAD trustworthy
        for m in range(12):
            for h in range(24):
                mask = (m_int == m) & (h_int == h)
                if mask.sum() < MIN_CELL_SAMPLES:
                    continue   # leave cell at the global-σ default
                cell = residuals[mask]
                for z in range(self.n_zones):
                    s = self._robust_std(cell[:, z])
                    sigma[m, h, z] = max(s, self.std_floor)
                    mean[m, h, z] = float(np.median(cell[:, z]))
                filled[m, h] = True
 
        # For partially-covered months (some hours filled, others not),
        # the unfilled hours get the row mean of FILLED hours in that
        # month — preserves the diurnal shape where we can.
        for m in range(12):
            if filled[m].any() and not filled[m].all():
                row_mean = sigma[m, filled[m]].mean(axis=0)
                for h in range(24):
                    if not filled[m, h]:
                        sigma[m, h] = row_mean
        sigma = np.maximum(sigma, self.std_floor)
 
        self.sigma_table = sigma
        self.mean_table  = mean
 
        # ── Now compute empirical differential & common-mode σ tables ──
        # The "differential" σ at (month, hour, zone) is the σ of
        # (residual_z - mu_z - median_across_zones(residual - mu)). This
        # is the EXACT noise floor the differential channel sees during
        # inference, measured on real clean data instead of assumed
        # from a heuristic correlation factor. Two zones being more
        # correlated in clean data means the median absorbs more of
        # their joint variance, so the differential σ shrinks — which
        # is the right thing to want from a precision standpoint.
        bias_corrected = residuals - mean[m_int, h_int, :]   # (N, n_zones)
        common_per_step = np.median(bias_corrected, axis=1)   # (N,)
        diff_per_step = (bias_corrected
                         - common_per_step[:, None])           # (N, n_zones)
 
        diff_sigma = np.tile(global_sigma[None, None, :] * 0.7,
                             (12, 24, 1))   # safe fallback
        common_sigma = np.full((12, 24), 0.5, dtype=np.float64)
        global_common_sigma = max(self._robust_std(common_per_step),
                                  self.std_floor)
        common_sigma[:] = global_common_sigma
 
        for m in range(12):
            for h in range(24):
                mask = (m_int == m) & (h_int == h)
                if mask.sum() < MIN_CELL_SAMPLES:
                    continue
                # Per-zone σ of the differential channel in this cell
                cell_diff = diff_per_step[mask]
                for z in range(self.n_zones):
                    s = self._robust_std(cell_diff[:, z])
                    diff_sigma[m, h, z] = max(s, self.std_floor)
                # σ of the common-mode signal in this cell
                cell_common = common_per_step[mask]
                common_sigma[m, h] = max(self._robust_std(cell_common),
                                          self.std_floor)
 
        # Same row-fill for unfilled hours
        for m in range(12):
            if filled[m].any() and not filled[m].all():
                row_d = diff_sigma[m, filled[m]].mean(axis=0)
                row_c = float(common_sigma[m, filled[m]].mean())
                for h in range(24):
                    if not filled[m, h]:
                        diff_sigma[m, h] = row_d
                        common_sigma[m, h] = row_c
 
        self.diff_sigma_table = np.maximum(diff_sigma, self.std_floor)
        self.common_sigma_table = np.maximum(common_sigma, self.std_floor)
 
        # Initialise online variance from the full pool, per zone
        for z in range(self.n_zones):
            mad = self._robust_std(residuals[:, z])
            self._online_var[z] = max(mad, self.std_floor) ** 2
        self._online_count = n
 
    def lookup(self, month: float, hour: float) -> Tuple[np.ndarray, np.ndarray]:
        """Return (mean, sigma) vectors of length n_zones for this
        (month, hour) cell. Falls back to ones if not fitted.
        """
        if self.sigma_table is None:
            return (np.zeros(self.n_zones), np.ones(self.n_zones))
        if hour <= 1.5: hour *= 24.0
        if month <= 1.5 and month >= 0.0: month = 1.0 + month * 11.0
        m = int(np.clip(int(month) - 1, 0, 11))
        h = int(np.clip(int(hour),     0, 23))
        return self.mean_table[m, h], self.sigma_table[m, h]
 
    def lookup_diff_common(self, month: float,
                           hour: float) -> Tuple[np.ndarray, float]:
        """Return (diff_sigma_vec, common_sigma_scalar) for this cell.
 
        Used by the detector's differential / common-mode channels.
        Falls back to defaults if the new tables aren't fitted (e.g.
        loading an older pickle).
        """
        if hour <= 1.5: hour *= 24.0
        if month <= 1.5 and month >= 0.0: month = 1.0 + month * 11.0
        m = int(np.clip(int(month) - 1, 0, 11))
        h = int(np.clip(int(hour),     0, 23))
        if self.diff_sigma_table is None or self.common_sigma_table is None:
            # Fallback: use 0.7× of sigma_table for diff, mean/2 for common
            if self.sigma_table is None:
                return (np.ones(self.n_zones), 0.5)
            return (self.sigma_table[m, h] * 0.7,
                    float(self.sigma_table[m, h].mean()) / 2.0)
        return (self.diff_sigma_table[m, h],
                float(self.common_sigma_table[m, h]))
 
 
# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------
 
class ForecastAnomalyDetector:
    """Forecast-residual anomaly detector.
 
    Public API mirrors SeasonalEnsembleDetector so it slots into
    wellbeing_main.py without changes.
 
    Parameters
    ----------
    n_zones, n_occupied, occupied_zone_indices :
        Standard config — see office_medium_multiagent_config.
 
    seq_len :
        Forecaster's input window (default 8 = 2 hours).
    hidden_size :
        GRU hidden size.
    epochs :
        Training epochs in fit(). Default 40; the model converges fast
        because the targets are deltas with near-zero mean.
    batch_size :
        Training batch size.
    lr :
        Adam learning rate.
    k_sigma :
        z-score threshold per zone.
    hvac_zones_required :
        Minimum number of zones simultaneously breaching k_sigma to
        declare an hvac_fault. Default 5 = 1/3 of zones; tuned to
        require enough agreement to be precise.
    drift_persist :
        Consecutive steps a SINGLE-zone breach must persist before
        declaring sensor_drift.
    fault_persist :
        Consecutive steps the multi-zone breach must persist before
        declaring hvac_fault. Two-step persistence keeps fast onset
        but eliminates single-step noise.
    cooldown :
        After an alert fires, suppress duplicates for this many steps.
    drift_min_signed_frac :
        Fraction of the persist window where the residual must keep
        the SAME sign before a single-zone breach is called drift.
        Sensor drift is a one-sided ramp — random forecast noise
        flips sign. Default 0.85.
    """
 
    # Pickle version stamp recognized by the wellbeing_main loader.
    # v7: action-conditioned forecaster (per-zone setpoints + occupancy +
    # current-action pathway). Architecture differs from v6 — old
    # checkpoints will fail the version check and must be retrained.
    # v8: weather-conditioned demand + level baselines (outdoor-temp
    # buckets) — fixes the clean-year cold-tail FPs. Refit required.
    PICKLE_VERSION = "forecast_v8"
 
    def __init__(self,
                 n_zones: int = 18,
                 n_occupied: int = 15,
                 occupied_zone_indices: Optional[List[int]] = None,
                 seq_len: int = _SEQ_LEN,
                 hidden_size: int = _HIDDEN,
                 epochs: int = 200,
                 patience: int = 25,
                 batch_size: int = 256,
                 lr: float = 2e-3,
                 weight_decay: float = 1e-5,
                 k_sigma: float = 5.0,
                 hvac_zones_required: int = 5,
                 fault_persist: int = 2,
                 drift_persist: int = 14,
                 cooldown: int = 12,
                 drift_min_signed_frac: float = 0.85,
                 cusum_slack: float = 0.8,
                 cusum_h: float = 20.0,
                 diff_sigma_scale: float = 0.7,
                 common_mode_persist: int = 4,
                 common_mode_h: float = 3.0,
                 mz_fault_zones: int = 2,
                 drift_dominance_ratio: float = 3.0,
                 device: str = "auto",
                 verbose: bool = True):
        if not _HAVE_TORCH:
            raise ImportError(
                "ForecastAnomalyDetector requires PyTorch. "
                "Install with `pip install torch`.")
 
        # Zone layout — fall back to a sensible default matching the
        # OfficeMedium config.
        if occupied_zone_indices is None:
            occupied_zone_indices = [0, 1, 2, 6, 7, 8, 9, 10, 11, 12,
                                     13, 14, 15, 16, 17]
        self.n_zones = n_zones
        self.n_occupied = n_occupied
        self.occupied_zone_indices = list(occupied_zone_indices)
 
        # Forecaster
        self.seq_len = int(seq_len)
        self.hidden_size = int(hidden_size)
        self.epochs = int(epochs)
        self.patience = int(patience)
        self.batch_size = int(batch_size)
        self.lr = float(lr)
        self.weight_decay = float(weight_decay)
        self.device = self._pick_device(device)
 
        # Detection
        self.k_sigma = float(k_sigma)
        self.hvac_zones_required = int(hvac_zones_required)
        self.fault_persist = int(fault_persist)
        self.drift_persist = int(drift_persist)
        self.cooldown = int(cooldown)
        self.drift_min_signed_frac = float(drift_min_signed_frac)
        # Multi-zone adjudication (added after AIF-controller diagnostics):
        #   mz_fault_zones        — >= this many simultaneous drift-style
        #                           candidates fire as a multizone hvac_fault
        #                           instead of being silently suppressed.
        #   drift_dominance_ratio — if the strongest candidate's severity is
        #                           >= ratio x the runner-up, treat it as a
        #                           single-zone drift even with co-candidates
        #                           (a controller compensating a drifted
        #                           sensor drags same-loop neighbors into the
        #                           candidate list; the true drift dominates).
        self._mz_fault_zones = int(mz_fault_zones)
        self._drift_dominance_ratio = float(drift_dominance_ratio)
        self.verbose = bool(verbose)
 
        # Built in fit()
        self.model: Optional[_ForecastGRU] = None
        self.normalizer: _ResidualNormalizer = _ResidualNormalizer(
            n_zones=n_occupied)
        self.is_fitted: bool = False
 
        # Online state — set up in reset_episode()
        self._step_count = 0
        # Rolling buffer of STATE feature vectors (one per step). We keep
        # seq_len+1 so we can form the window ending at t-1 while the
        # current step t is also stored.
        self._state_history: List[np.ndarray] = []
        self._prev_obs: Optional[np.ndarray] = None
        # Rolling buffer of the last seq_len RAW per-zone temperatures —
        # used for printing context when a sensor_drift alert fires, so
        # a postmortem can see what the zone's sensor was reading in the
        # steps leading up to the detection. Length matches seq_len.
        self._temp_history: List[np.ndarray] = []
        # Parallel buffers: per-zone setpoint midpoint (action) and the
        # building-wide HVAC demand, same length, for the same postmortem.
        self._sp_history: List[np.ndarray] = []
        self._dem_history: List[float]     = []
        # Outside temp + diffuse + direct solar (3 floats per step).
        self._wx_history: List[np.ndarray] = []
        # Month/day/hour per step — for boundary/ramp diagnosis.
        self._time_history: List[np.ndarray] = []
        self._cooldown_left = 0
        # Per-zone consecutive breach streaks
        self._streak_pos = np.zeros(n_occupied, dtype=np.int32)
        self._streak_neg = np.zeros(n_occupied, dtype=np.int32)
        # Per-zone residual sign history (for drift_min_signed_frac)
        self._sign_buf: List[np.ndarray] = []
        self._SIGN_BUF_LEN = max(self.drift_persist, 8)
        # Multi-zone streak counter
        self._fault_streak = 0
        # Per-zone CUSUM accumulator — catches slow drifts that
        # individual-step residuals miss. A constant +c bias each step
        # makes raw |z| stay small but the cumulative sum grows without
        # bound. Detection rule: if |cusum_z| (z = cusum / sqrt(N)·σ)
        # exceeds the drift threshold, we have evidence of a sustained
        # one-sided bias.
        self._cusum_pos = np.zeros(n_occupied, dtype=np.float64)
        self._cusum_neg = np.zeros(n_occupied, dtype=np.float64)
        # CUSUM "drift expected per step that is normal" — small value
        # ensures CUSUM resets to 0 under genuinely-zero-mean residuals
        # (otherwise it would slowly accumulate noise to a false alarm).
        self._cusum_slack = float(cusum_slack)
        # CUSUM detection threshold in σ-units. h = 6 means we need a
        # cumulative excursion of 6σ. With slack=0.5σ, a 1.25σ-per-step
        # one-sided bias needs ~8 steps to trigger; pure-noise reset
        # paths almost never cross 6σ.
        self._cusum_h = float(cusum_h)
        # Variance scale for the differential channel after subtracting
        # the cross-zone median. See update() for derivation.
        self._diff_sigma_scale = float(diff_sigma_scale)
        # Common-mode hvac_fault detection: how many consecutive steps
        # of |z_common| ≥ common_mode_h must persist before firing.
        self._common_mode_persist = int(common_mode_persist)
        self._common_mode_h = float(common_mode_h)
        # Per-step counter for common-mode streak
        self._common_streak_pos = 0
        self._common_streak_neg = 0
        # CUSUM on common-mode residual itself (catches gentle building-
        # wide drift that doesn't cross the per-step threshold but
        # accumulates over time). Same slack as per-zone CUSUM, but
        # threshold scales with sqrt(persist) to match the common-mode
        # noise floor.
        self._common_cusum_pos = 0.0
        self._common_cusum_neg = 0.0
        self._common_cusum_h = self._cusum_h   # reuse
        # Diagnostic counters
        self._n_alerts = 0
        self._n_fault_alerts = 0
        self._n_drift_alerts = 0
        self._n_other_alerts = 0
        self._residual_log: List[float] = []  # for summary diagnostic
 
        # ── HVAC-demand-ratio detector state (for hvac_fault) ─────────
        # CRITICAL: the anomaly injector applies hvac_fault by inflating
        # the DEMAND/METER readings by 1/efficiency — it does NOT touch
        # zone temperatures. A temperature forecaster is therefore
        # structurally blind to faults where the controller still holds
        # setpoint (it just burns more energy). So hvac_fault detection
        # MUST watch the demand signal directly.
        #
        # We fit a per-(hour, occupancy-bucket) baseline of EXPECTED
        # log-demand on clean data. At inference, the ratio
        #   actual_demand / expected_demand
        # is ≈ 1.0 on clean data and ≈ 1/η (1.4–2.0) during a fault.
        # This catches EVERY fault regardless of efficiency, because it
        # measures the exact quantity the injector manipulates.
        #
        # Baselines filled in fit():
        # Outdoor-temp bucket edges (quantile-based, learned at fit). HVAC
        # demand and inter-zone temperature spread are both dominated by
        # outdoor ΔT, so BOTH the demand and level baselines condition on
        # this bucket — otherwise the cold tail reads as anomalous against
        # a weather-blind baseline (the clean-year FP root cause).
        self._temp_bucket_edges: Optional[np.ndarray] = None  # (_N_TEMP_BUCKETS-1,)
        self._demand_log_mean: Optional[np.ndarray] = None  # (occ_b, temp_b)
        self._demand_log_std:  Optional[np.ndarray] = None  # (occ_b, temp_b)
        self._demand_global_log_mean = 0.0
        self._demand_global_log_std  = 1.0
        # Detection: log-ratio z-score threshold and persistence.
        self._demand_ratio_k       = 4.0   # σ above expected → suspect
        self._demand_ratio_persist = 3     # consecutive steps to fire
        self._demand_streak = 0
        # CUSUM on the demand log-ratio (catches gentle/mild faults like
        # eff=0.70 that inflate demand only 1.43× — still a strong, but
        # not enormous, signal that accumulates cleanly).
        self._demand_cusum = 0.0
        self._demand_cusum_slack = 1.5
        self._demand_cusum_h     = 6.0
        # ── hvac_fault CONJUNCTION (demand ⊕ comfort-sag) ─────────────
        # The demand ratio alone cannot separate a fault from a clean
        # demand excursion: in clean operation demand_z routinely reaches
        # 2–4 (morning warmup ramp, shoulder-season low-load), which is
        # AS HIGH as a real fault's log(1/η) inflation. The discriminator
        # is DIRECTION of zone movement: a clean ramp drives zones TOWARD
        # setpoint (AWAY from outdoor), while a real efficiency fault
        # cannot hold load so zones sag TOWARD outdoor. We therefore fire
        # hvac_fault only when demand is (mildly) elevated AND several
        # zones are simultaneously sagging toward outdoor — a conjunction
        # that clean ramps and weather swings do not produce.
        self._demand_soft_k      = 1.4   # lower demand bar (significance)
        # Upper bound is PHYSICAL: an efficiency fault inflates demand by
        # at most 1/η_min. We bound the raw demand RATIO (observed /
        # expected = exp(ld − exp_mean)), which — unlike a z-cap — is
        # independent of the cell's σ, so a severe fault in a low-variance
        # cell is never wrongly rejected. η≥0.4 ⇒ ratio ≤ 2.5. Clean ramp
        # spikes and sparse-cell artifacts blow far past this and are
        # rejected as physically-impossible-for-a-fault. A loose z-cap is
        # kept only as a numerical backstop.
        self._fault_ratio_cap    = 2.2   # observed/expected demand ceiling
        # Tightened to 2.1: across both severe (η=0.50) and mild faults the
        # rolling-mean demand_z topped out at ~1.9; clean ramp/heatwave
        # overshoots sit at 2.3–2.5. A real fault simply does not produce a
        # larger demand_z here, so the band's upper edge sits just above
        # the observed fault ceiling. Anything above is a clean spike.
        self._demand_hard_cap    = 2. #changeing from 2.1 to 1.7 removes a lot of FPs but loose few TPs
        # A real fault degrades a meaningfully-loaded plant. A few kW
        # reading a high RATIO against a near-zero baseline cell is a
        # sparse-cell artifact, not a comfort-affecting fault. Floor on
        # absolute rolling demand (W); building-size dependent.
        self._fault_abs_demand_floor = 5000.0
        self._fault_sag_bar      = 1.5   # per-zone outdoor-ward residual z
        self._fault_sag_min_zones = 3    # zones sagging together to count
        self._fault_joint_persist = 3    # consecutive joint steps → fire
        self._fault_joint_streak = 0
        # The residual-based sag is only strong during the temperature
        # ramp (~8 steps); afterwards the held sag produces ~0 residual
        # (the forecaster can't see a held offset). Demand stays elevated
        # for the WHOLE fault, so once a sag has been seen we keep a short
        # memory window during which the sustained demand is allowed to
        # confirm the fault. Clean ramps never open this window (their
        # residual points AWAY from outdoor), so it adds no FP risk.
        self._FAULT_SAG_MEMORY   = 12
        self._fault_sag_recent   = 0
        # Online trailing-demand ring buffer — the fault detector
        # watches the rolling mean, not the raw duty-cycling value.
        self._demand_ring: List[float] = []
 
        # ── Level-differential drift detector state ───────────────────
        # The delta-based forecaster goes BLIND to a sensor drift once
        # the bias stops changing (constant offset → zero delta). To
        # catch held drifts we ALSO track the LEVEL differential:
        #   level_diff_z = zone_temp - cross_zone_median_temp
        # On clean data this has a stable per-(month,hour,zone) baseline
        # (corner zones run warmer, etc.). A drifted sensor pushes its
        # zone's level differential far from baseline and HOLDS it — a
        # persistent signal that doesn't decay. We CUSUM the level-diff
        # z-score per zone; a held drift accumulates without bound.
        self._level_baseline_mean: Optional[np.ndarray] = None  # (temp_b,24,n_occ)
        self._level_baseline_std:  Optional[np.ndarray] = None  # (temp_b,24,n_occ)
        self._level_cusum_pos = np.zeros(n_occupied, dtype=np.float64)
        self._level_cusum_neg = np.zeros(n_occupied, dtype=np.float64)
        self._level_cusum_slack = 2.0
        self._level_cusum_h     = 500.0
        self._level_persist     = 18  # consecutive elevated steps required
        # Per-zone consecutive-elevation streak for the level channel.
        # A real held drift keeps level_z elevated for the entire event
        # (32 steps); a clean weather-driven zone excursion reverts in
        # a few steps. Requiring BOTH a CUSUM crossing AND a sustained
        # elevation streak separates the two without sacrificing the
        # weakest real drift.
        self._level_streak_pos = np.zeros(n_occupied, dtype=np.int32)
        self._level_streak_neg = np.zeros(n_occupied, dtype=np.int32)
        self._level_support_z   = 2.0  # |level_z| ≥ this counts as elevated
        # Two-channel confirmation: a level-CUSUM drift candidate must
        # also have its zone's delta-CUSUM at ≥ this fraction of the
        # delta threshold (independent same-zone evidence). Kills the
        # low-occupancy level-only false positives.
        self._level_confirm_frac = 0.3
        # Symmetric confirmation for the delta channel: a delta-CUSUM
        # drift candidate must have its zone's level-CUSUM at ≥ this
        # fraction of the level threshold. The level CUSUM accumulates
        # much faster (threshold 26 vs the per-step level_z), so a
        # modest fraction is enough independent evidence that the same
        # zone holds a sustained offset, not a one-step glitch.
        self._delta_confirm_frac = 0.25
        # Strong-evidence bypass multiplier: a CUSUM ≥ this × its
        # threshold fires without needing cross-channel confirmation.
        # Transient glitches never sustain a CUSUM this high; a real
        # sustained drift does. Lets the confirm fractions stay strict
        # (good precision) while still catching weak-but-clear drifts.
        self._strong_evidence_mult = 1.8
 
        # ══════════════════════════════════════════════════════════════
        # DIAGNOSTIC LOGGING (for offline TP/FP analysis)
        # ══════════════════════════════════════════════════════════════
        # When enabled, every update() captures a full snapshot of all
        # channel signals into self.diag_log, tagged as TP/FP/clean
        # based on ground-truth `info`. Dump with dump_diagnostics().
        # Disabled by default (zero overhead) — turn on with
        # detector.enable_diagnostics().
        self._diag_enabled = False
        self.diag_log: List[dict] = []
        self._diag_max = 200000   # safety cap on log length
 
    def enable_diagnostics(self, enabled: bool = True) -> None:
        """Turn on per-step diagnostic capture for offline analysis."""
        self._diag_enabled = bool(enabled)
        if enabled:
            self.diag_log = []
 
    # ── Static helpers ────────────────────────────────────────────────
 
    @staticmethod
    def _pick_device(d: str) -> str:
        if d != "auto":
            return d
        try:
            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"
 
    # ── Construction from config (parity with SeasonalEnsembleDetector) ─
 
    @classmethod
    def from_config(cls, cfg, **overrides) -> "ForecastAnomalyDetector":
        """Build with sensible defaults from a multi-agent OfficeMedium
        config module. `**overrides` lets you tweak hyperparams.
        """
        kwargs = dict(
            n_zones=getattr(cfg, "N_ZONES", 18),
            n_occupied=getattr(cfg, "N_OCCUPIED", 15),
            occupied_zone_indices=getattr(cfg, "OCCUPIED_ZONE_INDICES",
                                          [0, 1, 2, 6, 7, 8, 9, 10, 11,
                                           12, 13, 14, 15, 16, 17]),
        )
        kwargs.update(overrides)
        return cls(**kwargs)
 
    # ──────────────────────────────────────────────────────────────────
    # Training
    # ──────────────────────────────────────────────────────────────────
 
    def fit(self,
            obs_seq: np.ndarray,
            info_seq=None,
            action_seq=None) -> None:
        """Train the GRU on a clean rollout and fit the residual normaliser.
 
        Parameters
        ----------
        obs_seq : (N, 92) numpy array of CLEAN observations.
                  Pass exactly what pretrain_seasonal_detector.py collects.
        info_seq, action_seq : ignored (kept for API parity).
 
        The function:
          1. builds a STATE feature matrix and an ACTION feature matrix,
          2. constructs windowed samples where
             state_window = state_feats[i .. i+L-1]   (ends at t-1)
             action_vec   = action_feats[i+L]          (the action at t)
             y            = T_occ_t - T_occ_{t-1}       (ΔT into t),
          3. trains the GRU+action model with early stopping on a held-out
             10% tail,
          4. predicts on the WHOLE training set and computes residuals,
          5. fits the (month, hour, zone) σ table from those residuals.
 
        The key alignment fix vs the old version: the predicted ΔT into
        step t is conditioned on the ACTION applied at step t (not the
        previous step), so flapping/coupling control is no longer a
        guaranteed residual.
        """
        obs_seq = np.asarray(obs_seq, dtype=np.float64)
        if obs_seq.ndim != 2:
            raise ValueError(f"obs_seq must be 2-d, got shape {obs_seq.shape}")
        N = obs_seq.shape[0]
        if N < self.seq_len + 100:
            raise ValueError(f"Need at least {self.seq_len + 100} steps to "
                             f"fit the forecaster; got {N}.")
 
        if self.verbose:
            print(f"  [forecast] Building features from {N} obs steps "
                  f"(state_dim={_STATE_DIM}, action_dim={_ACTION_DIM})…")
        state_feats  = np.stack([_build_state_features(obs_seq[t])
                                 for t in range(N)], axis=0)   # (N, _STATE_DIM)
        action_feats = np.stack([_build_action_features(obs_seq[t])
                                 for t in range(N)], axis=0)   # (N, _ACTION_DIM)
 
        # ── Build windowed training set ───────────────────────────────
        # Sample i predicts ΔT into step t = i+L:
        #   state window = state_feats[i .. i+L-1]   (trajectory to t-1)
        #   action vec   = action_feats[i+L]          (control at t)
        #   target       = temp[i+L] - temp[i+L-1]
        L = self.seq_len
        n_samples = N - L
        if self.verbose:
            print(f"  [forecast] Windowing: seq_len={L}, "
                  f"{n_samples} training samples")
 
        # Pull occupied-zone temperatures from raw obs (not features —
        # we need natural °C for the target).
        zone_temps = np.stack(
            [obs_seq[:, zi] for zi in _OCCUPIED_TEMP_IDX], axis=1
        )   # (N, n_occupied)
 
        # X_state: (n_samples, L, _STATE_DIM) ; X_act: (n_samples, _ACTION_DIM)
        X_state = np.empty((n_samples, L, _STATE_DIM), dtype=np.float32)
        for i in range(n_samples):
            X_state[i] = state_feats[i:i + L]
        X_act = action_feats[L:N].astype(np.float32)            # action at t
        # y: (n_samples, n_occupied) — ΔT into step t = i+L
        y = (zone_temps[L:N] - zone_temps[L - 1:N - 1]).astype(np.float32)
 
        # 90/10 chronological split — last 10% is the val set. This
        # matters: shuffled splits leak adjacent windows.
        n_val = max(int(0.1 * n_samples), 64)
        X_state_tr, X_act_tr, y_tr = X_state[:-n_val], X_act[:-n_val], y[:-n_val]
        X_state_val, X_act_val, y_val = X_state[-n_val:], X_act[-n_val:], y[-n_val:]
 
        # ── Train ─────────────────────────────────────────────────────
        device = torch.device(self.device)
        self.model = _ForecastGRU(
            state_dim=_STATE_DIM,
            action_dim=_ACTION_DIM,
            hidden_size=self.hidden_size,
            n_targets=self.n_occupied,
        ).to(device)
        opt = torch.optim.Adam(self.model.parameters(),
                               lr=self.lr,
                               weight_decay=self.weight_decay)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            opt, T_max=self.epochs)
        # beta=0.03 matches the temp-delta scale (~0.13 °C RMSE target);
        # the old 0.1 was in the near-linear regime for most samples and
        # under-weighted the small but informative deltas.
        loss_fn = nn.SmoothL1Loss(beta=0.03)
 
        X_state_tr_t = torch.from_numpy(X_state_tr).to(device)
        X_act_tr_t   = torch.from_numpy(X_act_tr).to(device)
        y_tr_t       = torch.from_numpy(y_tr).to(device)
        X_state_val_t = torch.from_numpy(X_state_val).to(device)
        X_act_val_t   = torch.from_numpy(X_act_val).to(device)
        y_val_t       = torch.from_numpy(y_val).to(device)
 
        best_val = float("inf")
        best_state = None
        patience = self.patience
        bad_epochs = 0
 
        n_train = X_state_tr_t.shape[0]
        for ep in range(self.epochs):
            self.model.train()
            perm = torch.randperm(n_train, device=device)
            ep_loss = 0.0
            n_batches = 0
            for b0 in range(0, n_train, self.batch_size):
                idx = perm[b0:b0 + self.batch_size]
                sb = X_state_tr_t[idx]
                ab = X_act_tr_t[idx]
                yb = y_tr_t[idx]
                pred = self.model(sb, ab)
                loss = loss_fn(pred, yb)
                opt.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
                opt.step()
                ep_loss += loss.item()
                n_batches += 1
            scheduler.step()
 
            # Validate
            self.model.eval()
            with torch.no_grad():
                # Validate in chunks if huge, but typical N is fine
                val_pred = self.model(X_state_val_t, X_act_val_t)
                val_loss = loss_fn(val_pred, y_val_t).item()
 
            if self.verbose and (ep == 0 or (ep + 1) % 5 == 0
                                 or ep == self.epochs - 1):
                print(f"    epoch {ep+1:3d}/{self.epochs}  "
                      f"train_loss={ep_loss/max(n_batches,1):.5f}  "
                      f"val_loss={val_loss:.5f}")
 
            if val_loss < best_val - 1e-5:
                best_val = val_loss
                best_state = {k: v.detach().cpu().clone()
                              for k, v in self.model.state_dict().items()}
                bad_epochs = 0
            else:
                bad_epochs += 1
                if bad_epochs >= patience:
                    if self.verbose:
                        print(f"    early stop at epoch {ep+1} "
                              f"(no val improvement for {patience})")
                    break
 
        if best_state is not None:
            self.model.load_state_dict(best_state)
        self.model.eval()
 
        # ── Compute residuals on the full training set & fit σ table ──
        # We predict on the whole windowed set (incl val) — this gives
        # us residuals at every step we have a forecast for.
        if self.verbose:
            print(f"  [forecast] Predicting residuals for σ-table fit…")
        with torch.no_grad():
            # Chunk to keep memory bounded
            chunk = 4096
            preds = np.empty((n_samples, self.n_occupied), dtype=np.float32)
            for i in range(0, n_samples, chunk):
                s_chunk = torch.from_numpy(X_state[i:i + chunk]).to(device)
                a_chunk = torch.from_numpy(X_act[i:i + chunk]).to(device)
                preds[i:i + chunk] = self.model(s_chunk, a_chunk).cpu().numpy()
        residuals = y - preds  # (n_samples, n_occupied)
 
        # The residual at training sample i corresponds to the temp
        # delta observed at step (L + i). The (month, hour) bucket for
        # that step is read from obs_seq[L + i].
        target_step_idx = np.arange(L, N)
        months = obs_seq[target_step_idx, _IDX_MONTH]
        hours  = obs_seq[target_step_idx, _IDX_HOUR]
 
        if self.verbose:
            print(f"  [forecast] Fitting per-(month, hour) σ table on "
                  f"{n_samples} residuals…")
        self.normalizer.fit(residuals, months, hours)
 
        if self.verbose:
            sig = self.normalizer.sigma_table
            print(f"  [forecast] σ table built: shape={sig.shape}")
            print(f"             min σ = {sig.min():.3f} °C, "
                  f"median σ = {np.median(sig):.3f} °C, "
                  f"max σ = {sig.max():.3f} °C")
            print(f"             residual RMSE on val: "
                  f"{np.sqrt(((y_val - preds[-n_val:])**2).mean()):.3f} °C")
 
        # ── Fit the HVAC-demand baseline (for hvac_fault detection) ───
        # Demand depends mostly on (hour, how many zones occupied,
        # outdoor-temp bucket). We bucket and store log-demand stats so
        # the inference-time ratio is calibrated. Using log space means
        # the fault's multiplicative 1/η inflation becomes an ADDITIVE
        # offset of log(1/η), which a fixed σ threshold handles cleanly
        # across all demand magnitudes (winter base load ≠ summer).
        # Outdoor-temp bucket edges first — both baselines condition on them.
        self._fit_temp_bucket_edges(obs_seq)
        self._fit_demand_baseline(obs_seq)
        self._fit_level_baseline(obs_seq)
 
        self.is_fitted = True
 
    def _occ_bucket(self, occ_count: float) -> int:
        """Bucket BUILDING-TOTAL occupancy (sum over 15 zones, range
        0..~255 people) into 4 coarse bins. The old thresholds assumed
        0..15 — wrong by ~17×, which mis-binned every daytime step into
        the 'full' bucket and destroyed the demand baseline's
        conditioning. Real thresholds below match observed people counts.
        0 = empty/night, 1 = light, 2 = moderate, 3 = full building."""
        if occ_count <= 2.0:
            return 0
        elif occ_count <= 60.0:
            return 1
        elif occ_count <= 150.0:
            return 2
        return 3
 
    _N_OCC_BUCKETS = 4
    _N_TEMP_BUCKETS = 6
    _DEMAND_WINDOW = 16   # 4-hour trailing mean — collapses duty-cycle
 
    def _fit_temp_bucket_edges(self, obs_seq: np.ndarray) -> None:
        """Learn outdoor-temp bucket edges from the clean rollout via
        quantiles, so every bucket holds a comparable amount of data
        regardless of the weather file. Stored for inference."""
        out = obs_seq[:, _IDX_OUT_TEMP].astype(np.float64)
        qs = np.linspace(0.0, 1.0, self._N_TEMP_BUCKETS + 1)[1:-1]
        edges = np.quantile(out, qs)
        # Guard against degenerate (duplicate) edges if the weather is flat
        edges = np.maximum.accumulate(edges + 1e-6 * np.arange(len(edges)))
        self._temp_bucket_edges = edges.astype(np.float64)
        if self.verbose:
            print(f"  [forecast] Outdoor-temp bucket edges (°C): "
                  f"{np.round(self._temp_bucket_edges, 1)}")
 
    def _temp_bucket(self, out_temp: float) -> int:
        """Map an outdoor temperature to its learned bucket index."""
        if self._temp_bucket_edges is None:
            return 0
        return int(np.searchsorted(self._temp_bucket_edges, float(out_temp),
                                   side="right"))
 
    @staticmethod
    def _rolling_mean_demand(obs_seq: np.ndarray, w: int) -> np.ndarray:
        """Trailing w-step mean of HVAC demand at each step.
 
        The raw per-step demand duty-cycles (HVAC bursts on/off within a
        control step), so a single sample is unpredictable. The trailing
        mean is stable — its coefficient of variation drops ~4× — while
        still scaling by 1/η under a fault. This is THE signal the fault
        detector should watch, not the raw value.
        """
        dem = np.maximum(obs_seq[:, _HVAC_DEMAND].astype(np.float64), 0.0)
        out = np.empty_like(dem)
        csum = np.cumsum(dem)
        for t in range(len(dem)):
            lo = max(0, t - w + 1)
            s = csum[t] - (csum[lo - 1] if lo > 0 else 0.0)
            out[t] = s / (t - lo + 1)
        return out
 
    def _fit_demand_baseline(self, obs_seq: np.ndarray) -> None:
        """Build an (n_occ_buckets, n_temp_buckets) table of clean
        ROLLING-MEAN log-demand stats.
 
        WEATHER CONDITIONING (the clean-year FP fix): demand is dominated
        by outdoor ΔT (heating/cooling load), so we condition on an
        outdoor-temp bucket in addition to occupancy. The previous
        (hour, occ) baseline was weather-blind: any cold stretch sat
        persistently above the hour-averaged median, the CUSUM integrated
        that steady ~2.8σ offset, and it fired a false hvac_fault. It also
        produced jagged per-hour cells (the 0.71→5.09σ jump at an hour
        boundary). Dropping the raw-hour axis in favour of (occ, temp)
        removes BOTH problems: the temp bucket explains the cold-tail
        demand, and there are no per-hour cells to be discontinuous.
 
        A genuine efficiency fault inflates demand multiplicatively by
        log(1/η); against a weather-correct baseline that offset now
        stands clear instead of competing with the cold-day load, so this
        also SHARPENS real hvac_fault detection.
        """
        N = obs_seq.shape[0]
        nO, nT = self._N_OCC_BUCKETS, self._N_TEMP_BUCKETS
        log_dem = np.full((nO, nT), np.nan)
        log_std = np.full((nO, nT), np.nan)
 
        roll = self._rolling_mean_demand(obs_seq, self._DEMAND_WINDOW)
 
        buckets: Dict[Tuple[int, int], List[float]] = {}
        temp_marg: Dict[int, List[float]] = {}   # (temp_b) -> logs, for fallback
        all_logs = []
        for t in range(N):
            rd = roll[t]
            if rd <= 100.0:          # building essentially off (night)
                continue
            ld = math.log(rd)
            all_logs.append(ld)
            occ_count = float(obs_seq[t, _OCC_START:_OCC_END].sum())
            ob = self._occ_bucket(occ_count)
            tb = self._temp_bucket(float(obs_seq[t, _IDX_OUT_TEMP]))
            buckets.setdefault((ob, tb), []).append(ld)
            temp_marg.setdefault(tb, []).append(ld)
 
        if all_logs:
            self._demand_global_log_mean = float(np.median(all_logs))
            self._demand_global_log_std  = max(
                float(1.4826 * np.median(np.abs(
                    np.array(all_logs) - self._demand_global_log_mean))),
                0.05)
        else:
            self._demand_global_log_mean = 0.0
            self._demand_global_log_std  = 1.0
 
        # Per-(temp) marginal — the principled fallback for sparse
        # (occ, temp) cells. Weather is the dominant driver, so even when
        # an occupancy×temp cell is thin, the temp marginal is still
        # weather-correct (far better than the global median).
        temp_mean = np.full(nT, self._demand_global_log_mean)
        temp_std  = np.full(nT, self._demand_global_log_std)
        for tb, vals in temp_marg.items():
            if len(vals) >= 8:
                arr = np.array(vals)
                med = float(np.median(arr))
                temp_mean[tb] = med
                temp_std[tb]  = max(float(1.4826 * np.median(np.abs(arr - med))),
                                    0.05)
 
        for ob in range(nO):
            for tb in range(nT):
                vals = buckets.get((ob, tb), [])
                if len(vals) >= 8:
                    arr = np.array(vals)
                    med = float(np.median(arr))
                    log_dem[ob, tb] = med
                    log_std[ob, tb] = max(
                        float(1.4826 * np.median(np.abs(arr - med))), 0.05)
                else:
                    # Hierarchical fallback: temp marginal → global
                    log_dem[ob, tb] = temp_mean[tb]
                    log_std[ob, tb] = temp_std[tb]
 
        self._demand_log_mean = log_dem
        self._demand_log_std  = log_std
 
        if self.verbose:
            print(f"  [forecast] HVAC-demand baseline built "
                  f"(occ×temp = {nO}×{nT}): "
                  f"global log-demand μ={self._demand_global_log_mean:.2f}, "
                  f"σ={self._demand_global_log_std:.3f}")
            print(f"             hvac_fault conjunction: demand_k={self._demand_soft_k}, "
                  f"sag_bar={self._fault_sag_bar}, sag_zones={self._fault_sag_min_zones}, "
                  f"joint_persist={self._fault_joint_persist}, "
                  f"sag_mem={self._FAULT_SAG_MEMORY}, cusum_h={self._demand_cusum_h}")
 
    def _fit_level_baseline(self, obs_seq: np.ndarray) -> None:
        """Build (n_temp_buckets, 24, n_occupied) baseline of the LEVEL
        differential (zone_temp − cross-zone median temp) on clean data.
 
        This is what lets us catch a sensor drift after its bias stops
        changing: the drifted zone's level differential sits far from
        baseline and stays there, which the delta-residual channel can't
        see.
 
        WEATHER CONDITIONING (the FP fix): the inter-zone spread is driven
        by outdoor ΔT — on a −13 °C day a perimeter zone legitimately sits
        2 °C off the building median, but a (month, hour)-only baseline
        averaged the cold tail with milder days and flagged that normal
        offset. We key on (outdoor-temp bucket, hour, zone) instead of
        (month, hour, zone). We also raise the std floor for this channel:
        inter-zone offsets are inherently noisier than forecast residuals,
        so the 0.10 °C residual floor manufactured huge level z-scores.
        """
        N = obs_seq.shape[0]
        n_occ = self.n_occupied
        nT = self._N_TEMP_BUCKETS
        # Per-step occupied-zone temps and their cross-zone median
        temps = obs_seq[:, _OCCUPIED_TEMP_IDX]              # (N, n_occ)
        med = np.median(temps, axis=1, keepdims=True)        # (N, 1)
        level_diff = temps - med                             # (N, n_occ)
 
        hours = obs_seq[:, _IDX_HOUR]
        h_arr = hours.copy()
        h_arr[h_arr <= 1.5] *= 24.0
        h_int = np.clip(h_arr.astype(int), 0, 23)
        t_int = np.array([self._temp_bucket(float(o))
                          for o in obs_seq[:, _IDX_OUT_TEMP]])
 
        # Inter-zone offsets are noisier than forecast residuals — use a
        # dedicated, larger floor so a normal ±0.3 °C offset isn't a 3σ z.
        LEVEL_STD_FLOOR = 0.4
 
        # Global fallback per zone
        g_mean = np.median(level_diff, axis=0)               # (n_occ,)
        g_std = np.array([
            max(1.4826 * np.median(np.abs(level_diff[:, z] - g_mean[z])),
                LEVEL_STD_FLOOR)
            for z in range(n_occ)])
 
        # Per-(temp_bucket, zone) marginal — fallback for sparse cells,
        # still weather-correct.
        tb_mean = np.tile(g_mean[None, :], (nT, 1))
        tb_std  = np.tile(g_std[None, :], (nT, 1))
        for tb in range(nT):
            mask = (t_int == tb)
            if mask.sum() < 8:
                continue
            cell = level_diff[mask]
            for z in range(n_occ):
                md = float(np.median(cell[:, z]))
                sd = float(1.4826 * np.median(np.abs(cell[:, z] - md)))
                tb_mean[tb, z] = md
                tb_std[tb, z]  = max(sd, LEVEL_STD_FLOOR)
 
        lvl_mean = np.empty((nT, 24, n_occ))
        lvl_std  = np.empty((nT, 24, n_occ))
        for tb in range(nT):
            for h in range(24):
                lvl_mean[tb, h] = tb_mean[tb]   # default = temp marginal
                lvl_std[tb, h]  = tb_std[tb]
                mask = (t_int == tb) & (h_int == h)
                if mask.sum() < 8:
                    continue
                cell = level_diff[mask]
                for z in range(n_occ):
                    md = float(np.median(cell[:, z]))
                    sd = float(1.4826 * np.median(np.abs(cell[:, z] - md)))
                    lvl_mean[tb, h, z] = md
                    lvl_std[tb, h, z]  = max(sd, LEVEL_STD_FLOOR)
 
        self._level_baseline_mean = lvl_mean
        self._level_baseline_std  = lvl_std
        if self.verbose:
            print(f"  [forecast] Level-differential baseline built: "
                  f"σ median={np.median(lvl_std):.3f} °C")
 
    # ──────────────────────────────────────────────────────────────────
    # Online detection
    # ──────────────────────────────────────────────────────────────────
 
    def reset_episode(self) -> None:
        """Reset rolling state between episodes."""
        self._step_count = 0
        self._state_history = []
        self._prev_obs = None
        self._temp_history = []
        self._sp_history = []
        self._dem_history = []
        self._wx_history = []
        self._time_history = []
        self._cooldown_left = 0
        self._streak_pos[:] = 0
        self._streak_neg[:] = 0
        self._sign_buf = []
        self._fault_streak = 0
        self._cusum_pos[:] = 0
        self._cusum_neg[:] = 0
        # Reset common-mode state too
        self._common_streak_pos = 0
        self._common_streak_neg = 0
        self._common_cusum_pos = 0.0
        self._common_cusum_neg = 0.0
        # Reset demand-ratio detector state
        self._demand_streak = 0
        self._demand_cusum = 0.0
        self._demand_ring = []
        self._fault_joint_streak = 0
        self._fault_sag_recent = 0
        # Reset level-differential drift state
        self._level_cusum_pos[:] = 0
        self._level_cusum_neg[:] = 0
        self._level_streak_pos[:] = 0
        self._level_streak_neg[:] = 0
        # Counters are intentionally NOT reset — summary() reports
        # totals across episodes (matches existing detector behaviour).
 
    def update(self,
               obs: np.ndarray,
               info: Optional[dict] = None,
               action_summary=None) -> Optional[Alert]:
        """Process one observation and return an Alert or None.
 
        The action_summary parameter is accepted for API parity but is
        not used — the forecaster reads the action directly from the obs
        (the setpoint block obs[60:90] plus current HVAC demand), which
        IS MADDPG's command, and feeds it through the model's action
        pathway aligned to the step it drives.
        """
        if not self.is_fitted:
            return None
 
        obs = np.asarray(obs, dtype=np.float64)
        self._step_count += 1
 
        # Build the current STATE feature vector and push into the rolling
        # buffer. We keep seq_len+1 entries so the window ending at t-1 is
        # available while the current step t is also stored.
        cur_state = _build_state_features(obs)
        self._state_history.append(cur_state)
        if len(self._state_history) > self.seq_len + 1:
            self._state_history.pop(0)
        # Keep a parallel buffer of raw per-zone temps for drift postmortems.
        cur_temps = np.asarray(
            [float(obs[zi]) for zi in _OCCUPIED_TEMP_IDX],
            dtype=np.float32)
        self._temp_history.append(cur_temps)
        if len(self._temp_history) > self.seq_len:
            self._temp_history.pop(0)
        # Parallel buffers of per-zone setpoint midpoint (the realized
        # controller ACTION for that zone) and building-wide HVAC demand.
        # Used to print alongside the drift temp history so you can see
        # whether the controller was actively driving the zone (HVAC
        # demand high, setpoint moving) or coasting (low demand, flat
        # setpoint) during the period the drift was developing.
        sps_now = obs[_SETPOINTS_START:_SETPOINTS_END]
        # Store the FULL per-zone (htg, clg) pair, not just the midpoint.
        # The postmortem displays both so you can see the individual
        # zone's heating and cooling setpoints — useful when a zone is
        # held tighter or looser than the floor mean (e.g. floor mean
        # clg=23 but this specific zone has clg=25).
        cur_sp_htg = sps_now[0::2].astype(np.float32)
        cur_sp_clg = sps_now[1::2].astype(np.float32)
        # Pack as (n_occ, 2) so existing row[zi_occ] indexing still works,
        # but now yields a (2,) array of (htg, clg) for that zone.
        cur_sp = np.stack([cur_sp_htg, cur_sp_clg], axis=1)
        self._sp_history.append(cur_sp)
        if len(self._sp_history) > self.seq_len:
            self._sp_history.pop(0)
        self._dem_history.append(float(obs[_HVAC_DEMAND]))
        if len(self._dem_history) > self.seq_len:
            self._dem_history.pop(0)
        # Outside temp + solar (diffuse, direct) — context for drift
        # postmortems. Stored as a single (3,) float vector per step.
        self._wx_history.append(np.asarray([
            float(obs[_IDX_OUT_TEMP]),
            float(obs[_IDX_DIFF_SOLAR]),
            float(obs[_IDX_DIRECT_SOLAR]),
        ], dtype=np.float32))
        if len(self._wx_history) > self.seq_len:
            self._wx_history.pop(0)
        # Month/day/hour per step — lets the postmortem flag day/episode
        # boundaries and morning HVAC ramp-on as the cause of a temp jump.
        self._time_history.append(np.asarray([
            float(obs[_IDX_MONTH]),
            float(obs[_IDX_DAY]),
            float(obs[_IDX_HOUR]),
        ], dtype=np.float32))
        if len(self._time_history) > self.seq_len:
            self._time_history.pop(0)
 
        # We can only forecast once we have a full window of states ending
        # at t-1 (so seq_len+1 entries total, including the current step)
        # AND a previous obs (to measure the actual ΔT into t).
        if (len(self._state_history) < self.seq_len + 1
                or self._prev_obs is None):
            self._prev_obs = obs
            return None
 
        # Actual ΔT from previous step to current
        prev_temps = np.array(
            [self._prev_obs[zi] for zi in _OCCUPIED_TEMP_IDX])
        cur_temps  = np.array(
            [obs[zi] for zi in _OCCUPIED_TEMP_IDX])
        actual_delta = (cur_temps - prev_temps).astype(np.float32)
 
        # State window = the seq_len states ending at t-1. After the
        # append above, _state_history ends with the current step t at
        # index -1, so the window is the seq_len entries before it.
        window = self._state_history[-(self.seq_len + 1):-1]   # steps [t-L .. t-1]
        state_x = np.stack(window, axis=0)[None, ...]          # (1, L, state_dim)
        # Action = the control applied at step t (current obs). This is
        # what conditions the predicted ΔT — the alignment fix.
        action_x = _build_action_features(obs)[None, ...]      # (1, action_dim)
        with torch.no_grad():
            s_t = torch.from_numpy(state_x).to(self.device)
            a_t = torch.from_numpy(action_x).to(self.device)
            pred_delta = self.model(s_t, a_t).cpu().numpy()[0]  # (n_occupied,)
 
        residuals = actual_delta - pred_delta   # per-zone (n_occupied,)
        self._residual_log.append(float(np.linalg.norm(residuals)))
 
        # Remember current state for next call
        self._prev_obs = obs
 
        # ── Convert residuals to z-scores using calendar-conditioned σ ──
        month = float(obs[_IDX_MONTH])
        hour  = float(obs[_IDX_HOUR])
        mu, sigma = self.normalizer.lookup(month, hour)
        # FIRST subtract the per-cell mean bias — the forecaster has
        # small systematic biases at certain (month, hour) cells (it's
        # an imperfect finite-data model), and those biases are
        # CORRELATED across zones because the building physics couples
        # them. Without bias correction, every clean 7am step would
        # show, say, +0.1σ residual on all 15 zones simultaneously,
        # and the coherent-direction path would fire as a false hvac
        # fault. With bias correction, residuals are genuinely
        # zero-mean on clean data.
        residuals_corr = residuals - mu       # bias-corrected, per-zone
 
        # ── Decompose into common-mode and differential residuals ────
        # See the design notes above for why this is the structural
        # precision fix. Empirical σ tables (measured on clean data
        # at fit time) are used directly — no heuristic scaling.
        cross_zone_median = float(np.median(residuals_corr))
        diff_resid = residuals_corr - cross_zone_median   # per-zone
 
        # Empirical per-cell σ for both channels, measured on clean
        # data at fit time. This is the key fix vs the heuristic
        # 0.7× factor: real cross-zone correlation in this building
        # may be much higher (MADDPG's centralized critic couples
        # zones), so the empirical diff σ can be 0.3–0.5× of raw σ,
        # not 0.7×. Using the actual measurement keeps thresholds
        # at the right tightness.
        diff_sigma, common_sigma_scalar = \
            self.normalizer.lookup_diff_common(month, hour)
 
        # z-scores: per-zone for differential (used for drift), scalar
        # for common-mode (used for hvac_fault)
        z = diff_resid / np.maximum(diff_sigma, self.normalizer.std_floor)
        z_common = cross_zone_median / max(common_sigma_scalar,
                                            self.normalizer.std_floor)
 
        # ── Diagnostic snapshot (only assembled if logging enabled) ───
        # We capture the full signal state so TPs and FPs can be diffed
        # offline. Built incrementally — demand/level fields filled in
        # below as they're computed; defaults here so every exit path
        # can record a complete record.
        if self._diag_enabled:
            gt_kinds = []
            gt_eff = 1.0
            gt_bias_max = 0.0
            if info:
                gt_kinds = list(info.get("anomaly_kinds", []) or [])
                gt_eff = float(info.get("hvac_efficiency", 1.0))
                _bias = info.get("sensor_drift_bias", None)
                if _bias is not None:
                    try:
                        gt_bias_max = float(np.max(np.abs(_bias)))
                    except Exception:
                        gt_bias_max = 0.0
            snap = {
                "step": self._step_count,
                "month": float(obs[_IDX_MONTH]),
                "hour": float(obs[_IDX_HOUR]),
                "out_temp": float(obs[_IDX_OUT_TEMP]),
                "occ_count": float(obs[_OCC_START:_OCC_END].sum()),
                "hvac_demand": float(obs[_HVAC_DEMAND]),
                # ground truth
                "gt_kinds": gt_kinds,
                "gt_hvac_eff": gt_eff,
                "gt_drift_bias_max": gt_bias_max,
                "gt_anomaly_active": bool(gt_kinds),
                # temperature differential channel
                "cross_zone_median_resid": float(cross_zone_median),
                "z_common": float(z_common),
                "z_diff_max": float(np.max(np.abs(z))),
                "z_diff_argmax": int(np.argmax(np.abs(z))),
                "common_sigma": float(common_sigma_scalar),
                # level channel (filled below)
                "level_z_max": 0.0,
                "level_z_argmax": -1,
                "level_cusum_max": 0.0,
                "level_streak_max": 0,
                # demand channel (filled below)
                "demand_z": 0.0,
                "demand_streak": int(self._demand_streak),
                "demand_cusum": float(self._demand_cusum),
                # common-mode streaks (filled below)
                "common_streak": 0,
                "common_cusum_max": 0.0,
                # outcome (set at exit)
                "alert_fired": False,
                "alert_source": None,
                "alert_kind": None,
                "cooled": bool(self._cooldown_left > 0),
            }
            self._cur_snap = snap
        else:
            self._cur_snap = None
 
        # Cooldown gate (we still update streaks during cooldown so the
        # streak doesn't reset mid-event, but we don't emit alerts).
        cooled = self._cooldown_left > 0
        if cooled:
            self._cooldown_left -= 1
 
        # ══════════════════════════════════════════════════════════════
        # PRIMARY hvac_fault detector: demand ⊕ comfort-sag CONJUNCTION
        # ══════════════════════════════════════════════════════════════
        # A real efficiency fault does TWO things together: it inflates
        # demand (1/η) AND it cannot hold load, so zones sag TOWARD the
        # outdoor temperature. We require BOTH:
        #   (1) demand mildly elevated vs the weather/occupancy baseline, and
        #   (2) several occupied zones simultaneously diverging in the
        #       sign(T_out − T_zone) direction (i.e. moving toward outdoor,
        #       beyond what the action-conditioned forecaster predicted).
        # This conjunction is the discriminator the demand ratio alone
        # lacks: a clean morning warmup ramp ALSO elevates demand, but it
        # drives zones AWAY from outdoor (toward setpoint), so condition
        # (2) fails and it no longer fires. A weather swing moves zones but
        # leaves demand explained by the temp-bucket baseline. Only a true
        # fault trips both at once.
        #
        # The outdoor-ward residual uses the BIAS-CORRECTED, NON-median-
        # subtracted residual (residuals_corr / sigma), so a uniform
        # (common-mode) sag — e.g. a summer fault warming every zone — is
        # preserved rather than cancelled by the cross-zone-median step
        # that the differential/level channels apply.
        if self._demand_log_mean is not None:
            # Push raw demand into the trailing ring, then use its MEAN
            # — never the raw per-step value, which duty-cycles 200×.
            self._demand_ring.append(float(obs[_HVAC_DEMAND]))
            if len(self._demand_ring) > self._DEMAND_WINDOW:
                self._demand_ring.pop(0)
            roll_dem = float(np.mean(self._demand_ring))
            occ_count = float(obs[_OCC_START:_OCC_END].sum())
            # Only run while the building is meaningfully occupied and on
            # (a fault during empty hours affects nobody and the energy
            # signal is tiny); otherwise bleed the accumulators.
            if roll_dem > 100.0 and occ_count >= 5.0:
                ld = math.log(roll_dem)
                ob_idx = self._occ_bucket(occ_count)
                tb_idx = self._temp_bucket(float(obs[_IDX_OUT_TEMP]))
                exp_mean = self._demand_log_mean[ob_idx, tb_idx]
                exp_std  = self._demand_log_std[ob_idx, tb_idx]
                demand_z = (ld - exp_mean) / max(exp_std, 0.05)
                if self._cur_snap is not None:
                    self._cur_snap["demand_z"] = float(demand_z)
 
                # ── Comfort-sag signal: zones moving toward outdoor ──
                # dir_i = sign(T_out − T_zone_i); a fault makes the zone
                # residual share that sign. sag_z>0 ⇒ unexpected movement
                # toward outdoor (fault); sag_z<0 ⇒ toward setpoint (a
                # clean ramp / normal control).
                t_out = float(obs[_IDX_OUT_TEMP])
                direction = np.sign(t_out - cur_temps)        # (n_occ,)
                sag_z = (residuals_corr / np.maximum(
                    sigma, self.normalizer.std_floor)) * direction
                n_sag = int(np.sum(sag_z >= self._fault_sag_bar))
 
                demand_elevated = (
                    demand_z >= self._demand_soft_k
                    and demand_z <= self._demand_hard_cap
                    and math.exp(ld - exp_mean) <= self._fault_ratio_cap
                    and roll_dem >= self._fault_abs_demand_floor)
                sag_present     = n_sag >= self._fault_sag_min_zones
 
                # Sag-memory window: a fresh sag refills it; otherwise it
                # decays. This bridges the held-fault plateau where the
                # residual sag has faded but the demand inflation persists.
                if sag_present:
                    self._fault_sag_recent = self._FAULT_SAG_MEMORY
                elif self._fault_sag_recent > 0:
                    self._fault_sag_recent -= 1
 
                # "joint" = demand elevated WHILE a sag is current or
                # recent. A clean ramp elevates demand but never opens the
                # window (its residual points away from outdoor), so it
                # can't satisfy this.
                joint = demand_elevated and (self._fault_sag_recent > 0)
 
                # Joint streak: consecutive joint steps.
                if joint:
                    self._fault_joint_streak += 1
                else:
                    self._fault_joint_streak = max(
                        0, self._fault_joint_streak - 1)
 
                # Demand CUSUM, GATED by the sag window AND the demand
                # band: it only accumulates while a sag is current/recent
                # AND demand sits in the physically-plausible fault range,
                # so neither a clean ramp (no sag) nor an oversized spike
                # (above the 1/η cap) can drive it across threshold.
                if self._fault_sag_recent > 0 and demand_elevated:
                    self._demand_cusum = max(
                        0.0, self._demand_cusum + demand_z
                        - self._demand_cusum_slack)
                else:
                    self._demand_cusum = max(0.0, self._demand_cusum - 1.0)
 
                if self._cur_snap is not None:
                    self._cur_snap["demand_streak"] = int(self._fault_joint_streak)
                    self._cur_snap["demand_cusum"] = float(self._demand_cusum)
 
                fault = (self._fault_joint_streak >= self._fault_joint_persist
                         or self._demand_cusum >= self._demand_cusum_h)
 
                if not cooled and fault:
                    eff_est = float(np.clip(math.exp(-(ld - exp_mean)),
                                            0.01, 1.0))
                    severity = float(max(
                        demand_z / max(self._demand_soft_k, 1e-6),
                        self._demand_cusum / self._demand_cusum_h,
                        n_sag / float(self.n_occupied)))
                    src = ("forecast_hvac_joint_streak"
                           if self._fault_joint_streak >= self._fault_joint_persist
                           else "forecast_hvac_joint_cusum")
                    alert = Alert(
                        step=self._step_count,
                        message=(f"[hvac_fault] demand {demand_z:+.1f}σ + "
                                 f"{n_sag} zones sagging toward outdoor "
                                 f"(est. efficiency≈{eff_est:.2f}); "
                                 f"sev={severity:.2f}; step={self._step_count} "
                                 f"({src})"),
                        kind_guess="hvac_fault",
                        sources=[src],
                        severity=severity,
                        zone_idx=None,
                        detector_name="forecast",
                    )
                    self._n_alerts += 1
                    self._n_fault_alerts += 1
                    self._cooldown_left = self.cooldown
                    self._fault_joint_streak = 0
                    self._demand_cusum = 0.0
                    self._record_diag(info, fired=True, source=src,
                                      kind="hvac_fault")
                    return alert
            else:
                # HVAC off / building empty: bleed accumulators, no fire.
                self._fault_joint_streak = max(0, self._fault_joint_streak - 1)
                self._demand_cusum = max(0.0, self._demand_cusum - 1.0)
                self._fault_sag_recent = max(0, self._fault_sag_recent - 1)
 
        # ── Per-zone streak update (on DIFFERENTIAL z) ────────────────
        # After common-mode subtraction, these streaks only see
        # zone-specific divergence — exactly what we want for drift.
        signs = np.sign(diff_resid)
        breach_mask = np.abs(z) >= self.k_sigma
        for i in range(self.n_occupied):
            if breach_mask[i]:
                if signs[i] >= 0:
                    self._streak_pos[i] += 1
                    self._streak_neg[i] = 0
                else:
                    self._streak_neg[i] += 1
                    self._streak_pos[i] = 0
            else:
                # Soft decay so a single missed step doesn't reset:
                # halve the streak instead of zeroing.
                self._streak_pos[i] = self._streak_pos[i] // 2
                self._streak_neg[i] = self._streak_neg[i] // 2
 
        # Sign-history buffer (for drift_min_signed_frac)
        self._sign_buf.append(signs.copy())
        if len(self._sign_buf) > self._SIGN_BUF_LEN:
            self._sign_buf.pop(0)
 
        # ── Per-zone CUSUM (on DIFFERENTIAL z, for drift only) ───────
        for i in range(self.n_occupied):
            z_i = z[i]
            self._cusum_pos[i] = max(
                0.0, self._cusum_pos[i] + z_i - self._cusum_slack)
            self._cusum_neg[i] = max(
                0.0, self._cusum_neg[i] - z_i - self._cusum_slack)
 
        # ── Level-differential CUSUM (catches HELD drifts) ────────────
        # The delta channel goes blind once a drift's bias is constant.
        # The LEVEL differential (zone temp − cross-zone median temp)
        # stays elevated for the whole held period, so its CUSUM keeps
        # climbing. This is the channel that catches a +3°C drift that
        # has plateaued.
        if self._level_baseline_mean is not None:
            cur_temps = np.array([obs[zi] for zi in _OCCUPIED_TEMP_IDX])
            cur_med = float(np.median(cur_temps))
            level_diff_now = cur_temps - cur_med            # (n_occ,)
            if hour <= 1.5:
                h_l = hour * 24.0
            else:
                h_l = hour
            ti = self._temp_bucket(float(obs[_IDX_OUT_TEMP]))
            hi = int(np.clip(int(h_l), 0, 23))
            lvl_mean = self._level_baseline_mean[ti, hi]
            lvl_std  = self._level_baseline_std[ti, hi]
            level_z = (level_diff_now - lvl_mean) / np.maximum(
                lvl_std, self.normalizer.std_floor)
            for i in range(self.n_occupied):
                self._level_cusum_pos[i] = max(
                    0.0, self._level_cusum_pos[i] + level_z[i]
                    - self._level_cusum_slack)
                self._level_cusum_neg[i] = max(
                    0.0, self._level_cusum_neg[i] - level_z[i]
                    - self._level_cusum_slack)
                # Consecutive-elevation streak (separates held drift from
                # transient clean excursion)
                if level_z[i] >= self._level_support_z:
                    self._level_streak_pos[i] += 1
                    self._level_streak_neg[i] = 0
                elif level_z[i] <= -self._level_support_z:
                    self._level_streak_neg[i] += 1
                    self._level_streak_pos[i] = 0
                else:
                    # Decay (not hard reset) so a single noisy step
                    # doesn't break a genuine held drift's streak.
                    self._level_streak_pos[i] = self._level_streak_pos[i] // 2
                    self._level_streak_neg[i] = self._level_streak_neg[i] // 2
        else:
            level_z = np.zeros(self.n_occupied)

        # Expose the per-zone level residual vector so the neighbor-residual
        # head can difference it locally (MADDPG-aware, gradient-immune).
        self._cur_level_z_vec = level_z.copy()
 
        # Record level-channel state into the diagnostic snapshot
        if self._cur_snap is not None:
            lz_abs = np.abs(level_z)
            self._cur_snap["level_z_max"] = float(np.max(lz_abs))
            self._cur_snap["level_z_argmax"] = int(np.argmax(lz_abs))
            self._cur_snap["level_cusum_max"] = float(max(
                self._level_cusum_pos.max(), self._level_cusum_neg.max()))
            self._cur_snap["level_streak_max"] = int(max(
                self._level_streak_pos.max(), self._level_streak_neg.max()))
            self._cur_snap["demand_streak"] = int(self._fault_joint_streak)
            self._cur_snap["demand_cusum"] = float(self._demand_cusum)
 
        # ── Common-mode update (for hvac_fault) ──────────────────────
        # Per-step common-mode streak: |z_common| above threshold for
        # several consecutive steps with consistent sign.
        if z_common >= self._common_mode_h:
            self._common_streak_pos += 1
            self._common_streak_neg = 0
        elif z_common <= -self._common_mode_h:
            self._common_streak_neg += 1
            self._common_streak_pos = 0
        else:
            # Soft decay
            self._common_streak_pos = self._common_streak_pos // 2
            self._common_streak_neg = self._common_streak_neg // 2
 
        # Common-mode CUSUM: cumulative one-sided σ excursion of the
        # cross-zone median. Catches gentle building-wide faults that
        # individual steps under k_common but stay biased over time.
        self._common_cusum_pos = max(
            0.0, self._common_cusum_pos + z_common - self._cusum_slack)
        self._common_cusum_neg = max(
            0.0, self._common_cusum_neg - z_common - self._cusum_slack)
 
        # ── HVAC fault: fires on EITHER common-mode path ──────────────
        # Path A — sustained common-mode breach: |z_common| ≥
        #          common_mode_h for ≥ common_mode_persist consecutive
        #          steps. Catches abrupt-to-moderate faults.
        # Path B — common-mode CUSUM crossing: |common_cusum| ≥
        #          common_cusum_h. Catches slow building-wide drift.
        common_streak_pos = self._common_streak_pos >= self._common_mode_persist
        common_streak_neg = self._common_streak_neg >= self._common_mode_persist
        common_cusum_pos  = self._common_cusum_pos >= self._common_cusum_h
        common_cusum_neg  = self._common_cusum_neg >= self._common_cusum_h
        fault_pos = common_streak_pos or common_cusum_pos
        fault_neg = common_streak_neg or common_cusum_neg
 
        if (not cooled and (fault_pos or fault_neg)):
            # Direction: whichever side fired (or whichever has bigger
            # |z_common| if both somehow fire — shouldn't happen in
            # normal data)
            if fault_pos and not fault_neg:
                direction = "hot"
                severity = float(max(
                    abs(z_common) / self._common_mode_h,
                    self._common_cusum_pos / self._common_cusum_h))
                src = ("forecast_common_mode_streak" if common_streak_pos
                       else "forecast_common_mode_cusum")
            elif fault_neg and not fault_pos:
                direction = "cold"
                severity = float(max(
                    abs(z_common) / self._common_mode_h,
                    self._common_cusum_neg / self._common_cusum_h))
                src = ("forecast_common_mode_streak" if common_streak_neg
                       else "forecast_common_mode_cusum")
            else:
                # Both sides — pick the stronger
                if abs(z_common) >= 0:
                    direction = "hot" if z_common > 0 else "cold"
                else:
                    direction = "hot"
                severity = float(abs(z_common) / self._common_mode_h)
                src = "forecast_common_mode_both"
            alert = Alert(
                step=self._step_count,
                message=(f"[hvac_fault] common-mode {direction}; "
                         f"z_common={z_common:+.2f} "
                         f"(thr=±{self._common_mode_h}), "
                         f"cusum=±{max(self._common_cusum_pos, self._common_cusum_neg):.1f}; "
                         f"sev={severity:.2f}; step={self._step_count}"),
                kind_guess="hvac_fault",
                sources=[src],
                severity=severity,
                zone_idx=None,
                detector_name="forecast",
            )
            self._n_alerts += 1
            self._n_fault_alerts += 1
            self._cooldown_left = self.cooldown
            # Reset common-mode state — event is now acknowledged
            self._common_streak_pos = 0
            self._common_streak_neg = 0
            self._common_cusum_pos = 0.0
            self._common_cusum_neg = 0.0
            # ALSO reset per-zone state. A common-mode fault leaves
            # leftover per-zone CUSUMs that would immediately fire as
            # spurious drifts on the next step.
            self._cusum_pos[:] = 0.0
            self._cusum_neg[:] = 0.0
            self._streak_pos[:] = 0
            self._streak_neg[:] = 0
            self._record_diag(info, fired=True, source=src, kind="hvac_fault")
            return alert
 
        # ── Sensor drift: TWO independent paths converge here ─────────
        # Path A — streak-based: a zone breaches k_sigma for at least
        #   drift_persist steps with consistent sign. Catches faster
        #   drifts (1-2°C over an hour).
        # Path B — CUSUM-based: cumulative one-sided σ excursion of the
        #   residual exceeds the CUSUM threshold. Catches slow drifts
        #   (0.3°C over 30 minutes ramped over many hours) that produce
        #   small per-step residuals but a large persistent bias.
        # ── Sensor drift: THREE independent paths converge here ───────
        # Path A — streak-based: a zone breaches k_sigma for at least
        #   drift_persist steps with consistent sign. Catches faster
        #   drifts (1-2°C over an hour).
        # Path B — delta-CUSUM: cumulative one-sided σ excursion of the
        #   per-step delta residual exceeds the CUSUM threshold.
        # Path C — level-CUSUM: cumulative excursion of the LEVEL
        #   differential (zone temp − cross-zone median) exceeds its
        #   threshold. This is the ONLY path that survives a held drift
        #   (bias plateaued) and is therefore the primary drift channel.
        # Each candidate carries (zone, severity, sign, path_tag).
        candidates: List[Tuple[int, float, int, str]] = []
 
        # Path A: per-step streak with sign consistency
        for i in range(self.n_occupied):
            streak = max(self._streak_pos[i], self._streak_neg[i])
            if streak < self.drift_persist:
                continue
            sign_i = 1 if self._streak_pos[i] > self._streak_neg[i] else -1
            if len(self._sign_buf) >= self.drift_persist:
                recent = np.array([s[i] for s in self._sign_buf[-self.drift_persist:]])
                same = float(np.mean((recent * sign_i) >= 0))
            else:
                same = 1.0
            if same >= self.drift_min_signed_frac:
                candidates.append((i, float(abs(z[i])), sign_i, "streak"))
 
        # Path B: delta-CUSUM crossing.
        # SYMMETRIC TWO-CHANNEL CONFIRMATION (mirrors Path C): a
        # delta-CUSUM candidate must ALSO have level-channel support on
        # the SAME zone. Diagnostics showed the delta channel's FPs are
        # isolated single-step forecaster glitches — z_diff spikes to
        # 8-22 for one step on a DIFFERENT zone each time, then recovers.
        # The delta-CUSUM accumulates these scattered transients into
        # false alarms. A real sensor drift instead drives a SUSTAINED,
        # same-zone offset that the LEVEL channel also registers. So we
        # require the candidate zone's level-CUSUM to have reached a
        # fraction of its own threshold before trusting the delta alert.
        # Replay on real traces: 7/7 delta FPs lacked same-zone level
        # support and would be suppressed.
        # Confirmation is REQUIRED for moderate evidence, but BYPASSED
        # when the channel's own CUSUM is very strong (≥ strong_mult ×
        # threshold). Transient forecaster glitches never sustain a
        # strong one-sided CUSUM — they spike one step and reset — so a
        # CUSUM far above threshold is genuine drift evidence on its own.
        # This recovers weak-but-clear drifts that the gentle confirm
        # gate would otherwise drop, without readmitting the glitches.
        strong = self._strong_evidence_mult
        for i in range(self.n_occupied):
            lvl_confirm_pos = (self._level_cusum_pos[i]
                               >= self._delta_confirm_frac * self._level_cusum_h)
            lvl_confirm_neg = (self._level_cusum_neg[i]
                               >= self._delta_confirm_frac * self._level_cusum_h)
            strong_pos = self._cusum_pos[i] >= strong * self._cusum_h
            strong_neg = self._cusum_neg[i] >= strong * self._cusum_h
            if self._cusum_pos[i] >= self._cusum_h and (lvl_confirm_pos or strong_pos):
                if not any(c[0] == i for c in candidates):
                    sev = float(self._cusum_pos[i] / self._cusum_h)
                    candidates.append((i, sev, +1, "delta_cusum"))
            elif self._cusum_neg[i] >= self._cusum_h and (lvl_confirm_neg or strong_neg):
                if not any(c[0] == i for c in candidates):
                    sev = float(self._cusum_neg[i] / self._cusum_h)
                    candidates.append((i, sev, -1, "delta_cusum"))
 
        # Path C: LEVEL-differential CUSUM crossing — primary held-drift
        # channel. Requires BOTH (a) the CUSUM to have crossed AND (b) a
        # sustained consecutive-elevation streak. A clean weather-driven
        # zone excursion can briefly cross the CUSUM but won't sustain
        # the streak; a real held drift does both.
        #
        # TWO-CHANNEL CONFIRMATION (added after diagnostics showed the
        # level channel produces most FPs at low occupancy): a level-only
        # candidate must ALSO have independent differential support on the
        # SAME zone. Real sensor drift perturbs both the level (zone temp
        # vs building median) AND the differential (per-step residual)
        # channels on the affected zone — they are physically coupled. A
        # clean low-occupancy zone excursion (HVAC setback, startup drift)
        # trips ONLY the level channel; the differential stays quiet.
        # Diagnostics: at level-FP steps, only 37% had z_diff>4, but a
        # genuine drift drives it well above that on the drifting zone.
        # We confirm using the per-zone delta-CUSUM having accumulated a
        # meaningful fraction of its own threshold (independent evidence
        # that the same zone's per-step residual is also one-sided).
        level_confirm_frac = self._level_confirm_frac
        for i in range(self.n_occupied):
            streak_pos_ok = (self._level_cusum_pos[i] >= self._level_cusum_h
                             and self._level_streak_pos[i] >= self._level_persist)
            streak_neg_ok = (self._level_cusum_neg[i] >= self._level_cusum_h
                             and self._level_streak_neg[i] >= self._level_persist)
            # Independent confirmation: the SAME zone's differential
            # delta-CUSUM must be elevated on the SAME side. Real drift
            # makes the per-step residual one-sided too; a clean level
            # excursion does not.
            confirm_pos = (self._cusum_pos[i]
                           >= level_confirm_frac * self._cusum_h)
            confirm_neg = (self._cusum_neg[i]
                           >= level_confirm_frac * self._cusum_h)
            # Strong-evidence bypass: an overwhelming level-CUSUM (a
            # large, sustained, single-zone offset) is genuine drift on
            # its own and should fire EARLY, without waiting the full
            # level_persist streak or needing delta confirmation. A held
            # drift plateaus, so its delta-CUSUM decays to zero and the
            # standard confirmation path stalls — but the level-CUSUM
            # keeps climbing, so a CUSUM far past threshold is unambiguous
            # drift evidence. We still require a SHORT streak (half the
            # normal persistence) so a one-step spike can't trip it.
            short_streak = max(4, self._level_persist // 2)
            strong_lpos = (self._level_cusum_pos[i] >= strong * self._level_cusum_h
                           and self._level_streak_pos[i] >= short_streak)
            strong_lneg = (self._level_cusum_neg[i] >= strong * self._level_cusum_h
                           and self._level_streak_neg[i] >= short_streak)
            if (streak_pos_ok and confirm_pos) or strong_lpos:
                if not any(c[0] == i for c in candidates):
                    sev = float(self._level_cusum_pos[i] / self._level_cusum_h)
                    candidates.append((i, sev, +1, "level_cusum"))
            elif (streak_neg_ok and confirm_neg) or strong_lneg:
                if not any(c[0] == i for c in candidates):
                    sev = float(self._level_cusum_neg[i] / self._level_cusum_h)
                    candidates.append((i, sev, -1, "level_cusum"))
 
        # ── Candidate adjudication ────────────────────────────────────
        # Drift is single-sensor by physical definition, BUT a controller
        # that compensates a drifted sensor (AIF especially) physically
        # drags same-loop neighbors into the candidate list. So:
        #   * exactly one candidate, OR one candidate that DOMINATES the
        #     runner-up by drift_dominance_ratio  -> sensor_drift
        #   * >= mz_fault_zones comparable candidates -> multizone
        #     hvac_fault (held fault: per-zone level offsets with the
        #     cross-zone median moving along, so z_common stays quiet)
        candidates.sort(key=lambda c: -c[1])
        _single = len(candidates) == 1
        _dominant = (len(candidates) >= 2 and candidates[1][1] > 1e-9 and
                     candidates[0][1] >= self._drift_dominance_ratio
                     * candidates[1][1])
        if (not cooled and candidates and (_single or _dominant)):
            zi_occ, sev, sign_i, path_tag = candidates[0]
            global_zone = self.occupied_zone_indices[zi_occ]
            direction = "high" if sign_i > 0 else "low"
            alert = Alert(
                step=self._step_count,
                message=(f"[sensor_drift] zone {global_zone} (occ#{zi_occ}) "
                         f"reads {direction} by |z|={sev:.2f} "
                         f"({path_tag})"),
                kind_guess="sensor_drift",
                sources=[f"forecast_drift_{path_tag}"],
                severity=sev,
                zone_idx=global_zone,
                detector_name="forecast",
            )
            self._n_alerts += 1
            self._n_drift_alerts += 1
            self._cooldown_left = self.cooldown
            # Postmortem context: dump the last seq_len readings of the
            # flagged zone's temperature sensor, plus the per-zone
            # setpoint pair (the controller's action for this zone), the
            # building-wide HVAC demand, the cross-zone median temp, the
            # same-floor neighbours' lowest cooling setpoint, and the
            # hour-of-day at each step. Together these tell at a glance
            # whether the alert reflects a real held offset (temps
            # diverging from the building median, neighbours quiet, HVAC
            # demand modest) versus shared-loop COUPLING (a floor-mate
            # calling for cooling while HVAC demand is high) or a free-
            # floating excursion (HVAC ~idle) — neither of which is a
            # true sensor fault.
            if self.verbose and len(self._temp_history) > 0:
                temps = [float(row[zi_occ]) for row in self._temp_history]
                # Per-zone htg/clg setpoint pair for the flagged zone
                htg_zone = [float(row[zi_occ, 0]) for row in self._sp_history]
                clg_zone = [float(row[zi_occ, 1]) for row in self._sp_history]
                outT  = [float(w[0]) for w in self._wx_history]
                dif   = [float(w[1]) for w in self._wx_history]
                dir_  = [float(w[2]) for w in self._wx_history]
                dem   = [float(d) for d in self._dem_history]
                hrs   = ([float(t[2]) for t in self._time_history]
                         if self._time_history else [float("nan")] * len(temps))
                # Cross-zone median temp each step — did this zone move
                # WITH the building (common shift) or AGAINST it
                # (zone-specific, the drift signature)?
                xz_med = [float(np.median(row)) for row in self._temp_history]
                # Same-floor neighbours share an air loop with this zone.
                # If one of them is calling for cooling (low clg SP) while
                # building HVAC demand is high, a cold drop in THIS zone is
                # shared-loop coupling, not a sensor drift.
                floor = _FLOOR_OF_OCC_POS[zi_occ]
                neighbors = [p for p in _FLOOR_GROUPS[floor] if p != zi_occ]
                nbr_min_clg = ([min(float(row[p, 1]) for p in neighbors)
                                for row in self._sp_history]
                               if neighbors else [])
                fmt = lambda xs, w=6, p=2: " ".join(f"{x:{w}.{p}f}" for x in xs)
                # Label TP vs FP from the ground truth in `info`. A drift
                # alert is a TP iff a sensor_drift is currently active in
                # the truth; otherwise it's an FP. If no truth is supplied
                # (production deployment), label as "?".
                gt_kinds = list(info.get("anomaly_kinds", []) or []) if info else []
                if "sensor_drift" in gt_kinds:
                    label = "✓ TP"
                elif info is not None and "anomaly_kinds" in info:
                    label = "✗ FP"
                else:
                    label = "? "
                print(f"      [drift postmortem {label}] zone {global_zone} "
                      f"(occ#{zi_occ}, floor {floor}) — last {len(temps)} steps:")
                print(f"        hour    : {fmt(hrs, w=6, p=1)}")
                print(f"        temp °C : {fmt(temps)}")
                print(f"        xz med  : {fmt(xz_med)}")
                print(f"        htg SP  : {fmt(htg_zone)}")
                print(f"        clg SP  : {fmt(clg_zone)}")
                if neighbors:
                    print(f"        nbr clg↓: {fmt(nbr_min_clg)}   "
                          f"(lowest cooling SP among same-floor zones)")
                print(f"        HVAC dmd: {fmt(dem, w=8, p=0)}")
                print(f"        out °C  : {fmt(outT)}")
                print(f"        diff sol: {fmt(dif, w=6, p=0)}")
                print(f"        dir  sol: {fmt(dir_, w=6, p=0)}")
                # Latest same-floor neighbour snapshot + a quick heuristic
                # verdict so you can eyeball coupling-vs-drift instantly.
                if neighbors:
                    last_t = self._temp_history[-1]
                    last_sp = self._sp_history[-1]
                    nbr_str = ", ".join(
                        f"z{self.occupied_zone_indices[p]}:"
                        f"T={float(last_t[p]):.1f}/clg={float(last_sp[p, 1]):.1f}"
                        for p in neighbors)
                    print(f"        floor nbrs (now): {nbr_str}")
                    dem_now = dem[-1] if dem else 0.0
                    min_clg_now = nbr_min_clg[-1] if nbr_min_clg else 99.0
                    if dem_now > 100.0 and min_clg_now <= 24.5:
                        print(f"        ⇒ likely SHARED-LOOP COUPLING (HVAC "
                              f"active + a floor-mate calling for cooling), "
                              f"not a true sensor drift")
                    elif dem_now <= 100.0:
                        print(f"        ⇒ HVAC ~idle — drop is free-floating "
                              f"(weather/economizer/boundary), not mechanical")
            # Reset the firing zone's streaks AND CUSUM so we don't
            # keep emitting on the same accumulator.
            self._streak_pos[zi_occ] = 0
            self._streak_neg[zi_occ] = 0
            self._cusum_pos[zi_occ] = 0.0
            self._cusum_neg[zi_occ] = 0.0
            self._level_cusum_pos[zi_occ] = 0.0
            self._level_cusum_neg[zi_occ] = 0.0
            self._level_streak_pos[zi_occ] = 0
            self._level_streak_neg[zi_occ] = 0
            self._record_diag(info, fired=True,
                              source=f"forecast_drift_{path_tag}",
                              kind="sensor_drift")
            return alert
 
        # ── Multizone level fault: comparable drift-style candidates on
        # several zones at once. This is the held-fault signature under a
        # compensating controller: per-zone level offsets persist while
        # the per-step differential and the common-mode channel stay
        # quiet (the cross-zone median moves with the fault).
        if (not cooled and len(candidates) >= self._mz_fault_zones):
            zones_g = [self.occupied_zone_indices[c[0]] for c in candidates]
            sev = float(np.mean([c[1] for c in candidates]))
            alert = Alert(
                step=self._step_count,
                message=(f"[hvac_fault] multizone level offset on "
                         f"{len(candidates)} zones {zones_g}; "
                         f"mean sev={sev:.2f}; step={self._step_count}"),
                kind_guess="hvac_fault",
                sources=["forecast_multizone_level"],
                severity=sev,
                zone_idx=None,
                detector_name="forecast",
            )
            self._n_alerts += 1
            self._n_fault_alerts += 1
            self._cooldown_left = self.cooldown
            for c in candidates:
                i = c[0]
                self._streak_pos[i] = 0
                self._streak_neg[i] = 0
                self._cusum_pos[i] = 0.0
                self._cusum_neg[i] = 0.0
                self._level_cusum_pos[i] = 0.0
                self._level_cusum_neg[i] = 0.0
                self._level_streak_pos[i] = 0
                self._level_streak_neg[i] = 0
            self._record_diag(info, fired=True,
                              source="forecast_multizone_level",
                              kind="hvac_fault")
            return alert

        # No alert this step — still record the snapshot (clean or
        # missed-anomaly) so we can analyze near-misses and FPs.
        self._record_diag(info, fired=False, source=None, kind=None)
        return None
 
    # ──────────────────────────────────────────────────────────────────
    # Diagnostic logging
    # ──────────────────────────────────────────────────────────────────
 
    def _record_diag(self, info: Optional[dict], fired: bool,
                     source: Optional[str], kind: Optional[str]) -> None:
        """Finalize and store the current step's diagnostic snapshot."""
        if not self._diag_enabled or self._cur_snap is None:
            return
        snap = self._cur_snap
        snap["alert_fired"] = bool(fired)
        snap["alert_source"] = source
        snap["alert_kind"] = kind
        # Refresh live accumulator fields (they may have changed after
        # the snapshot was first built)
        snap["demand_streak"] = int(self._fault_joint_streak)
        snap["demand_cusum"] = float(self._demand_cusum)
        snap["common_streak"] = int(max(self._common_streak_pos,
                                        self._common_streak_neg))
        snap["common_cusum_max"] = float(max(self._common_cusum_pos,
                                             self._common_cusum_neg))
        if len(self.diag_log) < self._diag_max:
            self.diag_log.append(snap)
        self._cur_snap = None
 
    def dump_diagnostics(self, path: str,
                         n_tp_examples: int = 12,
                         n_fp_examples: int = 12,
                         gt_grace_steps: int = 12,
                         fp_burst_steps: int = 200,
                         gt_events=None) -> dict:
        """Write a structured diagnostic report to `path` (JSON).
 
        Selects:
          * Every TP alert (alert fired AND ground-truth anomaly active,
            OR active within the preceding `gt_grace_steps`)
          * Up to n_fp_examples FP alerts (alert fired AND NO ground-
            truth anomaly active or recently active)
          * For each selected alert, a short trace of the surrounding
            steps (±6) so you can see how the signals evolved into the
            decision.
          * Aggregate stats comparing TP vs FP signal distributions.
 
        Scoring fixes
        -------------
        `gt_grace_steps`: an injected anomaly ramps its effect OUT over
        ~8 steps, and the detector's 16-step rolling-demand mean plus its
        CUSUMs LAG the signal, so a correct detection often crosses
        threshold a few steps AFTER ground-truth flips inactive. Crediting
        alerts whose ground-truth was active within the preceding
        `gt_grace_steps` counts those decay-tail detections as TP instead
        of penalising the detector for the injector's ramp-out and its own
        integration lag.
 
        `fp_burst_steps`: one clean episode commonly trips the detector
        several times in a row (each step re-satisfies the condition until
        the episode passes). Counting every alert inflates the FP rate by
        the episode's length. We additionally report FP collapsed into
        BURSTS (alerts within `fp_burst_steps` of each other = one event),
        which is the honest event-level false-alarm count.
 
        Returns the report dict (also written to JSON).
        """
        import json
 
        log = self.diag_log
        if not log:
            raise RuntimeError(
                "No diagnostics captured. Call enable_diagnostics() "
                "before running inference.")
 
        # Index by step for fast trace lookup
        by_step = {s["step"]: i for i, s in enumerate(log)}
 
        def trace_around(center_step: int, radius: int = 6):
            out = []
            for st in range(center_step - radius, center_step + radius + 1):
                idx = by_step.get(st)
                if idx is not None:
                    s = log[idx]
                    out.append({
                        "step": s["step"],
                        "gt_active": s["gt_anomaly_active"],
                        "gt_kinds": s["gt_kinds"],
                        "gt_hvac_eff": round(s["gt_hvac_eff"], 3),
                        "gt_drift_bias_max": round(s["gt_drift_bias_max"], 3),
                        "demand_z": round(s["demand_z"], 2),
                        "demand_cusum": round(s["demand_cusum"], 2),
                        "z_common": round(s["z_common"], 2),
                        "common_cusum_max": round(s["common_cusum_max"], 2),
                        "z_diff_max": round(s["z_diff_max"], 2),
                        "z_diff_argmax": s["z_diff_argmax"],
                        "level_z_max": round(s["level_z_max"], 2),
                        "level_z_argmax": s["level_z_argmax"],
                        "level_cusum_max": round(s["level_cusum_max"], 2),
                        "level_streak_max": s["level_streak_max"],
                        "out_temp": round(s["out_temp"], 2),
                        "occ_count": s["occ_count"],
                        "hvac_demand": round(s["hvac_demand"], 1),
                        "hour": round(s["hour"], 2),
                        "alert": s["alert_source"] if s["alert_fired"] else None,
                    })
            return out
 
        # Classify every fired alert as TP or FP. An alert counts as TP
        # if ground-truth is active at the step OR was active within the
        # preceding `gt_grace_steps` (decay-tail / detector-lag credit).
        def gt_active_within(center: int, back: int) -> bool:
            for st in range(center, center - back - 1, -1):
                idx = by_step.get(st)
                if idx is not None and log[idx]["gt_anomaly_active"]:
                    return True
            return False
 
        tp_alerts, fp_alerts = [], []
        for s in log:
            if not s["alert_fired"]:
                continue
            if s["gt_anomaly_active"] or gt_active_within(s["step"], gt_grace_steps):
                tp_alerts.append(s)
            else:
                fp_alerts.append(s)
 
        # Collapse FP alerts into bursts (alerts within fp_burst_steps of
        # each other = one false-alarm episode) for an honest event-level
        # count. Also collapse TP alerts into distinct detected episodes.
        def collapse(alerts, gap):
            seq = sorted(alerts, key=lambda s: s["step"])
            bursts = []
            for s in seq:
                if bursts and s["step"] - bursts[-1][-1]["step"] <= gap:
                    bursts[-1].append(s)
                else:
                    bursts.append([s])
            return bursts
        fp_bursts = collapse(fp_alerts, fp_burst_steps)
        tp_bursts = collapse(tp_alerts, fp_burst_steps)
 
        # Sample evenly across the timeline so we don't get 12 FPs all
        # clustered in one bad hour
        def sample_even(items, n):
            if len(items) <= n:
                return items
            idxs = np.linspace(0, len(items) - 1, n).astype(int)
            return [items[i] for i in idxs]
 
        tp_sel = sample_even(tp_alerts, n_tp_examples)
        fp_sel = sample_even(fp_alerts, n_fp_examples)
 
        def signal_stats(items):
            if not items:
                return {}
            keys = ["demand_z", "demand_cusum", "z_common", "common_cusum_max",
                    "z_diff_max", "level_z_max", "level_cusum_max",
                    "level_streak_max", "out_temp", "occ_count", "hvac_demand"]
            out = {}
            for k in keys:
                vals = np.array([s[k] for s in items], dtype=float)
                out[k] = {
                    "mean": round(float(vals.mean()), 3),
                    "median": round(float(np.median(vals)), 3),
                    "min": round(float(vals.min()), 3),
                    "max": round(float(vals.max()), 3),
                }
            return out
 
        # Source breakdown of all FPs
        fp_src = {}
        for s in fp_alerts:
            fp_src[s["alert_source"]] = fp_src.get(s["alert_source"], 0) + 1
        tp_src = {}
        for s in tp_alerts:
            tp_src[s["alert_source"]] = tp_src.get(s["alert_source"], 0) + 1
 
        report = {
            "summary": {
                "total_steps_logged": len(log),
                "total_alerts": len(tp_alerts) + len(fp_alerts),
                "n_tp_alerts": len(tp_alerts),
                "n_fp_alerts": len(fp_alerts),
                "n_tp_detections": len(tp_bursts),   # distinct detected episodes
                "n_fp_bursts": len(fp_bursts),       # distinct false-alarm episodes
                "gt_grace_steps": gt_grace_steps,
                "fp_burst_steps": fp_burst_steps,
                "tp_source_breakdown": tp_src,
                "fp_source_breakdown": fp_src,
            },
            "signal_stats": {
                "TP_alerts": signal_stats(tp_alerts),
                "FP_alerts": signal_stats(fp_alerts),
            },
            "TP_examples": [
                {
                    "alert_step": s["step"],
                    "alert_source": s["alert_source"],
                    "alert_kind": s["alert_kind"],
                    "gt_kinds": s["gt_kinds"],
                    "trace": trace_around(s["step"]),
                }
                for s in tp_sel
            ],
            "FP_examples": [
                {
                    "alert_step": s["step"],
                    "alert_source": s["alert_source"],
                    "alert_kind": s["alert_kind"],
                    "trace": trace_around(s["step"]),
                }
                for s in fp_sel
            ],
        }
 
        # ── Event-level recall report (when ground-truth windows given) ──
        # gt_events: list of (kind, start_step, end_step). An event counts
        # as detected if any alert fired within [start, end + gt_grace_steps]
        # (the grace tail credits decay-lag detections, same as TP scoring).
        if gt_events:
            fired_steps = sorted(s["step"] for s in log
                                 if s.get("alert_fired"))
            log_by_step = {s["step"]: s for s in log}

            def _peak(field, start, end):
                vals = [abs(float(log_by_step[st].get(field, 0.0)))
                        for st in range(start, end + 1) if st in log_by_step]
                return round(max(vals), 2) if vals else 0.0

            def _event_signals(start, end):
                # Provide every key run_diagnostics/aggregate_diag print.
                # Fields this detector version doesn't track (roll_dem,
                # n_sag) are reported as 0.0 rather than omitted.
                return {
                    "max_demand_z": _peak("demand_z", start, end),
                    "max_roll_dem": _peak("roll_dem", start, end),
                    "max_n_sag":    _peak("n_sag", start, end),
                    "max_z_diff":   _peak("z_diff_max", start, end),
                    "max_level_z":  _peak("level_z_max", start, end),
                    "max_z_common": _peak("common_cusum_max", start, end),
                }

            def _likely_block(sig):
                out = []
                if sig["max_demand_z"] < 1.0:
                    out.append("demand quiet")
                if sig["max_level_z"] < 3.0:
                    out.append("level weak")
                if sig["max_z_diff"] < 4.0:
                    out.append("differential weak")
                if not out:
                    out.append("signal present but below fire threshold")
                return out

            detected = 0
            missed = []
            by_kind: dict = {}
            for kind, start, end in gt_events:
                bk = by_kind.setdefault(kind, {"total": 0, "detected": 0})
                bk["total"] += 1
                hi = end + gt_grace_steps
                hit = any(start <= fs <= hi for fs in fired_steps)
                if hit:
                    detected += 1
                    bk["detected"] += 1
                else:
                    sig = _event_signals(int(start), int(end))
                    missed.append({
                        "kind": kind,
                        "start": int(start),
                        "end": int(end),
                        "likely_block": _likely_block(sig),
                        "signals": sig,
                    })

            n_events = len(gt_events)
            report["event_report"] = {
                "n_events": n_events,
                "n_detected": detected,
                "n_missed": n_events - detected,
                "recall_by_kind": {
                    k: {"total": v["total"], "detected": v["detected"],
                        "recall": (v["detected"] / v["total"]) if v["total"]
                        else 0.0}
                    for k, v in by_kind.items()
                },
                "missed_events": missed,
            }

        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        if self.verbose:
            print(f"  [forecast] Diagnostics written → {path}")
            print(f"    {len(tp_alerts)} TP alerts, {len(fp_alerts)} FP alerts")
            print(f"    FP sources: {fp_src}")
        return report
 
    # ──────────────────────────────────────────────────────────────────
    # Reporting
    # ──────────────────────────────────────────────────────────────────
 
    def summary(self) -> dict:
        """Diagnostic summary dict.
 
        Schema notes
        ------------
        We expose BOTH the legacy-LSTM-AE keys (`total_alerts`,
        `top_features`) AND the new forecast-detector keys
        (`n_alerts`, `by_kind`, residual stats). This lets the existing
        wellbeing_main.py end-of-episode print code work unchanged while
        also exposing the richer breakdown to any newer consumer.
        """
        total = self._n_alerts
        if total == 0:
            by_kind = {}
            top_features = {}
        else:
            by_kind = {
                "hvac_fault":   self._n_fault_alerts / total,
                "sensor_drift": self._n_drift_alerts / total,
            }
            # `top_features` in the LSTM-AE detector was a kind→fraction
            # histogram. We mirror that shape so wellbeing_main's
            # "Top anomalous feats" line renders sensibly here too.
            top_features = dict(sorted(by_kind.items(),
                                       key=lambda kv: -kv[1]))
        # Recent residual norm stats for diagnostic
        resid_stats = {}
        if self._residual_log:
            arr = np.array(self._residual_log[-2000:])
            resid_stats = {
                "residual_norm_mean":  float(arr.mean()),
                "residual_norm_p95":   float(np.percentile(arr, 95)),
                "residual_norm_p99":   float(np.percentile(arr, 99)),
            }
        return {
            # ── Legacy keys (read by wellbeing_main.py) ────────────────
            "total_alerts": total,
            "top_features": top_features,
            # ── Forecast-detector keys ────────────────────────────────
            "n_alerts":   total,
            "by_kind":    by_kind,
            "n_fault_alerts": self._n_fault_alerts,
            "n_drift_alerts": self._n_drift_alerts,
            "step_count": self._step_count,
            "is_fitted":  self.is_fitted,
            **resid_stats,
        }
 
    # ──────────────────────────────────────────────────────────────────
    # Persistence
    # ──────────────────────────────────────────────────────────────────
 
    def save(self, path: str) -> None:
        """Save to pickle in a format wellbeing_main can detect by version."""
        state = {
            "version": self.PICKLE_VERSION,
            "hyperparams": {
                "n_zones": self.n_zones,
                "n_occupied": self.n_occupied,
                "occupied_zone_indices": self.occupied_zone_indices,
                "seq_len": self.seq_len,
                "hidden_size": self.hidden_size,
                "k_sigma": self.k_sigma,
                "hvac_zones_required": self.hvac_zones_required,
                "fault_persist": self.fault_persist,
                "drift_persist": self.drift_persist,
                "cooldown": self.cooldown,
                "drift_min_signed_frac": self.drift_min_signed_frac,
                "cusum_slack": self._cusum_slack,
                "cusum_h": self._cusum_h,
                "diff_sigma_scale": self._diff_sigma_scale,
                "common_mode_persist": self._common_mode_persist,
                "common_mode_h": self._common_mode_h,
                "mz_fault_zones": self._mz_fault_zones,
                "drift_dominance_ratio": self._drift_dominance_ratio,
            },
            "model_state": (self.model.state_dict()
                            if self.model is not None else None),
            "sigma_table": self.normalizer.sigma_table,
            "mean_table":  self.normalizer.mean_table,
            "diff_sigma_table":   self.normalizer.diff_sigma_table,
            "common_sigma_table": self.normalizer.common_sigma_table,
            "demand_log_mean":    self._demand_log_mean,
            "demand_log_std":     self._demand_log_std,
            "demand_global_log_mean": self._demand_global_log_mean,
            "demand_global_log_std":  self._demand_global_log_std,
            "temp_bucket_edges":  self._temp_bucket_edges,
            "level_baseline_mean": self._level_baseline_mean,
            "level_baseline_std":  self._level_baseline_std,
            "online_var":  self.normalizer._online_var,
            "is_fitted":   self.is_fitted,
        }
        # Convert any torch tensors to cpu numpy-friendly form
        if state["model_state"] is not None:
            state["model_state"] = {k: v.detach().cpu()
                                    for k, v in state["model_state"].items()}
        with open(path, "wb") as f:
            pickle.dump(state, f)
 
    @classmethod
    def load(cls, path: str) -> "ForecastAnomalyDetector":
        with open(path, "rb") as f:
            state = pickle.load(f)
        if state.get("version") != cls.PICKLE_VERSION:
            raise ValueError(
                f"Pickle version mismatch: expected {cls.PICKLE_VERSION!r}, "
                f"got {state.get('version')!r}")
        hp = state["hyperparams"]
        det = cls(**hp, verbose=False)
        # Rebuild the model and load weights
        if state["model_state"] is not None:
            det.model = _ForecastGRU(
                state_dim=_STATE_DIM,
                action_dim=_ACTION_DIM,
                hidden_size=det.hidden_size,
                n_targets=det.n_occupied,
            ).to(det.device)
            det.model.load_state_dict({k: v.to(det.device)
                                       for k, v in state["model_state"].items()})
            det.model.eval()
        det.normalizer.sigma_table = state["sigma_table"]
        det.normalizer.mean_table  = state["mean_table"]
        # New tables — gracefully absent in older pickles
        det.normalizer.diff_sigma_table   = state.get("diff_sigma_table")
        det.normalizer.common_sigma_table = state.get("common_sigma_table")
        # Demand baseline — gracefully absent in older pickles
        det._demand_log_mean = state.get("demand_log_mean")
        det._demand_log_std  = state.get("demand_log_std")
        det._demand_global_log_mean = state.get("demand_global_log_mean", 0.0)
        det._demand_global_log_std  = state.get("demand_global_log_std", 1.0)
        det._temp_bucket_edges = state.get("temp_bucket_edges")
        det._level_baseline_mean = state.get("level_baseline_mean")
        det._level_baseline_std  = state.get("level_baseline_std")
        det.normalizer._online_var = state["online_var"]
        det.is_fitted = bool(state["is_fitted"])
        return det