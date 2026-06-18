# register_env.py

import gymnasium as gym
import numpy as np
import sinergym
from sinergym.envs.eplus_env import EplusEnv
from sinergym.utils.rewards import LinearReward

# Optional real-world reward (peak-demand, time-of-use, occupied-only comfort).
# Lives in real_world_reward.py at the same level as this file.
try:
    from real_world_reward import RealWorldCostReward
    _HAS_REAL_WORLD = True
except ImportError:
    RealWorldCostReward = None
    _HAS_REAL_WORLD = False


# =============================================================================
# OFFICE MEDIUM — ZONE CONFIGURATION
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

ALL_ZONES = OCCUPIED_ZONES + [
    "FirstFloor_Plenum", "MidFloor_Plenum", "TopFloor_Plenum"
]

N_OCCUPIED = len(OCCUPIED_ZONES)  # 15
N_ALL = len(ALL_ZONES)            # 18

# Action bounds with guaranteed 2°C deadband
HTG_LOW = 15.0
HTG_HIGH = 22.0
CLG_LOW = 24.0
CLG_HIGH = 30.0

WEATHER_FILES = {
    "hot":   "USA_AZ_Davis-Monthan.AFB.722745_TMY3.epw",
    "mixed": "USA_NY_New.York-J.F.Kennedy.Intl.AP.744860_TMY3.epw",
    "cool":  "USA_WA_Port.Angeles-William.R.Fairchild.Intl.AP.727885_TMY3.epw",
}


# =============================================================================
# REWARD HELPER — pick reward class + kwargs based on flags
# =============================================================================
def _build_reward(temperature_vars, energy_vars,
                  real_world: bool,
                  lambda_temperature: float = 1.0,
                  # Real-world weights (only used when real_world=True)
                  energy_weight:  float = 0.20,
                  comfort_weight: float = 0.40,
                  peak_weight:    float = 0.30,
                  tou_weight:     float = 0.10,
                  occupied_only_comfort: bool  = True,
                  unocc_floor:           float = 0.0,
                  peak_reset_period:     str   = 'monthly'):
    """
    Returns (reward_class, reward_kwargs) ready to pass to EplusEnv.
    """
    common_kwargs = {
        "temperature_variables": temperature_vars,
        "energy_variables":      energy_vars,
        "range_comfort_winter":  (20.0, 23.5),
        "range_comfort_summer":  (23.0, 26.0),
        "summer_start":          (6, 1),
        "summer_final":          (9, 30),
    }

    if real_world:
        if not _HAS_REAL_WORLD:
            raise ImportError(
                "real_world=True but `real_world_reward.py` not importable. "
                "Place it next to register_env.py."
            )
        kwargs = {
            **common_kwargs,
            "energy_weight":         energy_weight,
            "comfort_weight":        comfort_weight,
            "peak_weight":           peak_weight,
            "tou_weight":            tou_weight,
            "lambda_energy":         1.0e-4,
            "lambda_temperature":    lambda_temperature,
            "lambda_peak":           1.0e-4,
            "lambda_tou":            1.0e-4,
            "occupied_only_comfort": occupied_only_comfort,
            "unocc_floor":           unocc_floor,
            "peak_reset_period":     peak_reset_period,
        }
        return RealWorldCostReward, kwargs

    # Default sinergym LinearReward (original behaviour)
    kwargs = {
        **common_kwargs,
        "energy_weight":      0.5,
        "lambda_energy":      1.0e-4,
        "lambda_temperature": lambda_temperature,
    }
    return LinearReward, kwargs


