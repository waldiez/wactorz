# register_env.py

import gymnasium as gym
import numpy as np
import sinergym
from sinergym.envs.eplus_env import EplusEnv
from sinergym.utils.rewards import LinearReward

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
# OFFICE MEDIUM — VARIABLES & ACTUATORS
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
# OFFICE MEDIUM — ENVIRONMENT FACTORY
# =============================================================================
def make_custom_env(weather="mixed"):
    """
    Create the OfficeMedium multi-agent environment.

    Args:
        weather: 'hot', 'mixed', or 'cool'
    """
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
        reward=LinearReward,
        reward_kwargs={
            "temperature_variables": [f"air_temperature_{zone}" for zone in OCCUPIED_ZONES],
            "energy_variables": ["HVAC_electricity_demand_rate"],
            "range_comfort_winter": (20.0, 23.5),
            "range_comfort_summer": (23.0, 26.0),
            "summer_start": (6, 1),
            "summer_final": (9, 30),
            "energy_weight": 0.5,
            "lambda_energy": 1.0e-4,
            "lambda_temperature": 1.0,
        },
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
def make_custom_env_5zone(weather="mixed"):
    """
    Create the 5ZoneAutoDXVAV multi-agent environment.

    Observation vector (37 total):
      [0-2]   month, day_of_month, hour
      [3-8]   outdoor_temperature, outdoor_humidity, wind_speed,
              wind_direction, diffuse_solar_radiation, direct_solar_radiation
      [9-13]  air_temperature  SPACE1-1 … SPACE5-1
      [14-18] air_humidity     SPACE1-1 … SPACE5-1
      [19-23] people_occupant  SPACE1-1 … SPACE5-1
      [24-28] htg_setpoint     SPACE1-1 … SPACE5-1
      [29-33] clg_setpoint     SPACE1-1 … SPACE5-1
      [34]    co2_emission
      [35]    HVAC_electricity_demand_rate
      [36]    total_electricity_HVAC  (meter)

    Action vector (10-dim):
      [0-4]   htg setpoints  SPACE1-1 … SPACE5-1
      [5-9]   clg setpoints  SPACE1-1 … SPACE5-1

    Args:
        weather: 'hot', 'mixed', or 'cool'
    """
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
        reward=LinearReward,
        reward_kwargs={
            "temperature_variables": [
                f"air_temperature_{z.replace('-', '_')}" for z in ZONES_5
            ],
            "energy_variables": ["HVAC_electricity_demand_rate"],
            "range_comfort_winter": (20.0, 23.5),
            "range_comfort_summer": (23.0, 26.0),
            "summer_start": (6, 1),
            "summer_final": (9, 30),
            "energy_weight": 0.5,
            "lambda_energy": 1.0e-4,
            "lambda_temperature": 1.0,
        },
    )
    return env