"""The agent: one function, one decorator.

Subscribed to every IMU topic, it scores each reading with the trained model
and publishes the ones that are far from normal. The model is loaded once, on
the first reading: from the agent's own state when it holds one, else from the
file beside this module or the path given as the agent's `model` option. The
file's bytes are then persisted, and bytes an agent persists are kept as a
file of their own beside its state and go with it when it moves -- a restart
without the file, a migration to a node -- so the stored copy is the one that
counts from then on. To read a newly trained file, delete the agent's state.
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
        stored = me.recall("model")
        if not isinstance(stored, bytes):
            path = Path(me.options.get("model") or MODEL_PATH)
            stored = path.read_bytes()
            me.persist("model", stored)
        model = pickle.loads(stored)  # noqa: S301  # bytes this example wrote
        me.options["_model"] = model
    score = model.score(reading)
    if score < float(me.options.get("threshold", THRESHOLD)):
        return None
    me.persist("anomalies_total", int(me.recall("anomalies_total", 0)) + 1)
    return {"score": round(score, 2), "reading": reading}