# =============================================================================
# OFFICE MEDIUM — VARIABLES & ACTUATORS  (original 3-AHU model)
# =============================================================================
def build_variables_dict():
    variables = {}

    # Outdoor (6 vars including wind_direction)
    outdoor_vars = [
        ("outdoor_temperature", "Site Outdoor Air DryBulb Temperature", "Environment"),
        ("outdoor_humidity", "Site Outdoor Air Relative Humidity", "Environment"),
        ("wind_speed", "Site Wind Speed", "Environment"),
        ("wind_direction", "Site Wind Direction", "Environment"),
        ("diffuse_solar_radiation", "Site Diffuse Solar Radiation Rate per Area", "Environment"),
        ("direct_solar_radiation", "Site Direct Solar Radiation Rate per Area", "Environment"),
    ]
    for var_name, ep_var, key in outdoor_vars:
        variables[var_name] = (ep_var, key)

    # Zone air temperatures (18 zones)
    for zone in ALL_ZONES:
        variables[f"air_temperature_{zone}"] = ("Zone Air Temperature", zone)

    # Zone air humidity (18 zones)
    for zone in ALL_ZONES:
        variables[f"air_humidity_{zone}"] = ("Zone Air Relative Humidity", zone)

    # Zone occupancy (15 zones)
    for zone in OCCUPIED_ZONES:
        variables[f"people_occupant_{zone}"] = ("Zone People Occupant Count", zone)

    # Setpoints PAIRED per zone (htg + clg together, NOT grouped separately)
    for zone in OCCUPIED_ZONES:
        variables[f"htg_setpoint_{zone}"] = ("Zone Thermostat Heating Setpoint Temperature", zone)
        variables[f"clg_setpoint_{zone}"] = ("Zone Thermostat Cooling Setpoint Temperature", zone)

    # HVAC demand
    variables["HVAC_electricity_demand_rate"] = (
        "Facility Total HVAC Electricity Demand Rate", "Whole Building"
    )

    return variables


def build_actuators_dict():
    actuators = {}
    for zone in OCCUPIED_ZONES:
        actuators[f"htg_{zone}"] = (
            "Schedule:Compact", "Schedule Value", f"HTGSETP_SCH_{zone.upper()}"
        )
    for zone in OCCUPIED_ZONES:
        actuators[f"clg_{zone}"] = (
            "Schedule:Compact", "Schedule Value", f"CLGSETP_SCH_{zone.upper()}"
        )
    return actuators


# =============================================================================
# OFFICE MEDIUM — ENVIRONMENT FACTORY (original 3-AHU model)
# =============================================================================
def make_custom_env(weather="mixed", real_world: bool = False,
                    # Real-world reward tuning (ignored if real_world=False)
                    energy_weight:  float = 0.20,
                    comfort_weight: float = 0.40,
                    peak_weight:    float = 0.30,
                    tou_weight:     float = 0.10,
                    occupied_only_comfort: bool  = True,
                    unocc_floor:           float = 0.0,
                    peak_reset_period:     str   = 'monthly'):
    """
    Create the OfficeMedium multi-agent environment (3 PVAV systems, shared
    cooling/fan per floor — the original DOE reference building).
    """
    temp_vars   = [f"air_temperature_{zone}" for zone in OCCUPIED_ZONES]
    energy_vars = ["HVAC_electricity_demand_rate"]
    reward_cls, reward_kwargs = _build_reward(
        temp_vars, energy_vars,
        real_world=real_world,
        lambda_temperature=1.0,
        energy_weight=energy_weight,
        comfort_weight=comfort_weight,
        peak_weight=peak_weight,
        tou_weight=tou_weight,
        occupied_only_comfort=occupied_only_comfort,
        unocc_floor=unocc_floor,
        peak_reset_period=peak_reset_period,
    )

    env = EplusEnv(
        building_file="ASHRAE901_OfficeMedium_STD2019_Denver_MultiAgent.epJSON",
        weather_files=WEATHER_FILES[weather],
        time_variables=["month", "day_of_month", "hour"],
        variables=build_variables_dict(),
        meters={"total_electricity_HVAC": "Electricity:HVAC"},
        actuators=build_actuators_dict(),
        action_space=gym.spaces.Box(
            low=np.array([HTG_LOW] * N_OCCUPIED + [CLG_LOW] * N_OCCUPIED, dtype=np.float32),
            high=np.array([HTG_HIGH] * N_OCCUPIED + [CLG_HIGH] * N_OCCUPIED, dtype=np.float32),
            shape=(N_OCCUPIED * 2,),
            dtype=np.float32,
        ),
        reward=reward_cls,
        reward_kwargs=reward_kwargs,
    )
    return env


