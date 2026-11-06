# SPDX-License-Identifier: Apache-2.0
"""The model: what each room is usually like at each hour, and how far a reading is from it.

For every room, the mean and the spread of each quantity at each hour of a
weekday and of a weekend day. A reading's score is how many spreads its
furthest quantity is from that hour's mean: 1200 ppm of CO2 is ordinary in a
full meeting room at three in the afternoon and far from normal at three in
the morning. Kept as plain arrays in an ``.npz``, so loading it runs nothing
from the file.
"""

from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from .plant import DAY_S, QUANTITIES, ROOMS, WEEK

#: The name the model is stored under, and the agent names it by.
MODEL_NAME = "air-quality"
#: How many spreads from normal is an alarm, unless the version's metadata says.
K_SIGMA = 5.0
#: The least spread each quantity is given, so a sensor that hardly moves at
#: night does not make its every wobble an alarm.
FLOOR = np.array([20.0, 0.2, 1.0, 1.0])
SLOTS = 48


def slot(ts: float) -> int:
    """The hour of the day, counted again from 24 at the weekend."""
    since = ts - WEEK
    hour = int((since % DAY_S) // 3600)
    weekend = int(since // DAY_S) % 7 >= 5
    return hour + 24 * weekend


def fit(readings: Iterable[Mapping[str, Any]]) -> dict[str, np.ndarray]:
    """The model of a healthy stretch of ``readings``: per room, a mean and a spread per slot."""
    seen: dict[str, list[list[list[float]]]] = {room: [[] for _ in range(SLOTS)] for room in ROOMS}
    for r in readings:
        seen[r["device"]][slot(float(r["ts"]))].append([float(r[q]) for q in QUANTITIES])
    model: dict[str, np.ndarray] = {}
    for room, slots in seen.items():
        rows = [np.array(s) if s else np.zeros((1, len(QUANTITIES))) for s in slots]
        model[f"{room}.mean"] = np.array([r.mean(axis=0) for r in rows])
        model[f"{room}.spread"] = np.maximum(np.array([r.std(axis=0) for r in rows]), FLOOR)
    return model


def score(model: Mapping[str, np.ndarray], reading: Mapping[str, Any]) -> tuple[float, str]:
    """How far ``reading`` is from normal for its room and hour, and which quantity is furthest."""
    room, at = str(reading["device"]), slot(float(reading["ts"]))
    values = np.array([float(reading[q]) for q in QUANTITIES])
    z = np.abs(values - model[f"{room}.mean"][at]) / model[f"{room}.spread"][at]
    worst = int(np.argmax(z))
    return float(z[worst]), QUANTITIES[worst]
