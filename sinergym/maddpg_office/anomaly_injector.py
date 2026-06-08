"""
anomaly_injector.py
====================
Wraps a Sinergym Gymnasium environment and injects four types of
controlled anomalies into observations so the rest of the stack can
detect and reason about them.

Anomaly types
-------------
  hvac_fault      – HVAC efficiency multiplied by a factor < 1.0
  sensor_drift    – slow-walking bias added to zone temperature sensors
  occ_spike       – occupancy signals forced to 1 (unexpected crowd)
  weather_shock   – offset added to outdoor-temperature observation

All anomalies follow a deterministic schedule (AnomalySchedule).
A log of active/historical anomalies is kept so the causal layer and
LLM interface can query ground-truth labels for evaluation.
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List
import gymnasium as gym


# ---------------------------------------------------------------------------
# Anomaly descriptor
# ---------------------------------------------------------------------------

@dataclass
class Anomaly:
    kind: str                       # 'hvac_fault' | 'sensor_drift' | 'occ_spike' | 'weather_shock'
    step_offset: int = 0            # episode step at which this anomaly starts
    duration: int = 96              # steps  (96 × 15 min = 1 day)
    magnitude: float = 1.0         # meaning depends on kind
    zone_idx: Optional[int] = None  # None → all zones
    description: str = ""

    # runtime state (set by wrapper)
    _active: bool = field(default=False, repr=False)
    _start_step: int = field(default=-1, repr=False)
    _triggered: bool = field(default=False, repr=False)

    @property
    def label(self) -> str:
        z = f"zone{self.zone_idx}" if self.zone_idx is not None else "all"
        return f"{self.kind}|{z}|mag={self.magnitude:.2f}"

    def reset(self):
        self._active    = False
        self._start_step = -1
        self._triggered  = False


# ---------------------------------------------------------------------------
# Anomaly schedule
# ---------------------------------------------------------------------------

@dataclass
class AnomalySchedule:
    """Ordered list of Anomaly objects with step_offset relative to episode start."""
    entries: List[Anomaly] = field(default_factory=list)
    seed: Optional[int] = None   # set by default_demo for reproducibility

    @classmethod
    def default_demo(cls, n_zones: int = 1,
                     seed: Optional[int] = None) -> "AnomalySchedule":
        """
        Demo schedule with 24 short, well-spaced anomalies (~twice per
        month), RANDOMISED within fixed value pools so every call is a
        different layout drawn from the same difficulty distribution.

        Randomisation
        -------------
        The *pools* of values are fixed (the same multiset of HVAC
        efficiencies, drift magnitudes, durations, and hours every time),
        but each call:
          * shuffles which value lands on which slot,
          * jitters each event's date by ±4 days off the 15-day grid,
          * draws each drift's zone at random from the safe-zone set.
        So the detector can never be tuned to one fixed calendar — but the
        difficulty (value distribution, 12 hvac + 12 drift, ~twice/month,
        ≥7-day spacing) is held constant across runs.

        `seed`
        ------
        Pass an int for a REPRODUCIBLE layout — essential when A/B-testing
        a detector change, so both versions face the *same* schedule and
        the metric delta is the detector, not luck. Leave it None for a
        fresh random layout each call; the drawn seed is printed and
        stored on `.seed` so an interesting setup can be reproduced later.

        Design rationale (unchanged)
        ----------------------------
        Short anomalies (≤ a few hours) keep the whole window inside the
        detector's "history normal, present weird" regime. 24 events (12
        hvac_fault + 12 sensor_drift) halve the binomial standard error on
        per-kind recall vs 12. Events stay ≥7 days apart (no overlap, room
        to settle) and start after the extra-cold first fortnight.
        """
        DAY = 96
        HRS = 4   # 4 timesteps per hour at 15-min resolution

        # Resolve a concrete seed so every run differs yet stays
        # reproducible (the printed seed regenerates this exact layout).
        if seed is None:
            seed = int(np.random.SeedSequence().generate_state(1)[0])
        rng = np.random.default_rng(seed)
        print(f"[injector] default_demo random seed = {seed}  "
              f"(pass seed={seed} to reproduce this layout)")

        # Helper: short anomaly factory
        def hvac(day, hour, hours_long, mag, desc):
            return Anomaly(
                kind="hvac_fault",
                step_offset=day * DAY + hour * HRS,
                duration=hours_long * HRS,
                magnitude=mag,
                description=desc,
            )

        def drift(day, hour, hours_long, mag, zone, desc):
            return Anomaly(
                kind="sensor_drift",
                step_offset=day * DAY + hour * HRS,
                duration=hours_long * HRS,
                magnitude=mag, zone_idx=zone,
                description=desc,
            )

        # Pick zones that exist in the building. The demo runs on
        # OfficeMedium-multiagent (N_ZONES=18). For smaller buildings
        # the wrapper clips out-of-range writes, so this is safe.
        z_safe = [0, 6, 10, 14] if n_zones >= 15 else [0, min(1, n_zones - 1)]

        # ── Fixed value POOLS (multisets preserved; only the assignment
        #    and placement are randomised) ─────────────────────────────
        effs    = [0.60, 0.55, 0.70, 0.65, 0.50, 0.55,
                   0.50, 0.60, 0.70, 0.65, 0.60, 0.55]
        f_hours = [10, 14, 9, 13, 11, 8, 12, 15, 10, 14, 9, 13]
        f_durs  = [8, 6, 8, 8, 6, 8, 8, 6, 8, 8, 6, 8]
        d_mags  = [3.0, 4.0, 2.5, 3.5, 3.0, 4.0, 2.5, 3.5, 3.0, 4.0, 2.5, 3.5]
        d_hours = [9, 12, 7, 15, 10, 8, 11, 14, 9, 12, 7, 15]
        d_durs  = [8, 8, 6, 8, 8, 8, 6, 8, 8, 8, 6, 8]

        # Shuffle each pool independently → same values, new assignment.
        for pool in (effs, f_hours, f_durs, d_mags, d_hours, d_durs):
            rng.shuffle(pool)

        # Rough season label from the (jittered) day, for the description.
        def season_of(day):
            return ["winter", "winter", "spring", "spring", "spring",
                    "summer", "summer", "summer", "autumn", "autumn",
                    "autumn", "winter"][(day // 30) % 12]

        s = cls()
        s.seed = seed                      # remember the layout's seed
        entries = []
        # 15-day grid anchors (15, 30, … 360), each jittered ±4 days. With
        # ±4 the closest two anchors can get is 15-8 = 7 days, so spacing
        # stays ≥7 and events never overlap (max duration 8h ≪ 7 days).
        for k in range(24):
            anchor = 15 * (k + 1)
            day = int(np.clip(anchor + int(rng.integers(-4, 5)), 15, 360))
            if k % 2 == 0:                     # hvac_fault
                i = k // 2
                entries.append(hvac(
                    day, f_hours[i], f_durs[i], effs[i],
                    f"HVAC eff {int(effs[i]*100)}% — "
                    f"{season_of(day)} fault (day {day})"))
            else:                              # sensor_drift
                i = k // 2
                zone = int(rng.choice(z_safe))
                entries.append(drift(
                    day, d_hours[i], d_durs[i], d_mags[i], zone,
                    f"Zone {zone} temp sensor drifts "
                    f"+{d_mags[i]:.1f}°C (day {day})"))
        # Keep entries time-ordered (jitter never reorders, but be safe).
        entries.sort(key=lambda a: a.step_offset)
        s.entries = entries
        return s


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------

class AnomalyEnvWrapper(gym.Wrapper):
    """
    Gymnasium wrapper that injects anomalies into Sinergym observations.

    Parameters
    ----------
    env              : base Sinergym environment
    schedule         : AnomalySchedule (None → no anomalies)
    outdoor_temp_idx : obs index for outdoor temperature  (default: 3)
    zone_temp_start  : first obs index for zone temperatures (default: 9)
    n_zones          : number of thermal zones
    occ_start_idx    : first obs index for occupancy signals (or None)
    hvac_demand_idx  : obs index for facility HVAC electricity demand,
                       used by the hvac_fault injector to scale the
                       observed power. Pass None to skip obs-level
                       hvac_fault signal (info-only mode).
    meter_idx        : obs index for the total_electricity_HVAC meter
                       (paired with hvac_demand on the multi-agent
                       OfficeMedium env). Same treatment as
                       hvac_demand_idx. None → skip.
    random_seed      : RNG seed for sensor-drift random walk
    verbose          : print injection / expiry messages
    """

    # Number of steps to ramp a sensor-drift bias from 0 to its target
    # magnitude (and back to 0 when the event ends). 8 steps × 15 min =
    # 2 hours, fast enough that a 6-8 hour event holds the full drift
    # for the majority of its duration, slow enough to mimic a real
    # calibration failure (no abrupt jump).
    _DRIFT_RAMP_STEPS = 8

    # ── hvac_fault comfort-coupling constants ──────────────────────────
    # Steps over which the temperature sag ramps in once a fault starts.
    _HVAC_FAULT_RAMP_STEPS = 8
    # °C of zone sag per unit of (severity × load-gap × zone-gain). With
    # severity≤0.5, load-gap up to ~12 °C, zone-gain ~1.0, this yields up
    # to ~1.8 °C sag for a severe fault on a high-load zone, and well
    # under 0.5 °C for a mild fault on a low-load zone.
    _HVAC_FAULT_COUPLE_GAIN_DEFAULT = 0.45

    def __init__(self,
                 env: gym.Env,
                 schedule: Optional[AnomalySchedule] = None,
                 outdoor_temp_idx: int = 3,
                 zone_temp_start: int = 9,
                 n_zones: int = 1,
                 occ_start_idx: Optional[int] = None,
                 hvac_demand_idx: Optional[int] = None,
                 meter_idx: Optional[int] = None,
                 occupied_zone_indices: Optional[List[int]] = None,
                 random_seed: int = 42,
                 verbose: bool = True):

        super().__init__(env)
        self.schedule         = schedule or AnomalySchedule()
        self.outdoor_temp_idx = outdoor_temp_idx
        self.zone_temp_start  = zone_temp_start
        self.n_zones          = n_zones
        self.occ_start_idx    = occ_start_idx
        self.hvac_demand_idx  = hvac_demand_idx
        self.meter_idx        = meter_idx
        # Which zones are occupied (monitored by the detector). The
        # hvac_fault comfort sag is concentrated on these — a fault in a
        # plenum nobody monitors is neither realistic to flag nor useful.
        # Defaults to all zones if not provided.
        self.occupied_zone_indices = (
            list(occupied_zone_indices) if occupied_zone_indices is not None
            else list(range(n_zones)))
        self.verbose          = verbose
        self.rng              = np.random.default_rng(random_seed)

        self._step            = 0
        self._active: List[Anomaly]  = []
        self._history: List[Anomaly] = []
        self._hvac_efficiency         = 1.0
        self._drift_bias              = np.zeros(n_zones, dtype=np.float32)

        # ── hvac_fault comfort-coupling state ──────────────────────────
        # Steps since the currently-active hvac_fault began (for ramping
        # the temperature sag in smoothly). Reset to 0 whenever no fault
        # is active.
        self._hvac_fault_elapsed = 0
        # Per-zone under-delivery gains, DRAWN FRESH each fault event
        # (option 2: per-event random failure mode). Initialised to zero;
        # _draw_hvac_fault_zone_gains() populates them when a fault starts.
        self._hvac_fault_zone_gain = np.zeros(n_zones, dtype=np.float32)
        # Tunable coupling gain (°C sag per severity×load×zone-gain unit).
        self._hvac_fault_couple_gain = self._HVAC_FAULT_COUPLE_GAIN_DEFAULT
        # Fraction of occupied zones that are "weak" in a given event.
        self._hvac_fault_weak_frac = 0.20
        # Post-event decay state: lets the comfort sag ramp DOWN over
        # _HVAC_FAULT_RAMP_STEPS after the event ends, mirroring the
        # ramp-in. Snapshots severity so the decay uses the last active
        # value, not the current (=0) one.
        self._hvac_fault_post_decay = self._HVAC_FAULT_RAMP_STEPS
        self._hvac_fault_last_severity = 0.0

    # ------------------------------------------------------------------
    # Gymnasium API
    # ------------------------------------------------------------------

    def reset(self, **kwargs):
        self._step            = 0
        self._active          = []
        self._drift_bias      = np.zeros(self.n_zones, dtype=np.float32)
        self._hvac_efficiency = 1.0
        self._hvac_fault_elapsed = 0
        self._hvac_fault_zone_gain = np.zeros(self.n_zones, dtype=np.float32)
        self._hvac_fault_post_decay = self._HVAC_FAULT_RAMP_STEPS
        self._hvac_fault_last_severity = 0.0
        for a in self.schedule.entries:
            a.reset()
        obs, info = self.env.reset(**kwargs)
        return obs, info

    def step(self, action):
        obs, reward, terminated, truncated, info = self.env.step(action)
        self._step += 1
        self._tick_anomalies()
        obs  = self._apply_obs_anomalies(obs)
        info = self._annotate_info(info)
        return obs, reward, terminated, truncated, info

    # ------------------------------------------------------------------
    # Anomaly lifecycle
    # ------------------------------------------------------------------

    def _tick_anomalies(self):
        # 1. Activate newly due anomalies
        for a in self.schedule.entries:
            if not a._triggered and self._step >= a.step_offset:
                a._active     = True
                a._triggered  = True
                a._start_step = self._step
                self._active.append(a)
                self._history.append(a)
                # When an hvac_fault begins, draw a FRESH per-event
                # weak-zone profile (option 2: per-event random failure
                # mode). A real efficiency fault doesn't degrade every
                # zone equally — a fouled coil, a stuck damper, or a weak
                # branch starves specific zones. Each event randomly
                # selects ~35% of the OCCUPIED zones as "weak" (strong
                # sag) and leaves the rest barely affected. This produces
                # a DIFFERENTIAL temperature signature (some zones diverge
                # from their neighbours) that is both physically faithful
                # and detectable by the differential/level channels —
                # unlike a uniform sag, which is common-mode and gets
                # subtracted away.
                if a.kind == "hvac_fault":
                    self._draw_hvac_fault_zone_gains()
                if self.verbose:
                    print(f"\n  ⚠ [ANOMALY START  step={self._step:6d}] {a.description}")

        # 2. Expire finished anomalies
        still = []
        for a in self._active:
            if self._step >= a._start_step + a.duration:
                a._active = False
                if self.verbose:
                    print(f"\n  ✓ [ANOMALY END    step={self._step:6d}] {a.kind} resolved")
            else:
                still.append(a)
        self._active = still

        # 3. Recompute aggregate HVAC efficiency
        eff = 1.0
        for a in self._active:
            if a.kind == "hvac_fault":
                eff *= a.magnitude
        self._hvac_efficiency = float(eff)

        # Track how long a fault has been active, for ramping the
        # comfort-degradation sag in smoothly. When no fault is active,
        # we count UP a decay counter for symmetric ramp-OUT so the sag
        # fades over the same window it ramped in — otherwise the sag
        # snaps off at event end and produces a ~4 °C discontinuity that
        # the detector (correctly) flags as an artifact-driven FP.
        if self._hvac_efficiency < 1.0:
            self._hvac_fault_elapsed += 1
            self._hvac_fault_post_decay = 0
            # Snapshot the active fault state so we can keep applying a
            # decaying version of it after the event expires.
            self._hvac_fault_last_severity = 1.0 - self._hvac_efficiency
        else:
            self._hvac_fault_elapsed = 0
            if self._hvac_fault_post_decay < self._HVAC_FAULT_RAMP_STEPS:
                self._hvac_fault_post_decay += 1
            else:
                # Decay fully expired — clear the snapshot.
                self._hvac_fault_last_severity = 0.0

        # 4. Update sensor-drift bias (deterministic ramp + decay).
        # Previous design: random walk N(0.02, 0.005) per step, clipped
        # at ±magnitude. Problem: for short events (≤32 steps) the walk
        # rarely reached the clip, so a "3°C drift event" produced only
        # ~0.6°C of actual drift — invisible to the detector.
        #
        # New design: deterministic linear ramp from 0 to ±magnitude
        # over `_DRIFT_RAMP_STEPS` (default 8 = 2h), held thereafter.
        # When the event ends, decay to 0 over the same ramp duration.
        # Result: a "3°C drift event" produces a well-defined +3°C
        # bias by hour 2 and holds it for the rest of the event.
        active_drift_targets = {}   # zone_idx → signed magnitude target
        for a in self._active:
            if a.kind == "sensor_drift":
                targets = ([a.zone_idx] if a.zone_idx is not None
                           else range(self.n_zones))
                for z in targets:
                    if 0 <= z < self.n_zones:
                        # If multiple events target the same zone in
                        # the same step, the larger-magnitude one wins.
                        prev = active_drift_targets.get(z, 0.0)
                        if abs(a.magnitude) > abs(prev):
                            active_drift_targets[z] = float(a.magnitude)

        for z in range(self.n_zones):
            if z in active_drift_targets:
                target = active_drift_targets[z]
                # Step the bias toward the target.
                step_amt = target / float(self._DRIFT_RAMP_STEPS)
                cur      = float(self._drift_bias[z])
                if abs(target - cur) <= abs(step_amt):
                    self._drift_bias[z] = target
                else:
                    self._drift_bias[z] = cur + step_amt
                # Stay clipped at ±magnitude in case the step
                # overshot.
                self._drift_bias[z] = float(np.clip(
                    self._drift_bias[z], -abs(target), abs(target)
                ))
            else:
                # No active drift on this zone — decay to 0 at the
                # same ramp rate as injection (symmetric).
                cur = float(self._drift_bias[z])
                if cur == 0.0:
                    continue
                decay_amt = 1.0 / float(self._DRIFT_RAMP_STEPS)
                if abs(cur) <= decay_amt:
                    self._drift_bias[z] = 0.0
                else:
                    self._drift_bias[z] = cur - np.sign(cur) * decay_amt

    # ------------------------------------------------------------------
    # Observation transforms
    # ------------------------------------------------------------------

    def _draw_hvac_fault_zone_gains(self) -> None:
        """Draw a fresh weak-zone gain profile for a new hvac_fault event.

        ~`_hvac_fault_weak_frac` of the OCCUPIED zones become 'weak'
        (large under-delivery gain → strong sag); the rest get a small
        residual gain (they're still slightly affected, as a real plant
        loses a little everywhere). Plenums / unmonitored zones get zero
        — degrading a zone nobody observes is neither realistic to flag
        nor scoreable. The result is a per-event-random DIFFERENTIAL
        signature: which zones suffer changes each event, modelling a
        different failure mode (coil, damper, branch) each time.
        """
        gains = np.zeros(self.n_zones, dtype=np.float32)
        occ = list(self.occupied_zone_indices)
        if not occ:
            self._hvac_fault_zone_gain = gains
            return
        n_weak = max(1, int(round(self._hvac_fault_weak_frac * len(occ))))
        weak = self.rng.choice(occ, size=n_weak, replace=False)
        for z in occ:
            if z in weak:
                # Strong, varied: 1.5 … 2.5
                gains[z] = 1.5 + float(self.rng.random())
            else:
                # Mild residual loss: 0.1 … 0.35
                gains[z] = 0.1 + 0.25 * float(self.rng.random())
        self._hvac_fault_zone_gain = gains
        if self.verbose:
            print(f"      [hvac_fault] weak zones this event: "
                  f"{sorted(int(w) for w in weak)}")

    def _apply_obs_anomalies(self, obs: np.ndarray) -> np.ndarray:
        obs = obs.copy()
        for a in self._active:
            if a.kind == "sensor_drift":
                target = ([a.zone_idx] if a.zone_idx is not None
                          else range(self.n_zones))
                for z in target:
                    idx = self.zone_temp_start + z
                    if idx < len(obs):
                        obs[idx] = float(obs[idx]) + self._drift_bias[z]

            elif a.kind == "occ_spike" and self.occ_start_idx is not None:
                for z in range(self.n_zones):
                    idx = self.occ_start_idx + z
                    if idx < len(obs):
                        obs[idx] = 1.0

            elif a.kind == "weather_shock":
                if self.outdoor_temp_idx < len(obs):
                    elapsed  = self._step - a._start_step
                    ramp_in  = min(elapsed / 12.0, 1.0)
                    ramp_out = min((a.duration - elapsed) / 12.0, 1.0)
                    ramp     = min(ramp_in, ramp_out)
                    obs[self.outdoor_temp_idx] = (
                        float(obs[self.outdoor_temp_idx]) + a.magnitude * ramp
                    )

        # ── hvac_fault: COUPLED energy + comfort degradation. ──────────
        # Realistic model. A real efficiency fault (fouled coil, low
        # refrigerant, failing compressor) does TWO things at once:
        #   1. Burns more electricity for the thermal output it manages
        #      to deliver  → meter/demand inflated by 1/η  (as before).
        #   2. CANNOT fully meet the load → the plant under-delivers, so
        #      zones sag TOWARD the outdoor temperature (the direction
        #      the building drifts when HVAC can't keep up). This is the
        #      comfort consequence the old energy-only model omitted, and
        #      it is what makes a fault realistically detectable: a true
        #      efficiency fault degrades comfort, it doesn't hold perfect
        #      setpoint while quietly costing more.
        #
        # The temperature sag is built to be physically faithful AND not
        # gameable as a flat common-mode shift:
        #   * Proportional to severity (1 − η): η=0.7 → mild, η=0.5 → severe.
        #   * Proportional to thermal load |T_out − T_zone|: a zone with a
        #     large gap to outdoors is harder to hold, so it sags more.
        #     Zones already near outdoor temp barely move.
        #   * Direction is sign(T_out − T_zone): zones drift toward
        #     outdoors (warm up in summer, cool down in winter).
        #   * Per-zone heterogeneity (fixed random gains) so the pattern
        #     is zone-structured, not a uniform offset — this is what a
        #     real failing plant produces and what a differential/level
        #     detector can legitimately catch.
        #   * Ramped in over the same window as the efficiency change so
        #     there is no unphysical instantaneous jump.
        # Apply sag while the fault is active OR during the post-event
        # decay window. During active fault: ramp = elapsed/RAMP (rises
        # from 0 to 1). During decay: ramp = (RAMP - post_decay)/RAMP
        # (falls from 1 to 0). Severity is taken live during the active
        # phase and from the snapshot during decay.
        in_active_fault = (self._hvac_efficiency < 1.0
                           and self._hvac_efficiency > 1e-6)
        in_decay = (not in_active_fault
                    and self._hvac_fault_post_decay < self._HVAC_FAULT_RAMP_STEPS
                    and self._hvac_fault_last_severity > 0.0)

        if in_active_fault:
            # Inflate demand/meter while the fault is active.
            inflation = 1.0 / self._hvac_efficiency
            for idx in (self.hvac_demand_idx, self.meter_idx):
                if idx is not None and 0 <= idx < len(obs):
                    obs[idx] = float(obs[idx]) * inflation

        if in_active_fault or in_decay:
            if in_active_fault:
                severity = 1.0 - self._hvac_efficiency
                ramp = min(self._hvac_fault_elapsed
                           / float(self._HVAC_FAULT_RAMP_STEPS), 1.0)
            else:
                # Symmetric ramp-out: decay from 1 → 0 over RAMP steps.
                severity = self._hvac_fault_last_severity
                ramp = max(0.0,
                           (self._HVAC_FAULT_RAMP_STEPS
                            - self._hvac_fault_post_decay)
                           / float(self._HVAC_FAULT_RAMP_STEPS))
            t_out = float(obs[self.outdoor_temp_idx])
            k_couple = self._hvac_fault_couple_gain
            for z in range(self.n_zones):
                idx = self.zone_temp_start + z
                if idx >= len(obs):
                    continue
                t_zone = float(obs[idx])
                load_gap = t_out - t_zone                    # signed
                sag = (k_couple * severity * load_gap
                       * self._hvac_fault_zone_gain[z] * ramp)
                sag = float(np.clip(sag, -4.0, 4.0))
                obs[idx] = t_zone + sag

        return obs

    # ------------------------------------------------------------------
    # Info annotation
    # ------------------------------------------------------------------

    def _annotate_info(self, info: dict) -> dict:
        info = dict(info)
        info["anomalies_active"]  = [a.label for a in self._active]
        info["anomaly_kinds"]     = list({a.kind for a in self._active})
        info["hvac_efficiency"]   = self._hvac_efficiency
        info["hvac_fault_active"] = self._hvac_efficiency < 1.0
        info["sensor_drift_bias"] = self._drift_bias.tolist()
        info["anomaly_step"]      = self._step
        # Stable event identifier for each active anomaly. Used by the
        # evaluation layer to group consecutive active-steps into events
        # for event-level (not step-level) recall scoring. The id is
        # the anomaly's slot in schedule.entries, which is unique per
        # episode.
        info["anomaly_event_ids"] = [
            self.schedule.entries.index(a) for a in self._active
        ]
        return info

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def get_active_anomalies(self) -> List[Anomaly]:
        return list(self._active)

    def get_anomaly_history(self) -> List[Anomaly]:
        return list(self._history)

    def get_hvac_efficiency(self) -> float:
        return self._hvac_efficiency

    def is_kind_active(self, kind: str) -> bool:
        return any(a.kind == kind for a in self._active)