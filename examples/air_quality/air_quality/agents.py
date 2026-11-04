"""The two agents: one scores every reading, the other asks about what stood out.

`detect` is a function: it scores each reading against the stored model and
publishes the score, for the Air tab, and returns an alarm only when the score
is past the threshold. That keeps it a detector a trial can judge (an input is
flagged when the agent publishes something for it), and keeps it free of
memory, since a trial runs it a second time for each reading with the version
on trial. The threshold is the model version's own, from its metadata
(``--meta k_sigma=5``), so a version and the threshold it was meant for travel
together.

`AirWatch` is an actor, because it remembers: it opens an episode when a room
stays past the threshold, closes it when the room settles, and then asks the
person what it was. The answer becomes a label on the room over exactly the
episode's window, which is what a later trial is judged against.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any

import wactorz
from wactorz import Actor, Message

from .model import K_SIGMA, MODEL_NAME, score
from .plant import STEP_S, TITLES, UNITS

#: Readings in a row past the threshold that open an episode, and below it that close one.
OPEN_AFTER = 2
CLOSE_AFTER = 3
#: What the person may answer, and the label each answer makes. "Normal" is a
#: label too, so a false alarm is counted as one.
ANSWERS = {
    "Window open": "window_open",
    "Ventilation fault": "ventilation_fault",
    "Sensor fault": "sensor_fault",
    "Normal": "normal",
}


@wactorz.agent(
    name="air-detect",
    subscribes="sensors/air/#",
    publishes="air/alarms",
    description="Scores each room's air against what is usual for the hour.",
    capabilities=["anomaly_detection", "air_quality"],
    models={"model": MODEL_NAME},
)
async def detect(reading: dict, me: wactorz.FunctionAgent) -> dict | None:
    """Publish the reading's score; return an alarm when it is past the threshold."""
    model = await me.load_model("model")
    threshold = float(me.artifacts["model"]["metadata"].get("k_sigma", K_SIGMA))
    value, worst = score(model, reading)
    room = str(reading["device"])
    scored = {"device": room, "ts": reading["ts"], "score": round(value, 2), "worst": worst}
    await me.publish(f"air/{room}/score", {**scored, "threshold": threshold})
    return scored if value >= threshold else None


@dataclass
class Episode:
    """One room's stretch past the threshold, while it is being followed."""

    above: int = 0
    below: int = 0
    start: float | None = None
    last: float = 0.0
    peak: float = 0.0
    worst: str = ""
    open: bool = False


def follow(episode: Episode, scored: dict[str, Any]) -> str | None:
    """Take one score into ``episode``; say ``opened`` or ``closed`` when that happens."""
    ts, value = float(scored["ts"]), float(scored["score"])
    if value >= float(scored["threshold"]):
        episode.above, episode.below = episode.above + 1, 0
        if episode.start is None:
            episode.start = ts
        episode.last = ts
        if value > episode.peak:
            episode.peak, episode.worst = value, str(scored["worst"])
        if not episode.open and episode.above >= OPEN_AFTER:
            episode.open = True
            return "opened"
        return None
    episode.above, episode.below = 0, episode.below + 1
    if episode.open and episode.below >= CLOSE_AFTER:
        return "closed"
    if not episode.open:
        # A reading or two past the threshold that did not last: not an episode.
        episode.start, episode.peak = None, 0.0
    return None


def clock(ts: float) -> str:
    """``ts`` as the simulated building's time of day."""
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%H:%M")


def question(room: str, start: float, end: float, episode: Episode) -> str:
    """What the person is asked about one episode."""
    unit = UNITS.get(episode.worst, "")
    return (
        f"{TITLES.get(room, room)}, {clock(start)}–{clock(end)} UTC: {episode.worst} ({unit}) "
        f"was far from usual for the hour. What was it?"
    )


class AirWatch(Actor):
    """Opens an episode when a room stays past its threshold, and asks what it was once it ends."""

    AGENT_NAME = "air-watch"
    DESCRIPTION = "Asks the person about each stretch a room's air was far from usual."
    SUBSCRIBES = ("air/+/score",)
    PUBLISHES = ("air/episodes",)

    #: Each room's episode, as it is being followed.
    rooms: dict[str, Episode]

    async def on_start(self) -> None:
        self.rooms = {}
        self.subscribe("air/+/score", self.on_score)

    async def on_score(self, scored: Any) -> None:
        if not isinstance(scored, dict) or "device" not in scored:
            return
        room = str(scored["device"])
        episode = self.rooms.setdefault(room, Episode())
        said = follow(episode, scored)
        if said == "opened":
            await self.tell(room, "open", episode)
        elif said == "closed":
            self.rooms[room] = Episode()
            await self.tell(room, "closed", episode)
            self.run_detached(self.ask(room, episode), name=f"{self.name}:ask:{room}")

    async def tell(self, room: str, state: str, episode: Episode, answer: str = "") -> None:
        """Say where an episode stands, for the Air tab."""
        await self.publish(
            "air/episodes",
            {
                "device": room,
                "state": state,
                "start": episode.start,
                "end": episode.last + STEP_S,
                "peak": round(episode.peak, 1),
                "worst": episode.worst,
                "answer": answer,
            },
        )

    async def ask(self, room: str, episode: Episode) -> None:
        start = float(episode.start or episode.last)
        end = episode.last + STEP_S
        answer = await self.ask_operator(
            question(room, start, end, episode),
            options=list(ANSWERS),
            label={"source": room, "start": start, "end": end, "values": ANSWERS},
            context=(
                f"Its score, how many spreads from usual for the hour, peaked at "
                f"{episode.peak:.0f}. Your answer labels {TITLES.get(room, room)} from "
                f"{clock(start)} to {clock(end)}."
            ),
        )
        await self.tell(room, "answered", episode, answer)

    async def handle_message(self, msg: Message) -> None:
        return None