# =============================================================================
# OFFICE MEDIUM 15-PTHP — VARIABLES & ACTUATORS
# =============================================================================
# Layout differences vs the original 3-AHU model:
#   * 15 independent PTHPs replace the 3 floor-level AHUs
#   * Per-PTHP electricity rate is exposed as an observation, so the agent
#     can attribute energy to each zone.
#   * Plenums (FirstFloor_Plenum / MidFloor_Plenum / TopFloor_Plenum) remain
#     as geometric zones (their surfaces still matter), so they still appear
#     in air_temperature_<zone> and air_humidity_<zone>.
#   * Actuator schedule names are preserved in their original case in the
#     epJSON (HtgSetp_Sch_<zone> / ClgSetp_Sch_<zone>). EnergyPlus itself is
#     case-insensitive but Sinergym's actuator dict is treated as a literal
#     lookup, so we match the case in the epJSON.
def build_variables_dict_15pthp():
    variables = {}

    # Outdoor (6 vars)
    outdoor_vars = [
        ("outdoor_temperature", "Site Outdoor Air DryBulb Temperature", "Environment"),
        ("outdoor_humidity", "Site Outdoor Air Relative Humidity", "Environment"),
        ("wind_speed", "Site Wind Speed", "Environment"),
        ("wind_direction", "Site Wind Direction", "Environment"),
        ("diffuse_solar_radiation", "Site Diffuse Solar Radiation Rate per Area", "Environment"),
        ("direct_solar_radiation", "Site Direct Solar Radiation Rate per Area", "Environment"),
    ]
    for var_name, ep_var, key in outdoor_vars:
        variables[var_name] = (ep_var, key)

    # Zone air temperatures (18 zones — plenums kept as geometry)
    for zone in ALL_ZONES:
        variables[f"air_temperature_{zone}"] = ("Zone Air Temperature", zone)

    # Zone air humidity (18 zones)
    for zone in ALL_ZONES:
        variables[f"air_humidity_{zone}"] = ("Zone Air Relative Humidity", zone)

    # Zone occupancy (15 zones)
    for zone in OCCUPIED_ZONES:
        variables[f"people_occupant_{zone}"] = ("Zone People Occupant Count", zone)

    # Setpoints paired per zone
    for zone in OCCUPIED_ZONES:
        variables[f"htg_setpoint_{zone}"] = ("Zone Thermostat Heating Setpoint Temperature", zone)
        variables[f"clg_setpoint_{zone}"] = ("Zone Thermostat Cooling Setpoint Temperature", zone)

    # NEW: per-PTHP electricity rate (true per-zone energy attribution)
    # Object key is "<Zone> PTHP" — matches the names used in the epJSON.
    for zone in OCCUPIED_ZONES:
        variables[f"pthp_electricity_rate_{zone}"] = (
            "Zone Packaged Terminal Heat Pump Electricity Rate", f"{zone} PTHP"
        )

    # Whole-building HVAC demand (kept for backward compatibility)
    variables["HVAC_electricity_demand_rate"] = (
        "Facility Total HVAC Electricity Demand Rate", "Whole Building"
    )

    return variables


def build_actuators_dict_15pthp():
    """
    Schedule names match the existing per-zone thermostat schedules in the
    converted epJSON:  HtgSetp_Sch_<zone> / ClgSetp_Sch_<zone>.
    (Original case preserved; EnergyPlus internally is case-insensitive.)
    """
    actuators = {}
    for zone in OCCUPIED_ZONES:
        actuators[f"htg_{zone}"] = (
            "Schedule:Compact", "Schedule Value", f"HtgSetp_Sch_{zone}"
        )
    for zone in OCCUPIED_ZONES:
        actuators[f"clg_{zone}"] = (
            "Schedule:Compact", "Schedule Value", f"ClgSetp_Sch_{zone}"
        )
    return actuators


