"""The detector as one stage of a pipeline, with a notifier, a rule and a schedule.

Each step stays an agent of its own; the pipeline groups them, ticks the
report every five minutes, and acts on a strong anomaly without a program for
the glue. Run it the way `run.py` is run:

    python pipeline.py
"""

from agent import detect

import wactorz


@wactorz.agent(subscribes="anomalies/imu", description="Says what the detector found.")
async def notify(anomaly: dict, me: wactorz.FunctionAgent) -> None:
    """Every anomaly, as a line in chat."""
    await me.notify_user(f"IMU anomaly, score {anomaly.get('score')}")


@wactorz.agent(
    subscribes="pipelines/imu-watch/tick",
    publishes="reports/imu",
    description="A count of anomalies since the last tick.",
)
def report(tick: dict, me: wactorz.FunctionAgent) -> dict:
    """What the tick asks for: how many anomalies the detector has flagged."""
    seen = int(me.recall("reported", 0))
    total = int(me.recall("anomalies_total", 0))
    me.persist("reported", total)
    return {"new_anomalies": total - seen, "total": total}


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
