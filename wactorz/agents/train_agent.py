# SPDX-License-Identifier: Apache-2.0
"""The trainer: makes a new version of a model from recorded readings, on a task.

A library agent the package brings, spawned from the catalogue on main with
the recipe it runs and the model it trains::

    {"type": "module", "target": "wactorz.agents.train_agent:TrainAgent",
     "name": "trainer",
     "options": {"recipe": "examples/imu_anomaly/recipe.py", "model": "imu-detector",
                 "source": "sensors/imu", "metric": "f1", "margin": 0.02}}

A task to it runs one training (`wactorz.train.run`): ``{"since": ...,
"until": ..., "split": {...}, "settings": {...}, "reason": "..."}``, from
chat (``@trainer {"reason": "drift"}``, or ``@trainer <why>`` for a run with
the defaults), from a rule's ``task`` action or from the controller, each
saying why.
What the task does not say takes the agent's options; the default window is
the last week, with its last fifth held out. The fit runs in a child process
with the agent's timeout and memory limit, the version is stored with what it
was trained on, the pointer the agent is for (``shadow`` unless said) moves
when the new version beats the one served by the margin, and the run and the
version are written into the provenance graph. One training runs at a time;
a task that arrives during one is refused, and ``{"action": "status"}`` says
what is running or what the last run came to. ``docs/design/train-step.md``
is the decision.

The trainer runs on main, beside the store and the data directory it reads;
spawned on a node, it refuses every task and says so.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from ..core.actor import Actor, Message, MessageType
from ..core.labels import Labels
from ..core.paths import resolve_state_dir
from ..core.persistence.stores import get_db
from ..core.provenance import Provenance
from ..train.run import LabelsOf, Outcome, run
from ..train.spec import DEFAULT_SEED, DEFAULT_TIMEOUT_S, DEFAULT_WINDOW_S, TrainError, TrainSpec

#: What the catalogue and the dashboard call it.
NAME = "trainer"


class TrainAgent(Actor):
    """Trains ``model`` from ``source`` with ``recipe`` when asked, and sets the shadow when it is better."""

    #: What the catalogue lists it as, and says of it.
    AGENT_NAME = NAME
    DESCRIPTION = (
        "Trains a new version of a model from recorded readings, scores it against the one "
        "served, and sets it as shadow when it is better by the margin."
    )
    CAPABILITIES = ("ml_training", "models", "timeseries")
    INPUT_SCHEMA: ClassVar[dict[str, str]] = {
        "recipe": "str — the recipe module, or .py file, with train() and score() (spawn option)",
        "model": "str — the name the version is stored under (spawn option)",
        "source": "str — the recorded source to read, e.g. sensors/imu (spawn option)",
        "metric": "str — the figure of score() the move depends on (spawn option)",
        "margin": "float — how much better the new version must be (spawn option)",
        "pointer": "str — shadow|active|none, the pointer a run is for (spawn option)",
        "since": "float — Unix seconds the window starts at, as a task; default: a week before until",
        "until": "float — Unix seconds the window ends at, as a task; default: now",
        "split": 'dict — {"kind": "holdout"|"time_split"|"rolling_origin"|"leave_k_out", ...}, as a task',
        "settings": "dict — passed to train(), as a task; over the spawn option's",
        "reason": "str — why the run was asked for, kept with the move",
        "action": "str — status, as a task: what is running or what the last run came to",
    }
    AUTOSTART = False

    def __init__(
        self,
        *,
        name: str | None = None,
        persistence_dir: str | None = None,
        actor_id: str | None = None,
        recipe: str = "",
        model: str = "",
        source: str = "",
        metric: str = "",
        margin: float = 0.0,
        higher_is_better: bool = True,
        pointer: str = "shadow",
        settings: Mapping[str, Any] | None = None,
        split: Mapping[str, Any] | None = None,
        seed: int = DEFAULT_SEED,
        labels: bool | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        memory_mb: int = 0,
        window_s: float = DEFAULT_WINDOW_S,
    ) -> None:
        super().__init__(actor_id=actor_id, name=name or NAME, persistence_dir=persistence_dir)
        self.options: dict[str, Any] = {
            "recipe": recipe,
            "model": model,
            "source": source,
            "metric": metric,
            "margin": margin,
            "higher_is_better": higher_is_better,
            "pointer": pointer,
            "settings": dict(settings or {}),
            "split": dict(split) if split else None,
            "seed": seed,
            "labels": labels,
            "timeout_s": timeout_s,
            "memory_mb": memory_mb,
        }
        self.window_s = window_s
        #: The run in progress, and the last one that ended.
        self.running: TrainSpec | None = None
        self.last: Outcome | None = None
        self.runs = 0

    @property
    def on_node(self) -> bool:
        """Whether this agent runs on a node, where the store and the data are not."""
        return getattr(self, "_runner", None) is not None

    async def on_start(self) -> None:
        await self.publish_manifest(
            description=self.DESCRIPTION,
            publishes=[],
            capabilities=list(self.CAPABILITIES),
            input_schema=dict(self.INPUT_SCHEMA),
        )
        if self.on_node:
            await self.log(TrainError.not_on_main().args[0], level="warning")
            return
        if not self.options["recipe"] or not self.options["model"] or not self.options["source"]:
            await self.log(
                "a task needs recipe, model and source: set them as spawn options or on the task",
                level="warning",
            )

    def spec_for(self, payload: Mapping[str, Any], *, now: float | None = None) -> TrainSpec:
        """The run a task asks for, over this agent's options. Raises `TrainError`."""
        at = time.time() if now is None else now
        given = {k: v for k, v in payload.items() if k not in ("_task_id", "action")}
        # From chat the task is its text: a JSON object says the run's fields;
        # any other words are why it was asked for.
        text = given.pop("text", None)
        if isinstance(text, str) and text.strip():
            asked = _json_object(text)
            if asked is not None:
                given = {**asked, **given}
            elif "reason" not in given:
                given["reason"] = text.strip()
        merged: dict[str, Any] = {**self.options, **given}
        if "settings" in given and isinstance(given["settings"], Mapping):
            merged["settings"] = {**self.options["settings"], **given["settings"]}
        until = merged.get("until")
        merged["until"] = float(until) if isinstance(until, (int, float)) else at
        since = merged.get("since")
        merged["since"] = (
            float(since) if isinstance(since, (int, float)) else merged["until"] - self.window_s
        )
        return TrainSpec.from_dict(merged)

    def status(self) -> dict[str, Any]:
        """What is running, and what the last run came to."""
        return {
            "running": self.running.to_dict() if self.running else None,
            "last": self.last.to_dict() if self.last else None,
            "runs": self.runs,
            "options": dict(self.options),
        }

    async def handle_message(self, msg: Message) -> None:
        """A task: ``{"action": "status"}`` answers at once; anything else is a run, answered when it ends."""
        if msg.type != MessageType.TASK:
            return
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        task_id = payload.get("_task_id")
        target = msg.reply_to or msg.sender_id
        if str(payload.get("action") or "") == "status":
            await self._reply(target, task_id, self.status())
            return
        if self.on_node:
            await self._reply(target, task_id, {"error": TrainError.not_on_main().args[0]})
            return
        if self.running is not None:
            await self._reply(
                target,
                task_id,
                {"error": TrainError.in_progress(self._running_id).args[0]},
            )
            return
        try:
            spec = self.spec_for(payload)
        except TrainError as exc:
            await self._reply(target, task_id, {"error": str(exc)})
            return
        self.running = spec
        self._running_id = "starting"
        self.run_detached(self._train_and_reply(spec, target, task_id), name=f"{self.name}:train")

    async def _train_and_reply(self, spec: TrainSpec, target: str, task_id: Any) -> None:
        try:
            outcome = await self.train(spec)
        finally:
            self.running = None
        await self._reply(target, task_id, outcome.to_dict())

    async def train(self, spec: TrainSpec) -> Outcome:
        """One run, said on the feed as it starts and as it ends."""
        await self.log(
            f"training {spec.model} with {spec.recipe} on {spec.source}, "
            f"{_window(spec.since, spec.until)}" + (f": {spec.reason}" if spec.reason else "")
        )
        db = get_db()
        outcome = await run(
            spec,
            state_dir=Path(resolve_state_dir()),
            by=f"agent:{self.name}",
            prov=Provenance(db) if db is not None else None,
            labels_of=_labels_reader(db) if db is not None else None,
        )
        self._running_id = outcome.run_id
        self.last = outcome
        self.runs += 1
        if outcome.ok:
            self.metrics.tasks_completed += 1
        else:
            self.metrics.tasks_failed += 1
        await self.log(outcome.summary(), level="info" if outcome.ok else "warning")
        return outcome

    async def _reply(self, target: str, task_id: Any, reply: dict[str, Any]) -> None:
        if task_id is not None:
            reply = {**reply, "_task_id": task_id}
        await self.send(target, MessageType.RESULT, reply)

    async def log(self, message: str, level: str = "info") -> None:
        """Say something on the dashboard feed, under this agent's name."""
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": message, "level": level, "timestamp": time.time()},
        )


def _json_object(text: str) -> dict[str, Any] | None:
    """``text`` as the JSON object it is, or None when it is not one."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _window(since: float, until: float) -> str:
    hours = (until - since) / 3600.0
    return f"the last {hours / 24:g} day(s)" if hours >= 48 else f"the last {hours:g} hour(s)"


def _labels_reader(db: Any) -> LabelsOf:
    labels = Labels(db)

    def effective(source: str, since: float, until: float) -> Any:
        return labels.effective(source, since=since, until=until)

    return effective