# =============================================================================
# OFFICE MEDIUM 15-PTHP — ENVIRONMENT FACTORY
# =============================================================================
def make_custom_env_15pthp(weather="mixed", real_world: bool = False,
                           # Real-world reward tuning (ignored if real_world=False)
                           energy_weight:  float = 0.20,
                           comfort_weight: float = 0.40,
                           peak_weight:    float = 0.30,
                           tou_weight:     float = 0.10,
                           occupied_only_comfort: bool  = True,
                           unocc_floor:           float = 0.0,
                           peak_reset_period:     str   = 'monthly'):
    """
    Create the OfficeMedium 15-PTHP environment: one independent packaged
    terminal heat pump per occupied zone (15 PTHPs total). Plenums remain
    as unconditioned geometric zones.

    Action space and bounds are identical to the original 3-AHU env, so
    existing agents (e.g. PyMDPOffice) can drive this env with no changes
    to the action vector. The big differences are physical, not API-level:

      * Per-zone actions now correspond to physically independent equipment.
        Two zones on the same floor can no longer fight each other through
        a shared AHU.
      * The reward still uses `HVAC_electricity_demand_rate` (whole-building
        total) for backwards compatibility, but per-PTHP electricity rates
        are now exposed as observations
        ('pthp_electricity_rate_<zone>') so agents can do zone-level energy
        attribution.

    Args mirror `make_custom_env`; see that function for parameter meanings.
    """
    temp_vars   = [f"air_temperature_{zone}" for zone in OCCUPIED_ZONES]
    energy_vars = ["HVAC_electricity_demand_rate"]
    reward_cls, reward_kwargs = _build_reward(
        temp_vars, energy_vars,
        real_world=real_world,
        lambda_temperature=1.0,
        energy_weight=energy_weight,
        comfort_weight=comfort_weight,
        peak_weight=peak_weight,
        tou_weight=tou_weight,
        occupied_only_comfort=occupied_only_comfort,
        unocc_floor=unocc_floor,
        peak_reset_period=peak_reset_period,
    )

    env = EplusEnv(
        building_file="ASHRAE901_OfficeMedium_STD2019_Denver_15PTHP.epJSON",
        weather_files=WEATHER_FILES[weather],
        time_variables=["month", "day_of_month", "hour"],
        variables=build_variables_dict_15pthp(),
        meters={"total_electricity_HVAC": "Electricity:HVAC"},
        actuators=build_actuators_dict_15pthp(),
        action_space=gym.spaces.Box(
            low=np.array([HTG_LOW] * N_OCCUPIED + [CLG_LOW] * N_OCCUPIED, dtype=np.float32),
            high=np.array([HTG_HIGH] * N_OCCUPIED + [CLG_HIGH] * N_OCCUPIED, dtype=np.float32),
            shape=(N_OCCUPIED * 2,),
            dtype=np.float32,
        ),
        reward=reward_cls,
        reward_kwargs=reward_kwargs,
    )
    return env


# =============================================================================
# 5ZONE — ZONE CONFIGURATION
# =============================================================================
ZONES_5 = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]
N_ZONES_5 = len(ZONES_5)  # 5

# Action bounds — same comfortable deadband
HTG_LOW_5  = 12.0
HTG_HIGH_5 = 23.25
CLG_LOW_5  = 23.25
CLG_HIGH_5 = 30.0


# =============================================================================
# 5ZONE — VARIABLES & ACTUATORS
# =============================================================================
def build_variables_dict_5zone():
    variables = {}

    # Outdoor (6 vars)
    outdoor_vars = [
        ("outdoor_temperature",    "Site Outdoor Air DryBulb Temperature",       "Environment"),
        ("outdoor_humidity",       "Site Outdoor Air Relative Humidity",          "Environment"),
        ("wind_speed",             "Site Wind Speed",                             "Environment"),
        ("wind_direction",         "Site Wind Direction",                         "Environment"),
        ("diffuse_solar_radiation","Site Diffuse Solar Radiation Rate per Area",  "Environment"),
        ("direct_solar_radiation", "Site Direct Solar Radiation Rate per Area",   "Environment"),
    ]
    for var_name, ep_var, key in outdoor_vars:
        variables[var_name] = (ep_var, key)

    # Per-zone: temperature, humidity, occupancy, setpoints
    for zone in ZONES_5:
        safe = zone.replace("-", "_")
        variables[f"air_temperature_{safe}"]  = ("Zone Air Temperature",                          zone)
        variables[f"air_humidity_{safe}"]     = ("Zone Air Relative Humidity",                    zone)
        variables[f"people_occupant_{safe}"]  = ("Zone People Occupant Count",                    zone)
        variables[f"htg_setpoint_{safe}"]     = ("Zone Thermostat Heating Setpoint Temperature",  zone)
        variables[f"clg_setpoint_{safe}"]     = ("Zone Thermostat Cooling Setpoint Temperature",  zone)

    # CO2 and HVAC demand
    variables["co2_emission"] = (
        "Environmental Impact Total CO2 Emissions Carbon Equivalent Mass", "site"
    )
    variables["HVAC_electricity_demand_rate"] = (
        "Facility Total HVAC Electricity Demand Rate", "Whole Building"
    )

    return variables


