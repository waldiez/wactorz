"""
metrics_utils.py — standalone comfort / reporting metrics for the Sinergym
OfficeMedium bridge.

Self-contained (numpy + datetime only). Lifted verbatim from the ensemble
controller's reporting helpers so the bridge can compute the SAME monthly /
comfort-score / ZCR / deviation numbers WITHOUT importing maddpg_v3 (which pulls
in ensemble_controller) or any training code — exactly the way
hourly_reward_metric.py is a small separate file.

Provides:
    COMFORT_WINTER, COMFORT_SUMMER
    get_seasonal_comfort(month, day)         -> (low, high)
    get_occupancy_state(month, day, hour)    -> 'occupied'|'pre_occupy'|...
    tou_multiplier(month, day, hour)         -> float
    compute_comfort_score(obs)               -> (score|None, info)
    compute_mean_deviation(obs, occupied_only=True)     -> (mean_dev_C, n)
    compute_zone_comfort_rate(obs, occupied_only=True)  -> (n_ok, n_total)
    CSAccumulator                            -> .update(obs) / .mean / .mean_occ / .reset()

NOTE: this does NOT provide CustomRewardWrapper (the training-shaped reward).
That lives in maddpg_v2 and is the only thing the bridge still optionally needs
maddpg for; the bridge imports it separately and degrades gracefully if absent.
"""

from datetime import datetime, date
from typing import Optional, Dict, Tuple

import numpy as np

# ── Constants (OfficeMedium, 92-dim obs) ────────────────────────────────────
N_OCCUPIED = 15

COMFORT_WINTER = (20.0, 23.5)
COMFORT_SUMMER = (23.0, 26.0)

IDX_MONTH = 0
IDX_DAY   = 1
IDX_HOUR  = 2
IDX_HVAC_DEMAND = 90
IDX_ZONE_TEMPS  = slice(9, 24)   # 15 occupied zone air temperatures

OCCUPIED_ZONES = [
    "Core_bottom", "Core_mid", "Core_top",
    "Perimeter_bot_ZN_1", "Perimeter_bot_ZN_2",
    "Perimeter_bot_ZN_3", "Perimeter_bot_ZN_4",
    "Perimeter_mid_ZN_1", "Perimeter_mid_ZN_2",
    "Perimeter_mid_ZN_3", "Perimeter_mid_ZN_4",
    "Perimeter_top_ZN_1", "Perimeter_top_ZN_2",
    "Perimeter_top_ZN_3", "Perimeter_top_ZN_4",
]


# ── Helpers ─────────────────────────────────────────────────────────────────
def get_seasonal_comfort(month: int, day: int) -> Tuple[float, float]:
    is_summer = ((month > 6) or (month == 6 and day >= 1)) and \
                ((month < 10) or (month == 9 and day <= 30))
    return COMFORT_SUMMER if is_summer else COMFORT_WINTER


def get_occupancy_state(month: int, day: int, hour: int) -> str:
    try:
        is_weekend = datetime(2024, int(month), int(day)).weekday() >= 5
    except Exception:
        is_weekend = False
    if is_weekend:       return 'unoccupied'
    if 7 <= hour <= 19:  return 'occupied'
    if 5 <= hour < 7:    return 'pre_occupy'
    if 19 < hour <= 21:  return 'post_occupy'
    return 'unoccupied'


def tou_multiplier(month: int, day: int, hour: int) -> float:
    try:
        is_weekend = date(2024, int(month), int(day)).weekday() >= 5
    except Exception:
        is_weekend = False
    if is_weekend:
        return 1.0
    h = int(hour)
    if 16 <= h < 21:                     return 3.0   # on-peak
    if (9 <= h < 16) or (21 <= h < 23):  return 1.5   # mid-peak
    return 1.0                                         # off-peak


