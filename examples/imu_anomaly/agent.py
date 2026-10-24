"""The agent: one function, one decorator.

Subscribed to every IMU topic, it scores each reading with the trained model
and publishes the ones that are far from normal. The model is named with
``models=``: Wactorz finds the newest ``imu-detector`` among the models kept
on main before the agent starts, and refuses to start it without one. On a
node, the node fetches it by name first.
"""

from model import MODEL_NAME, score

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
    models={"model": MODEL_NAME},
)
async def detect(reading: dict, me: wactorz.FunctionAgent) -> dict | None:
    """Score one reading; return it with its score when it is abnormal."""
    # Loaded on the first reading and kept: the arrays `train.py` saved.
    model = await me.load_model("model")
    value = score(model, reading)
    if value < float(me.options.get("threshold", THRESHOLD)):
        return None
    me.persist("anomalies_total", int(me.recall("anomalies_total", 0)) + 1)
    return {"score": round(value, 2), "reading": reading}