def build_actuators_dict_5zone():
    """
    Actuator names must match the Schedule:Compact entries in
    5ZoneAutoDXVAV_MultiAgent.epJSON: HTG-SETP-SCH-SPACE1-1, etc.
    """
    actuators = {}
    # All heating first, then all cooling — matches action vector layout
    for zone in ZONES_5:
        safe = zone.replace("-", "_")
        actuators[f"htg_{safe}"] = (
            "Schedule:Compact", "Schedule Value", f"HTG-SETP-SCH-{zone}"
        )
    for zone in ZONES_5:
        safe = zone.replace("-", "_")
        actuators[f"clg_{safe}"] = (
            "Schedule:Compact", "Schedule Value", f"CLG-SETP-SCH-{zone}"
        )
    return actuators


# =============================================================================
# 5ZONE — ENVIRONMENT FACTORY
# =============================================================================
def make_custom_env_5zone(weather="mixed", comfort_zones: int = 5,
                          real_world: bool = False,
                          energy_weight:  float = 0.20,
                          comfort_weight: float = 0.40,
                          peak_weight:    float = 0.30,
                          tou_weight:     float = 0.10,
                          occupied_only_comfort: bool  = True,
                          unocc_floor:           float = 0.0,
                          peak_reset_period:     str   = 'monthly'):
    """
    Create the 5ZoneAutoDXVAV multi-agent environment.

    comfort_zones: how many zones contribute to the reward's comfort term.
        1 → only SPACE5-1 (core zone, matches original single-agent env)
        5 → all 5 zones (default multiagent setting)

    real_world: if True, use RealWorldCostReward (peak-demand + TOU +
        occupied-only comfort). Otherwise the default sinergym LinearReward.

    Observation vector (37 total):
      [0-2]   month, day_of_month, hour
      [3-8]   outdoor_temperature, outdoor_humidity, wind_speed,
              wind_direction, diffuse_solar_radiation, direct_solar_radiation
      [9-13]  air_temperature  SPACE1-1 … SPACE5-1   (interleaved, base=9+i*5)
      [14-18] air_humidity
      [19-23] people_occupant
      [24-28] htg_setpoint
      [29-33] clg_setpoint
      [34]    co2_emission
      [35]    HVAC_electricity_demand_rate
      [36]    total_electricity_HVAC  (meter)

    Action vector (10-dim):
      [0-4]   htg setpoints  SPACE1-1 … SPACE5-1
      [5-9]   clg setpoints  SPACE1-1 … SPACE5-1
    """
    if comfort_zones not in (1, 5):
        raise ValueError(f"comfort_zones must be 1 or 5, got {comfort_zones}")

    # comfort_zones=1 → only SPACE5-1 (last zone, index 4), like original
    # comfort_zones=5 → all zones
    reward_zones = ZONES_5[-comfort_zones:]   # last N zones; for 1: ['SPACE5-1']

    temp_vars   = [f"air_temperature_{z.replace('-', '_')}" for z in reward_zones]
    energy_vars = ["HVAC_electricity_demand_rate"]

    reward_cls, reward_kwargs = _build_reward(
        temp_vars, energy_vars,
        real_world=real_world,
        # Normalise by zone count so the comfort magnitude is comparable
        # across the comfort_zones=1 and =5 setups (same as before).
        lambda_temperature=1.0 / comfort_zones,
        energy_weight=energy_weight,
        comfort_weight=comfort_weight,
        peak_weight=peak_weight,
        tou_weight=tou_weight,
        occupied_only_comfort=occupied_only_comfort,
        unocc_floor=unocc_floor,
        peak_reset_period=peak_reset_period,
    )

    env = EplusEnv(
        building_file="5ZoneAutoDXVAV_MultiAgent.epJSON",
        weather_files=WEATHER_FILES[weather],
        time_variables=["month", "day_of_month", "hour"],
        variables=build_variables_dict_5zone(),
        meters={"total_electricity_HVAC": "Electricity:HVAC"},
        actuators=build_actuators_dict_5zone(),
        action_space=gym.spaces.Box(
            low=np.array(
                [HTG_LOW_5]  * N_ZONES_5 + [CLG_LOW_5]  * N_ZONES_5,
                dtype=np.float32
            ),
            high=np.array(
                [HTG_HIGH_5] * N_ZONES_5 + [CLG_HIGH_5] * N_ZONES_5,
                dtype=np.float32
            ),
            shape=(N_ZONES_5 * 2,),
            dtype=np.float32,
        ),
        reward=reward_cls,
        reward_kwargs=reward_kwargs,
    )
    return env