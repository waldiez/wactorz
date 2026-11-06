"""The loop without a person: the four library agents, as this package carries them.

Wactorz brings a recorder, a drift agent, a trainer and a rule agent, each
spawned from the catalogue with options. This package carries them with
their options set, started with the system, so the building's readings are
filed, watched, and retrained on without anyone typing a spawn:

- `AirRecorder` files every room's readings under one source, `sensors/air`,
  and the detector's scores under another, `air/scores`, since the model is
  of every room at once and the drift agent reads one source of each;
- `AirDrift` watches the model: PSI and KS on each quantity, and on the
  scores, with ADWIN for the moment the scores changed, every ten seconds
  (the day plays in minutes);
- `AirTrainer` fits `recipe.py` on a window of the readings and sets the
  shadow when the new version is no worse on false alarms;
- `AirRetrain` is the baseline rule of ``docs/design/drift.md``: an alarm on
  the scores sends the trainer a task cut at the moment the alarm names, once
  a day.

The rule listens to the score tests alone: a day's readings held against a
week's, nights and weekend included, look shifted on an ordinary busy day,
while the scores are what is usual for the hour and shift only when a room
does. Each class is a subclass with defaults, so a spawn can still say
otherwise: ``@catalog spawn air-drift {"check_s": 60}``.
"""

from typing import Any

from wactorz.agents.drift_agent import DriftAgent
from wactorz.agents.recorder_agent import RecorderAgent
from wactorz.agents.rule_agent import RuleAgent, RuleConfig
from wactorz.agents.train_agent import TrainAgent

from .model import K_SIGMA, MODEL_NAME

#: Where the recorder files the readings and the scores: one source each.
READINGS = "sensors/air"
SCORES = "air/scores"

#: The recipe the trainer runs, by its importable name.
RECIPE = "air_quality.recipe"

#: How often the drift agent checks: the day plays in a couple of minutes.
CHECK_S = 10.0

#: The baseline rule: an alarm on the scores retrains once a day, cut where the alarm says.
RETRAIN_RULE: dict[str, Any] = {
    "triggers": [f"models/{MODEL_NAME}/drift"],
    "conditions": [{"field": "column", "op": "eq", "value": "score"}],
    "actions": [
        {
            "type": "task",
            "agent": "air-trainer",
            "payload": {
                "split": {"kind": "time_split", "at": "{at}"},
                "observation": "{observation}",
                "reason": "drift: {test} on {column} ({statistic})",
            },
        }
    ],
    "cooldown_seconds": 86400,
    "description": "retrains the air model once a day when its scores drift",
}


class AirRecorder(RecorderAgent):
    """Files the rooms' readings under `READINGS` and the detector's scores under `SCORES`."""

    AGENT_NAME = "air-recorder"
    DESCRIPTION = "Files every room's readings under one source and the scores under another."
    AUTOSTART = True

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("topics", ["sensors/air/#", "air/+/score"])
        super().__init__(**kwargs)

    def source_of(self, payload: Any, topic: str) -> str:
        return SCORES if topic.endswith("/score") else READINGS


class AirDrift(DriftAgent):
    """Watches the air model's readings and scores for drift."""

    AGENT_NAME = "air-drift"
    DESCRIPTION = "Watches the air model's readings and scores for drift, every ten seconds."
    AUTOSTART = True

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("model", MODEL_NAME)
        kwargs.setdefault("source", READINGS)
        kwargs.setdefault("scores", SCORES)
        kwargs.setdefault("check_s", CHECK_S)
        super().__init__(**kwargs)


class AirTrainer(TrainAgent):
    """Fits `recipe.py` on a window of the readings when asked."""

    AGENT_NAME = "air-trainer"
    DESCRIPTION = (
        "Trains the air model from the recorded readings, and sets the shadow when no worse."
    )
    AUTOSTART = True

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("recipe", RECIPE)
        kwargs.setdefault("model", MODEL_NAME)
        kwargs.setdefault("source", READINGS)
        kwargs.setdefault("metric", "false_alarms")
        kwargs.setdefault("higher_is_better", False)
        kwargs.setdefault("margin", 0.0)
        kwargs.setdefault("settings", {"k_sigma": K_SIGMA})
        super().__init__(**kwargs)


class AirRetrain(RuleAgent):
    """The baseline rule: a drift alarm on the scores retrains, cut where the alarm says, once a day."""

    AGENT_NAME = "air-retrain"
    DESCRIPTION = RETRAIN_RULE["description"]
    AUTOSTART = True
    SUBSCRIBES = tuple(RETRAIN_RULE["triggers"])

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(RuleConfig.from_dict(RETRAIN_RULE), **kwargs)
