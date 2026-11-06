# SPDX-License-Identifier: Apache-2.0
"""The building: three rooms, an office week, and three faults on a Tuesday.

Every reading is worked out from the seed, the room and the time alone, so the
same call gives the same reading on any machine and in any order: the healthy
week `train.py` fits on, the Tuesday `publish.py` plays and the tests all see
one building. Time is simulated and in UTC, so where the reader is does not
move the office hours.
"""

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np

ROOMS = ("meeting-room", "open-office", "kitchen")
TITLES = {"meeting-room": "Meeting room", "open-office": "Open office", "kitchen": "Kitchen"}
QUANTITIES = ("co2", "temperature", "humidity", "pm25")
UNITS = {"co2": "ppm", "temperature": "°C", "humidity": "%", "pm25": "µg/m³"}

#: A reading every five simulated minutes, per room.
STEP_S = 300
DAY_S = 86400
#: Monday 2 March 2026, 00:00 UTC: the start of the healthy week `train.py` fits on.
WEEK = dt.datetime(2026, 3, 2, tzinfo=dt.timezone.utc).timestamp()
#: The next Tuesday, which `publish.py` plays, faults and all.
TUESDAY = WEEK + 8 * DAY_S
SEED = 2026

#: When each room is in use on a weekday, in hours of the day.
IN_USE = {
    "meeting-room": ((10.0, 11.0), (14.0, 16.0)),
    "open-office": ((9.0, 17.0),),
    "kitchen": ((12.0, 13.5),),
}
#: How much each quantity rises in a full room, and its sensor's noise.
RISE = {
    "meeting-room": {"co2": 900.0, "temperature": 1.8, "humidity": 8.0, "pm25": 1.0},
    "open-office": {"co2": 500.0, "temperature": 1.5, "humidity": 6.0, "pm25": 2.0},
    "kitchen": {"co2": 300.0, "temperature": 2.0, "humidity": 10.0, "pm25": 20.0},
}
BASE = {"co2": 420.0, "temperature": 20.5, "humidity": 40.0, "pm25": 5.0}
NOISE = {"co2": 15.0, "temperature": 0.15, "humidity": 0.8, "pm25": 1.0}


@dataclass(frozen=True)
class Fault:
    """Something that goes wrong in one room, and the label it should get."""

    room: str
    kind: str
    start: float
    end: float
    what: str


FAULTS = (
    Fault(
        "meeting-room",
        "window_open",
        TUESDAY + 10.25 * 3600,
        TUESDAY + 11 * 3600,
        "a window left open during the morning meeting",
    ),
    Fault(
        "open-office",
        "ventilation_fault",
        TUESDAY + 13 * 3600,
        TUESDAY + 16 * 3600,
        "the ventilation stops after lunch",
    ),
    Fault(
        "kitchen",
        "sensor_fault",
        TUESDAY + 15 * 3600,
        TUESDAY + 18 * 3600,
        "the humidity sensor sticks at 99 %",
    ),
)


def _rng(seed: int, room: str, ts: float, stream: int) -> np.random.Generator:
    """The random numbers for one room at one moment, the same whoever asks."""
    step = int((ts - WEEK) // STEP_S)
    return np.random.default_rng([seed, ROOMS.index(room), step, stream])


def _ramp(hour: float, start: float, end: float) -> float:
    """1 between ``start`` and ``end``, rising and falling over a quarter of an hour."""
    width = 0.25
    return float(np.clip(min((hour - start) / width + 0.5, (end - hour) / width + 0.5), 0.0, 1.0))


def occupancy(room: str, ts: float, seed: int = SEED) -> float:
    """How full ``room`` is at ``ts``, from 0 to 1: nobody at night or at the weekend."""
    day = int((ts - WEEK) // DAY_S)
    if day % 7 >= 5:
        return 0.0
    hour = ((ts - WEEK) % DAY_S) / 3600
    # Some days more people come than others.
    attendance = 0.85 + 0.15 * float(
        np.random.default_rng([seed, ROOMS.index(room), day, 9]).random()
    )
    return attendance * max(_ramp(hour, start, end) for start, end in IN_USE[room])


def fault_at(room: str, ts: float) -> Fault | None:
    """The fault ``room`` has at ``ts``, if any."""
    return next((f for f in FAULTS if f.room == room and f.start <= ts < f.end), None)


def reading(room: str, ts: float, *, seed: int = SEED, faults: bool = True) -> dict[str, Any]:
    """What the room's sensors say at ``ts``."""
    full = occupancy(room, ts, seed)
    noise = _rng(seed, room, ts, 1).normal(size=len(QUANTITIES))
    values = {
        q: BASE[q] + RISE[room][q] * full + NOISE[q] * n
        for q, n in zip(QUANTITIES, noise, strict=True)
    }
    fault = fault_at(room, ts) if faults else None
    if fault is not None and fault.kind == "window_open":
        values["co2"] = BASE["co2"] + 10 + NOISE["co2"] * noise[0]
        values["temperature"] -= 3.5
        values["humidity"] -= 6.0
    elif fault is not None and fault.kind == "ventilation_fault":
        progress = (ts - fault.start) / (fault.end - fault.start)
        values["co2"] += 1200 * min(1.0, 2 * progress)
        values["temperature"] += 1.0
    elif fault is not None and fault.kind == "sensor_fault":
        values["humidity"] = 99.0
    return {
        "device": room,
        "ts": ts,
        "co2": round(values["co2"]),
        "temperature": round(values["temperature"], 1),
        "humidity": round(values["humidity"], 1),
        "pm25": round(max(0.0, values["pm25"]), 1),
    }


def readings(
    start: float, end: float, *, seed: int = SEED, faults: bool = True
) -> Iterator[dict[str, Any]]:
    """Every room's reading, each step from ``start`` up to ``end``."""
    for ts in np.arange(start, end, STEP_S):
        for room in ROOMS:
            yield reading(room, float(ts), seed=seed, faults=faults)
