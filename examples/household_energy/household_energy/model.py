# SPDX-License-Identifier: Apache-2.0
"""A run in the meter's readings, and which appliance it looks like.

`RunFinder` takes the readings one at a time and says when a run has ended:
the meter stepped up by more than any fridge does, then came back down. A run
is described by its features: how many watts it added and how many minutes it
lasted.

The model is, for each appliance, the mean and the spread of each feature over
the runs of a week someone noted down. A run is the appliance whose usual run
is nearest, counting each feature in that appliance's own spreads. Which
features a version looks at is the version's own: v1 looks at the watts
alone, and a kettle drawing a little less is then a washing machine; v2 looks
at how long it ran too. Kept as plain arrays in an ``.npz``, so loading it runs
nothing from the file.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .plant import METER, STEP_S

#: The name the model is stored under, and the agent names it by.
MODEL_NAME = "appliances"
#: What a run can be described by, and what v1 looks at.
FEATURES = ("watts", "minutes")
FIRST_FEATURES = ("watts",)
#: A step up past this, in watts, starts a run: far more than the fridge's.
STEP_W = 400.0
#: The least spread a feature is given, so an appliance seen a few times alike
#: does not make every run unlike it.
FLOOR = {"watts": 40.0, "minutes": 0.5}


@dataclass
class RunFinder:
    """Follows the meter, and gives back each run once it has ended."""

    #: What the house draws with nothing on, from the last reading outside a run.
    base: float | None = None
    start: float | None = None
    during: list[float] = field(default_factory=list)

    def take(self, reading: Mapping[str, Any]) -> dict[str, Any] | None:
        """Take one reading; return the run it ended, if it ended one."""
        ts, now = float(reading["ts"]), float(reading["watts"])
        if self.base is None:
            self.base = now
            return None
        if self.start is None:
            if now - self.base > STEP_W:
                self.start, self.during = ts, [now]
            else:
                self.base = now
            return None
        if now - self.base > STEP_W / 2:
            self.during.append(now)
            return None
        run = {
            "device": str(reading.get("device", METER)),
            "ts": self.start,
            "end": ts,
            "watts": round(float(np.mean(self.during)) - self.base),
            "minutes": round((ts - self.start) / STEP_S),
        }
        self.base, self.start, self.during = now, None, []
        return run


def find_runs(readings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every run in ``readings``, in order."""
    finder = RunFinder()
    return [run for r in readings if (run := finder.take(r)) is not None]


def fit(
    runs: Iterable[Mapping[str, Any]], features: Sequence[str] = FIRST_FEATURES
) -> dict[str, np.ndarray]:
    """The model of labelled ``runs`` (each with its ``appliance``), looking at ``features``."""
    by_class: dict[str, list[list[float]]] = {}
    for run in runs:
        by_class.setdefault(str(run["appliance"]), []).append([float(run[f]) for f in features])
    classes = sorted(by_class)
    floor = np.array([FLOOR[f] for f in features])
    return {
        "classes": np.array(classes),
        "features": np.array(list(features)),
        "mean": np.array([np.mean(by_class[c], axis=0) for c in classes]),
        "spread": np.array([np.maximum(np.std(by_class[c], axis=0), floor) for c in classes]),
    }


def classify(model: Mapping[str, np.ndarray], run: Mapping[str, Any]) -> tuple[str, float]:
    """The appliance ``run`` looks like, and how many spreads from its usual run it is."""
    x = np.array([float(run[str(f)]) for f in model["features"]])
    distance = np.sqrt((((x - model["mean"]) / model["spread"]) ** 2).sum(axis=1))
    nearest = int(np.argmin(distance))
    return str(model["classes"][nearest]), float(distance[nearest])
