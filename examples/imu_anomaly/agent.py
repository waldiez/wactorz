"""The agent: one function, one decorator.

Subscribed to every IMU topic, it scores each reading with the trained model
and publishes the ones that are far from normal. The model is named with
``models=``: before the agent starts, Wactorz finds the version of
``imu-detector`` that is active among the models kept on main (the newest when
none is set active), and refuses to start it without one. On a node, the node
fetches it by name first. When ``active`` moves, a running agent serves the
version it moved to from the next reading: it reads the model through
``load_model`` on each call, which is what the swap replaces.
"""

from model import MODEL_NAME, THRESHOLD, score

import wactorz


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
    # Loaded on the first reading and kept, until `active` moves: the arrays
    # `train.py` saved.
    model = await me.load_model("model")
    value = score(model, reading)
    if value < float(me.options.get("threshold", THRESHOLD)):
        return None
    me.persist("anomalies_total", int(me.recall("anomalies_total", 0)) + 1)
    return {"score": round(value, 2), "reading": reading}
