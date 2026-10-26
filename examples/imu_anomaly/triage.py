"""Ask a person about a strong anomaly, and keep the answer as a label.

On a jolt far past the detector's threshold, `triage` asks on the dashboard
whether it was maintenance or a real problem. The question carries a label
spec, so the answer becomes a label of that device from the jolt's own time
on, which `GET /api/labels` lists; "Not sure" makes none. Main records labels,
so they are kept with the full system (`python run.py --with-main --llm fake`);
in the minimal profile the question is still asked and answered.

A burst of jolts is one event to a person, not twenty: a device is asked about
at most once per `COOLDOWN_S`.
"""

import asyncio
import time

import wactorz

#: A score this far from normal is worth a person's time.
STRONG = 20.0
#: How long after asking about a device it is not asked about again.
COOLDOWN_S = 120.0
#: How long one question is waited for. It stays open on the dashboard after
#: that, and a later answer still becomes a label.
WAIT_S = 600.0
OPTIONS = ["Maintenance", "Real problem", "Not sure"]
#: Which answer is which label value; "Not sure" is left out, so it makes none.
VALUES = {"Maintenance": "maintenance", "Real problem": "fault"}


@wactorz.agent(
    subscribes="anomalies/imu",
    description="Asks a person whether a strong IMU anomaly was maintenance or a fault.",
    # A question waits for its answer; more than one in flight keeps the next
    # anomaly from queueing behind it, to be checked against the cooldown.
    concurrency=4,
)
async def triage(anomaly: dict, me: wactorz.FunctionAgent) -> None:
    """Ask about a strong anomaly, unless its device was asked about just now."""
    reading = anomaly.get("reading") or {}
    if float(anomaly.get("score", 0.0)) < STRONG:
        return
    source = f"sensors/imu/{reading.get('device', 'unknown')}"
    now = time.time()
    if now - float(me.recall(f"asked:{source}", 0.0)) < COOLDOWN_S:
        return
    # Noted before anything is awaited, so a jolt handled beside this one sees it.
    me.persist(f"asked:{source}", now)
    try:
        await me.ask_operator(
            f"{source} jolted (score {anomaly['score']}). Maintenance, or a real problem?",
            options=OPTIONS,
            timeout=WAIT_S,
            label={"source": source, "start": float(reading.get("ts", now)), "values": VALUES},
        )
    except asyncio.TimeoutError:
        return  # still open on the dashboard; a later answer still becomes a label
