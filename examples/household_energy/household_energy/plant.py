# SPDX-License-Identifier: Apache-2.0
"""The house: one power meter, a fridge, and four appliances that switch on and off.

Every reading is worked out from the seed and the time alone, so the same call
gives the same reading on any machine and in any order: the week `train.py`
fits on, the Saturday `publish.py` plays and the tests all see one house. Time
is simulated and in UTC, so where the reader is does not move breakfast.

The fridge is part of the base load: it steps by far less than any appliance,
so the agent that finds runs passes over it, and nobody is asked about it.
Runs never overlap, which keeps finding them a matter of a step up and the
step back down.
"""

import datetime as dt
import functools
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np

#: The one meter's name, which is the source every label is written against.
METER = "meter"
#: What the person may be told an appliance is called.
TITLES = {
    "kettle": "Kettle",
    "microwave": "Microwave",
    "washer": "Washing machine",
    "oven": "Oven",
}
APPLIANCES = tuple(TITLES)

#: A reading every simulated minute.
STEP_S = 60
DAY_S = 86400
#: Monday 2 March 2026, 00:00 UTC: the start of the week `train.py` fits on,
#: whose runs someone noted down.
WEEK = dt.datetime(2026, 3, 2, tzinfo=dt.timezone.utc).timestamp()
#: The Saturday after, which `publish.py` plays.
SATURDAY = WEEK + 12 * DAY_S
SEED = 2026

#: The house when nothing is on, the fridge's compressor, and the meter's noise, in watts.
BASE_W = 150.0
FRIDGE_W = 90.0
NOISE_W = 10.0
#: The fridge runs for the first part of every hour.
FRIDGE_ON_MIN = 20

#: Each appliance's draw (watts: usual, spread) and how long it runs (whole minutes: least, most).
DRAW = {
    "kettle": (2200.0, 120.0),
    "microwave": (1100.0, 80.0),
    "washer": (2000.0, 100.0),
    "oven": (2450.0, 100.0),
}
LENGTH = {"kettle": (2, 4), "microwave": (2, 6), "washer": (15, 25), "oven": (30, 50)}
#: How many runs of each a day of the noted week has: least, most.
PER_DAY = {"kettle": (3, 5), "microwave": (1, 3), "washer": (0, 1), "oven": (0, 1)}


@dataclass(frozen=True)
class Run:
    """One appliance switched on, and what it drew while it was."""

    appliance: str
    start: float
    minutes: int
    watts: float

    @property
    def end(self) -> float:
        return self.start + self.minutes * STEP_S


def _at(day: float, hour: int, minute: int) -> float:
    return day + hour * 3600 + minute * 60


#: The Saturday's runs, as written down. Most draw what each appliance usually
#: does; the kettle at 11:15 draws less and the oven at 17:30 less again, which
#: is near what the washing machine and the kettle draw: watts alone mistake
#: them, how long they ran does not.
PLAYED = (
    Run("kettle", _at(SATURDAY, 7, 5), 3, 2210.0),
    Run("microwave", _at(SATURDAY, 7, 20), 3, 1090.0),
    Run("kettle", _at(SATURDAY, 8, 10), 2, 2250.0),
    Run("washer", _at(SATURDAY, 9, 30), 20, 1990.0),
    Run("kettle", _at(SATURDAY, 11, 15), 4, 2060.0),
    Run("microwave", _at(SATURDAY, 12, 30), 5, 1130.0),
    Run("oven", _at(SATURDAY, 13, 0), 40, 2460.0),
    Run("kettle", _at(SATURDAY, 15, 40), 3, 2190.0),
    Run("oven", _at(SATURDAY, 17, 30), 45, 2300.0),
    Run("microwave", _at(SATURDAY, 19, 10), 2, 1080.0),
    Run("kettle", _at(SATURDAY, 21, 30), 3, 2230.0),
)


# Kept, since every reading of a day asks for the same day's runs.
@functools.lru_cache(maxsize=32)
def day_runs(day: float, seed: int = SEED) -> tuple[Run, ...]:
    """The runs of the day starting at ``day``: the Saturday's as written, any other drawn from the seed."""
    if day == SATURDAY:
        return PLAYED
    rng = np.random.default_rng([seed, int((day - WEEK) // DAY_S)])
    runs: list[Run] = []
    for appliance in APPLIANCES:
        low, high = PER_DAY[appliance]
        for _ in range(int(rng.integers(low, high + 1))):
            usual, spread = DRAW[appliance]
            shortest, longest = LENGTH[appliance]
            minutes = int(rng.integers(shortest, longest + 1))
            # Somewhere between 06:00 and 23:00 that keeps clear of every other run.
            for _attempt in range(100):
                start = day + 360 * 60 + int(rng.integers(0, 17 * 60 - minutes)) * 60
                run = Run(appliance, float(start), minutes, float(rng.normal(usual, spread)))
                if all(run.end + 600 <= r.start or r.end + 600 <= run.start for r in runs):
                    runs.append(run)
                    break
    return tuple(sorted(runs, key=lambda r: r.start))


def run_at(ts: float, seed: int = SEED) -> Run | None:
    """The run on at ``ts``, if any."""
    day = WEEK + ((ts - WEEK) // DAY_S) * DAY_S
    return next((r for r in day_runs(day, seed) if r.start <= ts < r.end), None)


def watts(ts: float, seed: int = SEED) -> float:
    """What the meter reads at ``ts``."""
    step = int((ts - WEEK) // STEP_S)
    noise = float(np.random.default_rng([seed, step, 1]).normal(0.0, NOISE_W))
    fridge = FRIDGE_W if (step % 60) < FRIDGE_ON_MIN else 0.0
    run = run_at(ts, seed)
    return BASE_W + fridge + (run.watts if run else 0.0) + noise


def reading(ts: float, seed: int = SEED) -> dict[str, Any]:
    """The meter's reading at ``ts``, as it is published."""
    return {"device": METER, "ts": ts, "watts": round(watts(ts, seed))}


def readings(start: float, end: float, seed: int = SEED) -> Iterator[dict[str, Any]]:
    """The meter's reading each minute from ``start`` up to ``end``."""
    for ts in np.arange(start, end, STEP_S):
        yield reading(float(ts), seed)