def compute_comfort_score(obs_raw: np.ndarray) -> Tuple[Optional[float], Dict]:
    """Magnitude-aware, occupancy-weighted comfort score in [0, 1]. Returns
    (None, info) during fully unoccupied hours so they are excluded from the
    average."""
    obs_raw = np.asarray(obs_raw, dtype=np.float64)
    month = int(obs_raw[IDX_MONTH]); day = int(obs_raw[IDX_DAY]); hour = int(obs_raw[IDX_HOUR])
    occ_state = get_occupancy_state(month, day, hour)

    state_weight = {'occupied': 1.0, 'pre_occupy': 0.6,
                    'post_occupy': 0.2, 'unoccupied': 0.0}.get(occ_state, 0.0)
    if state_weight == 0.0:
        return None, {'skipped': True, 'occ_state': occ_state}

    cl, ch = get_seasonal_comfort(month, day)
    zone_temps = np.array([float(obs_raw[9 + i]) for i in range(N_OCCUPIED)])

    zone_occ = np.array([float(obs_raw[45 + i]) for i in range(N_OCCUPIED)])
    has_sensor_occ = np.any(zone_occ > 0)
    if not has_sensor_occ and occ_state == 'occupied':
        zone_occ = np.ones(N_OCCUPIED)

    scores = np.zeros(N_OCCUPIED, dtype=np.float32)
    for i in range(N_OCCUPIED):
        if zone_occ[i] <= 0 and has_sensor_occ:
            scores[i] = 1.0
            continue
        T = float(zone_temps[i])
        if cl <= T <= ch:
            scores[i] = 1.0
        else:
            viol = max(cl - T, 0.0) + max(T - ch, 0.0)
            norm_viol = min(viol / 1.0, 1.0)
            scores[i] = float(1.0 - (norm_viol ** 2))

    comfort_score = float(np.mean(scores))
    worst_idx = int(np.argmin(scores))
    return comfort_score, {
        'occ_state': occ_state,
        'state_weight': state_weight,
        'zone_scores': scores,
        'worst_zone': worst_idx,
        'worst_zone_name': OCCUPIED_ZONES[worst_idx],
        'worst_temp': float(zone_temps[worst_idx]),
        'n_violated': int(np.sum(scores < 0.70)),
        'n_in_band': int(np.sum((zone_temps >= cl) & (zone_temps <= ch))),
    }


def compute_mean_deviation(obs_raw: np.ndarray, occupied_only: bool = True) -> Tuple[float, int]:
    """Mean °C deviation from the comfort band during occupied hours."""
    obs_raw = np.asarray(obs_raw, dtype=np.float64)
    month = int(obs_raw[IDX_MONTH]); day = int(obs_raw[IDX_DAY]); hour = int(obs_raw[IDX_HOUR])
    if occupied_only and get_occupancy_state(month, day, hour) != 'occupied':
        return 0.0, 0
    cl, ch = get_seasonal_comfort(month, day)
    zt = np.array([float(obs_raw[9 + i]) for i in range(N_OCCUPIED)])
    devs = np.maximum(cl - zt, 0) + np.maximum(zt - ch, 0)
    return float(np.mean(devs)), 1


def compute_zone_comfort_rate(obs_raw: np.ndarray, occupied_only: bool = True) -> Tuple[int, int]:
    """Binary zone-comfort rate (n_in_band, n_total) during occupied hours."""
    obs_raw = np.asarray(obs_raw, dtype=np.float64)
    month = int(obs_raw[IDX_MONTH]); day = int(obs_raw[IDX_DAY]); hour = int(obs_raw[IDX_HOUR])
    if occupied_only and get_occupancy_state(month, day, hour) != 'occupied':
        return 0, 0
    cl, ch = get_seasonal_comfort(month, day)
    zt = np.array([float(obs_raw[9 + i]) for i in range(N_OCCUPIED)])
    return int(np.sum((zt >= cl) & (zt <= ch))), N_OCCUPIED


