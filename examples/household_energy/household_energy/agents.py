# SPDX-License-Identifier: Apache-2.0
"""The two agents: one finds the runs in the meter and asks about each, the other names them.

`guess` is a function: it takes one run and answers with the appliance it
looks like, which makes it a classifier a trial can judge (the class is in the
``appliance`` field of what it publishes). It keeps nothing between runs,
since a trial runs it a second time for each one with the version on trial.
What it looks at is the version's own, kept in the model.

`EnergyRuns` is an actor, because it remembers: it follows the meter from one
reading to the next to find where a run starts and ends, publishes each run
for `guess` to name, and then asks the person whether the guess was right. The
answer becomes a label on the meter over exactly the run, which is what a
later trial is judged against.
"""

import datetime as dt
from typing import Any

import wactorz
from wactorz import Actor, Message

from .model import MODEL_NAME, RunFinder, classify
from .plant import METER, TITLES

#: What the person may answer, and the label each answer makes.
ANSWERS = {**{title: appliance for appliance, title in TITLES.items()}, "Something else": "other"}


@wactorz.agent(
    name="energy-guess",
    subscribes="energy/runs",
    publishes="energy/guesses",
    description="Names the appliance behind each run of the house's power meter.",
    capabilities=["classification", "energy"],
    models={"model": MODEL_NAME},
)
async def guess(run: dict, me: wactorz.FunctionAgent) -> dict:
    """The run, with the appliance it looks like."""
    model = await me.load_model("model")
    appliance, _distance = classify(model, run)
    return {**run, "appliance": appliance}


def clock(ts: float) -> str:
    """``ts`` as the simulated house's time of day."""
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%H:%M")


def question(guessed: dict[str, Any]) -> str:
    """What the person is asked about one run."""
    minutes = int(guessed["minutes"])
    title = TITLES.get(str(guessed["appliance"]), str(guessed["appliance"]))
    return (
        f"{clock(float(guessed['ts']))}–{clock(float(guessed['end']))} UTC: about "
        f"{int(guessed['watts']):,} W for {minutes} minute{'s' if minutes != 1 else ''}. "
        f"My guess: {title.lower()}. What was it?"
    )


def options(guessed: dict[str, Any]) -> list[str]:
    """The answers offered, the guess first."""
    first = TITLES.get(str(guessed["appliance"]))
    rest = [a for a in ANSWERS if a != first]
    return [first, *rest] if first else rest


class EnergyRuns(Actor):
    """Finds each run in the meter's readings, and asks what it was once it has been named."""

    AGENT_NAME = "energy-runs"
    DESCRIPTION = "Finds when an appliance ran, and asks the person whether the guess was right."
    SUBSCRIBES = ("sensors/energy/meter", "energy/guesses")
    PUBLISHES = ("energy/runs", "energy/answers")

    finder: RunFinder

    async def on_start(self) -> None:
        self.finder = RunFinder()
        self.subscribe("sensors/energy/meter", self.on_reading)
        self.subscribe("energy/guesses", self.on_guess)

    async def on_reading(self, reading: Any) -> None:
        if not isinstance(reading, dict) or "watts" not in reading:
            return
        run = self.finder.take(reading)
        if run is not None:
            await self.publish("energy/runs", run)

    async def on_guess(self, guessed: Any) -> None:
        if not isinstance(guessed, dict) or "appliance" not in guessed:
            return
        self.run_detached(self.ask(guessed), name=f"{self.name}:ask:{guessed['ts']}")

    async def ask(self, guessed: dict[str, Any]) -> None:
        start, end = float(guessed["ts"]), float(guessed["end"])
        answer = await self.ask_operator(
            question(guessed),
            options=options(guessed),
            label={"source": METER, "start": start, "end": end, "values": ANSWERS},
            context=(
                f"Your answer labels the meter from {clock(start)} to {clock(end)}, which is "
                f"what a new version of the model is judged against."
            ),
        )
        await self.publish(
            "energy/answers",
            {
                "device": METER,
                "ts": start,
                "end": end,
                "guess": guessed["appliance"],
                "answer": ANSWERS.get(answer, answer),
            },
        )

    async def handle_message(self, msg: Message) -> None:
        return None
