"""The detector as one stage of a pipeline, with a notifier, a rule and a schedule.

Each step stays an agent of its own; the pipeline groups them, ticks the
report every five minutes, and acts on a strong anomaly without a program for
the glue. Run it the way `run.py` is run:

    python pipeline.py
"""

import threading

from agent import detect

import wactorz

#: `report` counts on one thread per topic, anomalies and ticks; the lock keeps
#: a tick from reading the count while an anomaly is adding to it.
_count_lock = threading.Lock()


@wactorz.agent(subscribes="anomalies/imu", description="Says what the detector found.")
async def notify(anomaly: dict, me: wactorz.FunctionAgent) -> None:
    """Every anomaly, as a line in chat."""
    await me.notify_user(f"IMU anomaly, score {anomaly.get('score')}")


@wactorz.agent(
    subscribes=["anomalies/imu", "pipelines/imu-watch/tick"],
    publishes="reports/imu",
    description="A count of anomalies since the last tick.",
)
def report(message: dict, me: wactorz.FunctionAgent) -> dict | None:
    """Count each anomaly as it arrives; on each tick, report the count since the last.

    The count is this agent's own: persistence is per agent, so `report` cannot
    read what `detect` persists, and counts what `detect` publishes instead.
    A tick is told from an anomaly by its `fired_at`.
    """
    with _count_lock:
        if "fired_at" not in message:
            me.persist("since_tick", int(me.recall("since_tick", 0)) + 1)
            me.persist("total", int(me.recall("total", 0)) + 1)
            return None
        new = int(me.recall("since_tick", 0))
        me.persist("since_tick", 0)
        return {"new_anomalies": new, "total": int(me.recall("total", 0))}


watch = wactorz.pipeline(
    "imu-watch",
    steps=[detect, notify, report],
    schedule={"type": "interval", "seconds": 300},
    rules=[
        {
            "triggers": ["anomalies/imu"],
            "conditions": [{"field": "score", "op": "gt", "value": 20}],
            "actions": [
                {"type": "publish", "topic": "alerts/imu", "payload": {"level": "high"}},
            ],
            "cooldown_seconds": 30,
        }
    ],
    description="Watch the IMU stream, report every five minutes, alert on a strong anomaly.",
)

if __name__ == "__main__":
    wactorz.run(web=True, minimal=True, state_dir="./state")