class CSAccumulator:
    """Running comfort score over a window (episode or month).

    .mean      — average comfort score over all *active* steps (occupied /
                 pre-occupy / post-occupy; fully-unoccupied steps are skipped).
    .mean_occ  — average comfort score over *occupied* steps only.
    Built on compute_comfort_score (the same function the ensemble reports), so
    the numbers match the controller's own reporting rather than a re-derivation.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._sum = 0.0; self._n = 0
        self._sum_occ = 0.0; self._n_occ = 0

    def update(self, obs):
        score, info = compute_comfort_score(obs)
        if score is None:
            return
        self._sum += score; self._n += 1
        if info.get('occ_state') == 'occupied':
            self._sum_occ += score; self._n_occ += 1

    @property
    def mean(self) -> float:
        return (self._sum / self._n) if self._n > 0 else float('nan')

    @property
    def mean_occ(self) -> float:
        return (self._sum_occ / self._n_occ) if self._n_occ > 0 else float('nan')


# ============================================================================
# CustomRewardWrapper — training-shaped reward (verbatim from maddpg_v2)
# ============================================================================
# Inlined so the bridge no longer needs maddpg_v2 for the "Custom Reward". The
# weights below are maddpg_v2.Config's reward weights; the math is unchanged.

from datetime import datetime as _dt

try:
    import gymnasium as _gym
    _GYM_OK = True
except Exception:                       # gymnasium not installed -> metrics still usable
    _gym = None
    _GYM_OK = False


class RewardConfig:
    """Reward weights (from maddpg_v2.Config). Override by passing your own
    object with the same attributes to CustomRewardWrapper(env, config=...)."""
    W_ENERGY      = 0.20
    W_COMFORT     = 0.40
    W_ACTION      = 0.00
    W_DEADBAND    = 0.05
    W_TEMP_TARGET = 0.10
    W_PEAK        = 0.10
    W_TOU         = 0.08


def _tou_multiplier(month: int, day: int, hour: int) -> float:
    """Typical California commercial TOU (PG&E B-19 style). Off-peak=1.0,
    mid-peak=1.5, on-peak=3.0."""
    try:
        is_weekend = _dt(2024, int(month), int(day)).weekday() >= 5
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


if _GYM_OK:
    class CustomRewardWrapper(_gym.Wrapper):
        """
        Reward = -(
            W_ENERGY    * energy_penalty   [log-normalised HVAC power]
          + W_COMFORT   * comfort_penalty  [seasonal, occupancy-gated]
          + W_ACTION    * action_penalty   [setpoint change smoothness]
          + W_DEADBAND  * deadband_penalty [htg >= clg - 2C]
          + W_PEAK      * peak_penalty     [new monthly peak increment]
          + W_TOU       * tou_penalty      [extra cost above off-peak]
        ) + W_TEMP_TARGET * midpoint_bonus [zone near comfort midpoint]

        Sets info['custom_reward'] and info['original_reward'] (plus components).
        Verbatim port of maddpg_v2.CustomRewardWrapper.
        """

        def __init__(self, env, config=None):
            super().__init__(env)
            self.config = config if config is not None else RewardConfig
            self.prev_action   = None
            self.action_range  = self.action_space.high - self.action_space.low
            self.max_power     = 300_000.0
            self.running_peak_W  = 0.0
            self._peak_month_key = None

        def reset(self, **kwargs):
            obs, info = self.env.reset(**kwargs)
            self.prev_action   = None
            self.running_peak_W  = 0.0
            self._peak_month_key = None
            return obs, info

        def _maybe_reset_peak(self, month: int):
            key = (2024, int(month))
            if self._peak_month_key != key:
                self._peak_month_key = key
                self.running_peak_W  = 0.0

        def step(self, action):
            obs, original_reward, terminated, truncated, info = self.env.step(action)

            month = int(obs[IDX_MONTH]); day = int(obs[IDX_DAY]); hour = int(obs[IDX_HOUR])
            cl, ch     = get_seasonal_comfort(month, day)
            zone_temps = obs[IDX_ZONE_TEMPS]
            occ_state  = get_occupancy_state(month, day, hour)
            power      = float(obs[IDX_HVAC_DEMAND])

            # 1. Energy penalty (log-normalised)
            energy_penalty = np.log1p(power / self.max_power) / np.log1p(1.0)
            energy_penalty = min(energy_penalty, 2.0)
            energy_weight  = self.config.W_ENERGY * (1.5 if occ_state == 'unoccupied' else 1.0)

            # 2. Comfort penalty (4-tier occupancy)
            is_summer = get_seasonal_comfort(month, day) == COMFORT_SUMMER
            if occ_state == 'occupied':
                cold_v = np.maximum(cl - zone_temps, 0.0)
                hot_v  = np.maximum(zone_temps - ch,  0.0)
                violations = (cold_v + hot_v * 1.3) if is_summer else (cold_v * 1.3 + hot_v)
                zone_comfort_penalties = np.clip(violations / 4.0, 0.0, 1.0).astype(np.float32)
                comfort_penalty = float(np.mean(zone_comfort_penalties))
                comfort_weight  = self.config.W_COMFORT
            elif occ_state == 'pre_occupy':
                violations = (np.maximum(cl - zone_temps, 0.0) + np.maximum(zone_temps - ch, 0.0))
                zone_comfort_penalties = np.clip(violations / 5.0, 0.0, 1.0).astype(np.float32)
                comfort_penalty = float(np.mean(zone_comfort_penalties))
                comfort_weight  = self.config.W_COMFORT * 0.4
            elif occ_state == 'post_occupy':
                violations = (np.maximum(cl - 3.0 - zone_temps, 0.0) + np.maximum(zone_temps - ch - 3.0, 0.0))
                zone_comfort_penalties = np.clip(violations / 5.0, 0.0, 1.0).astype(np.float32)
                comfort_penalty = float(np.mean(zone_comfort_penalties))
                comfort_weight  = self.config.W_COMFORT * 0.15
            else:
                zone_comfort_penalties = np.zeros(N_OCCUPIED, dtype=np.float32)
                comfort_penalty = 0.0
                comfort_weight  = 0.0

            # 3. Midpoint bonus (occupied + pre-occupy)
            if occ_state == 'occupied':
                comfort_mid = (cl + ch) / 2.0
                dist = np.abs(zone_temps - comfort_mid)
                zone_midpoint_bonus = np.clip(1.0 - dist / 2.5, 0.0, 1.0).astype(np.float32)
                midpoint_bonus  = float(np.mean(zone_midpoint_bonus))
                midpoint_weight = self.config.W_TEMP_TARGET
            elif occ_state == 'pre_occupy':
                comfort_mid = (cl + ch) / 2.0
                dist = np.abs(zone_temps - comfort_mid)
                zone_midpoint_bonus = np.clip(1.0 - dist / 4.0, 0.0, 1.0).astype(np.float32)
                midpoint_bonus  = float(np.mean(zone_midpoint_bonus))
                midpoint_weight = self.config.W_TEMP_TARGET * 0.5
            else:
                zone_midpoint_bonus = np.zeros(N_OCCUPIED, dtype=np.float32)
                midpoint_bonus  = 0.0
                midpoint_weight = 0.0

            # 4. Action-smoothness penalty
            if self.prev_action is not None and occ_state in ('occupied', 'pre_occupy'):
                delta      = np.abs(action - self.prev_action)
                safe_range = np.where(self.action_range > 0, self.action_range, 1.0)
                action_penalty = float(np.mean(1.0 - np.exp(-3.0 * delta / safe_range)))
                action_weight  = self.config.W_ACTION
            else:
                action_penalty = 0.0
                action_weight  = 0.0
            self.prev_action = action.copy()

            # 5. Deadband penalty
            htg_a = action[:N_OCCUPIED]; clg_a = action[N_OCCUPIED:]
            db_viol = np.maximum(htg_a - clg_a + 2.0, 0.0)
            zone_db_penalties = np.clip(db_viol / 3.0, 0.0, 1.0).astype(np.float32)
            deadband_penalty  = float(np.mean(zone_db_penalties))

            # 6. Peak-demand penalty (sparse)
            self._maybe_reset_peak(month)
            new_peak_W = max(power - self.running_peak_W, 0.0)
            if new_peak_W > 0.0:
                self.running_peak_W = power
            peak_penalty = np.log1p(new_peak_W / self.max_power) / np.log1p(1.0)
            peak_penalty = min(peak_penalty, 2.0)

            # 7. TOU surcharge (extra above off-peak)
            tou_mult   = _tou_multiplier(month, day, hour)
            tou_extra  = max(tou_mult - 1.0, 0.0)
            norm_power = np.log1p(power / self.max_power) / np.log1p(1.0)
            tou_penalty = min(tou_extra * norm_power, 2.0)

            # Combine
            reward = -(
                energy_weight             * energy_penalty
                + comfort_weight          * comfort_penalty
                + action_weight           * action_penalty
                + self.config.W_DEADBAND  * deadband_penalty
                + self.config.W_PEAK      * peak_penalty
                + self.config.W_TOU       * tou_penalty
            ) + midpoint_weight * midpoint_bonus

            # Per-zone decomposition
            energy_per_zone = energy_weight * energy_penalty / N_OCCUPIED
            peak_per_zone   = self.config.W_PEAK * peak_penalty / N_OCCUPIED
            tou_per_zone    = self.config.W_TOU  * tou_penalty  / N_OCCUPIED
            zone_rewards = np.zeros(N_OCCUPIED, dtype=np.float32)
            for i in range(N_OCCUPIED):
                zone_rewards[i] = -(
                    energy_per_zone
                    + comfort_weight          * zone_comfort_penalties[i]
                    + self.config.W_DEADBAND  * zone_db_penalties[i]
                    + peak_per_zone
                    + tou_per_zone
                ) + midpoint_weight * zone_midpoint_bonus[i]

            info['custom_reward']   = reward
            info['original_reward'] = original_reward
            info['zone_rewards']    = zone_rewards
            info['energy_penalty']  = energy_penalty
            info['comfort_penalty'] = comfort_penalty
            info['peak_penalty']    = peak_penalty
            info['tou_penalty']     = tou_penalty
            info['tou_multiplier']  = tou_mult
            info['running_peak_W']  = self.running_peak_W
            info['new_peak_W']      = new_peak_W
            info['power_W']         = power
            info['occ_state']       = occ_state
            return obs, reward, terminated, truncated, info
else:
    CustomRewardWrapper = None   # gymnasium unavailable


# ============================================================================
# Hourly-schedule linear reward (verbatim from hourly_reward_metric.py)
# ============================================================================
# Reporting-only reproduction of Sinergym's HourlyLinearReward, computed from
# the raw obs. Constants match register_env.py / Sinergym LinearReward defaults.
RANGE_COMFORT_WINTER = (20.0, 23.5)
RANGE_COMFORT_SUMMER = (23.0, 26.0)
SUMMER_START = (6, 1)
SUMMER_FINAL = (9, 30)

ENERGY_WEIGHT      = 0.5
LAMBDA_ENERGY      = 1.0e-4
LAMBDA_TEMPERATURE = 1.0

# HourlyLinearReward comfort-hours window (Sinergym default (9, 19)).
COMFORT_HOURS = (9, 19)

ZONE_TEMPS_SLICE = slice(9, 24)   # 15 occupied zones (reward temperature vars)
_HLR_YEAR = 2024


def _hlr_temp_range(month: int, day: int):
    """Pick summer vs winter comfort range exactly like Sinergym."""
    try:
        dt = datetime(_HLR_YEAR, month, day)
        s0 = datetime(_HLR_YEAR, *SUMMER_START)
        s1 = datetime(_HLR_YEAR, *SUMMER_FINAL)
        is_summer = s0 <= dt <= s1
    except Exception:
        is_summer = False
    return RANGE_COMFORT_SUMMER if is_summer else RANGE_COMFORT_WINTER


def compute_hourly_linear_reward(obs):
    """
    Reproduce Sinergym HourlyLinearReward for one step from the raw obs array.
    Returns (reward, terms); sign convention matches Sinergym (reward <= 0).
    """
    obs = np.asarray(obs, dtype=np.float64)
    month = int(obs[IDX_MONTH]); day = int(obs[IDX_DAY]); hour = int(obs[IDX_HOUR])

    # Energy penalty: -sum(power over energy_variables)
    power = float(obs[IDX_HVAC_DEMAND])
    energy_penalty = -power

    # Comfort penalty: -sum_zones( max(T_low - T, 0, T - T_up) )
    low, high = _hlr_temp_range(month, day)
    zt = np.asarray(obs[ZONE_TEMPS_SLICE], dtype=np.float64)
    violations = np.maximum.reduce([low - zt, np.zeros_like(zt), zt - high])
    total_violation = float(np.sum(violations))
    comfort_penalty = -total_violation

    # Hourly weight switch: comfort term vanishes outside comfort hours.
    w = ENERGY_WEIGHT if COMFORT_HOURS[0] <= hour <= COMFORT_HOURS[1] else 1.0

    energy_term  = LAMBDA_ENERGY      * w         * energy_penalty
    comfort_term = LAMBDA_TEMPERATURE * (1.0 - w) * comfort_penalty
    reward = energy_term + comfort_term

    return reward, {
        "energy_term":                 energy_term,
        "comfort_term":                comfort_term,
        "energy_penalty":              energy_penalty,
        "comfort_penalty":             comfort_penalty,
        "total_power_demand":          power,
        "total_temperature_violation": total_violation,
        "reward_weight":               w,
    }
