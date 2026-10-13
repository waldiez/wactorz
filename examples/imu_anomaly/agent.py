"""The agent: one function, one decorator.

Subscribed to every IMU topic, it scores each reading with the trained model
and publishes the ones that are far from normal. The model is loaded once, on
the first reading, from the file beside this module or the path given as the
agent's `model` option.
"""

import pickle
from pathlib import Path

# `model.py` defines the model class; unpickling needs it importable.
from model import MODEL_PATH, MahalanobisModel  # noqa: F401

import wactorz

#: Readings this far from normal, in units of its spread, are anomalies.
THRESHOLD = 4.0


@wactorz.agent(
    name="imu-anomaly",
    subscribes="sensors/imu/#",
    publishes="anomalies/imu",
    description="Flags IMU readings the trained model calls abnormal.",
    capabilities=["anomaly_detection", "imu"],
    input_schema={"ax": "float", "ay": "float", "az": "float"},
    output_schema={"score": "float", "reading": "dict"},
    requires={"ram_mb": 128, "packages": ["numpy"]},
)
def detect(reading: dict, me: wactorz.FunctionAgent) -> dict | None:
    """Score one reading; return it with its score when it is abnormal."""
    model = me.options.get("_model")
    if model is None:
        path = Path(me.options.get("model") or MODEL_PATH)
        model = pickle.loads(path.read_bytes())  # noqa: S301  # a file this example wrote
        me.options["_model"] = model
    score = model.score(reading)
    if score < float(me.options.get("threshold", THRESHOLD)):
        return None
    me.persist("anomalies_total", int(me.recall("anomalies_total", 0)) + 1)
    return {"score": round(score, 2), "reading": reading}
