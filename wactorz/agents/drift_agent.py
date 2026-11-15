# SPDX-License-Identifier: Apache-2.0
"""The drift agent: watches one model's recorded readings, and says when they no longer look like what it learned from.

A library agent the package brings, spawned from the catalogue on main for
one model::

    {"type": "module", "target": "wactorz.agents.drift_agent:DriftAgent",
     "name": "drift",
     "options": {"model": "imu-detector", "source": "sensors/imu",
                 "scores": "anomalies/imu", "window_s": 86400}}

On a cadence (``check_s``, else ``WACTORZ_DRIFT_CHECK_S``) it reads two
windows of ``source`` through the loader, in event time: the current window,
the last ``window_s`` ending at the source's own "now", its newest recorded
event time held back by ``lateness_s`` (`wactorz.data.now_of`), so a replay
is checked at its own moments and a live run at the wall clock less a
minute; and the reference, what the served version learned from (its
``window`` metadata when the `train` step made it), else the window the
agent was spawned with, else the window of the same length before the
current one. Over them it runs PSI and KS on each feature column
(``columns``, else every numeric one), and, when ``scores`` names a recorded
source of the model's scores, the same on its ``score_column`` and ADWIN
over the current window for the moment the scores changed
(`wactorz.drift`).

A test over its threshold on a column raises an episode, once: an
``observation`` node about the model, a line on the feed, a message on
``models/<model>/drift`` with the observation's id, for a rule to fire the
trainer on, and the drift gauges. The episode stays raised, the gauges
following, until a check finds the statistic back under its threshold; a
promotion moves the reference to the new version and the episodes start
afresh. After a restart the episodes of the version served are read back
from the graph. ``docs/design/drift.md`` is the decision.

A task ``{"action": "check"}`` runs a check now and answers with every
reading; ``{"action": "status"}`` says what the last check found. The agent
runs on main, beside the store and the data directory it reads; spawned on
a node, it refuses and says so.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any, ClassVar

from ..config import DRIFT_CHECK_S
from ..core.actor import Actor, Message, MessageType
from ..core.artifacts import ArtifactError, ArtifactStore
from ..core.paths import resolve_state_dir
from ..core.persistence.stores import get_db
from ..core.provenance import Provenance
from ..core.topics import agent_topic, model_topic
from ..data import DEFAULT_LATENESS_S, data_root, now_of
from ..drift import (
    DEFAULT_SCORE_COLUMN,
    Reading,
    Thresholds,
    Window,
    check_features,
    check_scores,
    raised_episodes,
    record_alarm,
)
from ..monitoring import model_metrics

logger: logging.Logger = logging.getLogger(__name__)

#: What the catalogue and the dashboard call it.
NAME = "drift"

#: How long the current window is, in seconds, unless said.
DEFAULT_WINDOW_S = 86400.0

#: How long after starting the first check runs: a restart need not begin by
#: reading two windows of every model.
FIRST_CHECK_AFTER_S = 60.0

#: The pip name of what reading the windows needs, for the feed line when it is missing.
DATA_EXTRA = "wactorz[data]"


def drift_topic(model: str) -> str:
    """The topic an alarm on ``model`` is published on."""
    return model_topic(model, "drift")


class DriftError(ValueError):
    """What keeps a check from running, and why."""

    @classmethod
    def not_on_main(cls) -> DriftError:
        """The error for a drift check on a node, where neither the store nor the data is."""
        return cls(
            "drift is checked on main, where the store and the data are; this node has neither"
        )

    @classmethod
    def no_model(cls) -> DriftError:
        """The error for a drift check with no model to watch."""
        return cls("a check needs the model to watch: set 'model' as a spawn option")

    @classmethod
    def no_source(cls, model: str) -> DriftError:
        """The error for a drift check on ``model`` with no recorded source to read."""
        return cls(
            f"a check needs the recorded source of {model}'s readings: set 'source' as a spawn "
            "option, or serve a version the train step made, which says what it read"
        )

    @classmethod
    def no_version(cls, model: str) -> DriftError:
        """The error for a drift check on ``model``, of which no version is stored."""
        return cls(f"no version of {model} is stored, so there is nothing to drift from")

    @classmethod
    def no_data(cls) -> DriftError:
        """The error for reading the windows without the ``data`` extra."""
        return cls(f"reading the windows needs {DATA_EXTRA}")


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _columns(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(c.strip() for c in value.split(",") if c.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(c).strip() for c in value if str(c).strip())
    return ()


def _window_option(value: Any, source: str) -> Window | None:
    """A reference window as a spawn option gives it: ``{"since": ..., "until": ...}``."""
    if not isinstance(value, Mapping):
        return None
    since, until = value.get("since"), value.get("until")
    if isinstance(since, bool) or isinstance(until, bool):
        return None
    if not isinstance(since, (int, float)) or not isinstance(until, (int, float)):
        return None
    if until <= since:
        return None
    return Window(str(value.get("source") or source), float(since), float(until))


class DriftAgent(Actor):
    """Checks ``model``'s readings against what it learned from, and raises an episode when they moved."""

    #: What the catalogue lists it as, and says of it.
    AGENT_NAME = NAME
    DESCRIPTION = (
        "Watches a model's recorded readings for drift from what it learned from, and says "
        "when a feature or its scores moved, for a rule to retrain on."
    )
    CAPABILITIES = ("drift", "models", "timeseries")
    INPUT_SCHEMA: ClassVar[dict[str, str]] = {
        "model": "str — the model watched (spawn option)",
        "source": "str — the recorded source of its inputs, e.g. sensors/imu (spawn option)",
        "columns": "list — the feature columns tested; every numeric one when unsaid",
        "scores": "str — the recorded source of the model's scores, for ADWIN (spawn option)",
        "score_column": "str — the column of that source holding the score; score when unsaid",
        "reference": 'dict — {"since": ..., "until": ...}, the window compared against',
        "window_s": "float — how long the current window is; a day when unsaid",
        "lateness_s": "float — how far behind the source's newest reading the window ends (60)",
        "check_s": "float — seconds between checks; WACTORZ_DRIFT_CHECK_S when unsaid",
        "psi": "float — PSI at or over this alarms (0.2)",
        "ks": "float — KS's D at or over this, with p under ks_p, alarms (0.1, 0.05)",
        "adwin_delta": "float — ADWIN's confidence (0.002)",
        "min_rows": "int — rows each window needs before a test runs (100)",
        "action": "str — check|status, as a task",
    }
    AUTOSTART = False

    def __init__(
        self,
        *,
        name: str | None = None,
        persistence_dir: str | None = None,
        actor_id: str | None = None,
        model: str = "",
        source: str = "",
        columns: Any = (),
        scores: str = "",
        score_column: str = DEFAULT_SCORE_COLUMN,
        reference: Mapping[str, Any] | None = None,
        window_s: float = DEFAULT_WINDOW_S,
        lateness_s: float = DEFAULT_LATENESS_S,
        check_s: float | None = None,
        psi: float = Thresholds.psi,
        ks: float = Thresholds.ks,
        ks_p: float = Thresholds.ks_p,
        adwin_delta: float = Thresholds.adwin_delta,
        min_rows: int = Thresholds.min_rows,
        bins: int = Thresholds.bins,
    ) -> None:
        super().__init__(actor_id=actor_id, name=name or NAME, persistence_dir=persistence_dir)
        self.model: str = model.strip()
        self.source: str = source.strip()
        self.columns: tuple[str, ...] = _columns(columns)
        self.scores: str = scores.strip()
        self.score_column: str = score_column.strip() or DEFAULT_SCORE_COLUMN
        self.reference: Window | None = _window_option(reference, self.source)
        self.window_s: float = max(1.0, _number(window_s, DEFAULT_WINDOW_S))
        self.lateness_s: float = max(0.0, _number(lateness_s, DEFAULT_LATENESS_S))
        self.check_s: float = max(1.0, _number(check_s, DRIFT_CHECK_S))
        self.thresholds: Thresholds = Thresholds(
            psi=_number(psi, Thresholds.psi),
            ks=_number(ks, Thresholds.ks),
            ks_p=_number(ks_p, Thresholds.ks_p),
            adwin_delta=_number(adwin_delta, Thresholds.adwin_delta),
            min_rows=int(_number(min_rows, Thresholds.min_rows)),
            bins=int(_number(bins, Thresholds.bins)),
        )
        #: The episodes raised, by test and column, each with its observation's id.
        self._raised: dict[tuple[str, str], str] = {}
        #: The version the episodes are against; a change starts them afresh.
        self._version: int | None = None
        #: Whether the episodes were read back from the graph since starting.
        self._recovered = False
        self._checker: asyncio.Task[Any] | None = None
        #: What the last check found, and when, for the card and ``status``.
        self.last: list[Reading] = []
        self.checked_at: float | None = None
        self.checks: int = 0

    @property
    def on_node(self) -> bool:
        """Whether this agent runs on a node, where the store and the data are not."""
        return getattr(self, "_runner", None) is not None

    @property
    def store(self) -> ArtifactStore:
        """The store, read where the state directory is at the time of asking."""
        return ArtifactStore.in_state_dir(resolve_state_dir())

    async def on_start(self) -> None:
        """Publish the manifest and, on main with a model to watch, start checking on schedule;
        otherwise log why no check runs.
        """
        await self.publish_manifest(
            description=self.DESCRIPTION,
            publishes=[drift_topic(self.model)] if self.model else [],
            capabilities=list(self.CAPABILITIES),
            input_schema=dict(self.INPUT_SCHEMA),
        )
        if self.on_node:
            await self.log(DriftError.not_on_main().args[0], level="warning")
            return
        if not self.model:
            await self.log(DriftError.no_model().args[0], level="warning")
            return
        self._checker = self.run_detached(self._check_loop(), name=f"{self.name}:drift")
        await self.log(
            f"watching {self.model} on {self.source or 'the source its version names'}, "
            f"a window of {self.window_s / 3600:g} hour(s), every {self.check_s:g} s"
        )

    async def on_stop(self) -> None:
        """Stop checking, and drop the model's drift metrics."""
        if self._checker is not None:
            self._checker.cancel()
            self._checker = None
        if self.model:
            model_metrics.forget_drift(self.model)
        await super().on_stop()

    async def _check_loop(self) -> None:
        await asyncio.sleep(min(FIRST_CHECK_AFTER_S, self.check_s))
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except DriftError as exc:
                await self.log(str(exc), level="warning")
            except Exception as exc:
                logger.warning("[%s] cannot check %s for drift: %s", self.name, self.model, exc)
            await asyncio.sleep(self.check_s)

    async def handle_message(self, msg: Message) -> None:
        """A task: ``check`` runs one now and answers with its readings; ``status`` answers at once."""
        if msg.type != MessageType.TASK:
            return
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        task_id = payload.get("_task_id")
        target = msg.reply_to or msg.sender_id
        action = str(payload.get("action") or payload.get("text") or "status").strip().lower()
        if action != "check":
            await self._reply(target, task_id, self.status())
            return
        try:
            readings = await self.check()
        except DriftError as exc:
            await self._reply(target, task_id, {"error": str(exc)})
            return
        await self._reply(
            target, task_id, {"model": self.model, "readings": [r.to_dict() for r in readings]}
        )

    def status(self) -> dict[str, Any]:
        """What the agent watches, what the last check found, and which episodes are raised."""
        return {
            "model": self.model,
            "source": self.source,
            "scores": self.scores,
            "version": self._version,
            "checks": self.checks,
            "checked_at": self.checked_at,
            "thresholds": self.thresholds.to_dict(),
            "readings": [r.to_dict() for r in self.last],
            "raised": [
                {"test": test, "column": column, "observation": node_id}
                for (test, column), node_id in sorted(self._raised.items())
            ],
        }

    # ── One check ─────────────────────────────────────────────────────────

    async def check(self, now: float | None = None) -> list[Reading]:
        """Read both windows, run the tests, raise and settle episodes; every reading. Raises `DriftError`.

        ``now`` is where the current window ends; unsaid, it is the source's
        own now, the newest event time recorded for it held back by
        ``lateness_s``, or the wall clock while nothing is recorded.
        """
        if self.on_node:
            raise DriftError.not_on_main()
        if not self.model:
            raise DriftError.no_model()
        version, source, learned = await asyncio.to_thread(self._served)
        at = await asyncio.to_thread(self.now_of, source) if now is None else now
        reference = self._reference_for(source, learned, at)
        current = Window(source, at - self.window_s, at)
        rows = await self._read(current)
        base = await self._read(reference)
        readings = check_features(
            base, rows, source=source, columns=self.columns or None, thresholds=self.thresholds
        )
        if self.scores:
            score_window = Window(self.scores, current.since, current.until)
            score_reference = Window(self.scores, reference.since, reference.until)
            readings.extend(
                check_scores(
                    await self._read(score_reference),
                    await self._read(score_window),
                    source=self.scores,
                    column=self.score_column,
                    thresholds=self.thresholds,
                )
            )
        self.last = readings
        self.checked_at = at
        self.checks += 1
        model_metrics.record_drift(self.model, readings)
        await self._follow_episodes(version, readings, current, reference, at)
        return readings

    def _served(self) -> tuple[int, str, Window | None]:
        """The version served, the source read, and the window it learned from; off the event loop."""
        store = self.store
        try:
            pointer = store.pointer(self.model, "active")
            versions = store.versions(self.model)
        except ArtifactError as exc:
            raise DriftError(str(exc)) from exc
        if pointer is not None:
            version = pointer.version
        elif versions:
            version = versions[-1].version
        else:
            raise DriftError.no_version(self.model)
        learned = _learned_window(store, self.model, version)
        source = self.source or (learned.source if learned is not None else "")
        if not source:
            raise DriftError.no_source(self.model)
        return version, source, learned

    def _reference_for(self, source: str, learned: Window | None, now: float) -> Window:
        """The reference window: as spawned, else what the version learned from, else the window before."""
        if self.reference is not None:
            return self.reference
        if learned is not None and learned.source == source:
            return learned
        return Window(source, now - 2 * self.window_s, now - self.window_s)

    def now_of(self, source: str) -> float:
        """Where ``source``'s current window ends: its newest event time held back; off the event loop."""
        return now_of(data_root(resolve_state_dir()), source, lateness=self.lateness_s)

    async def _read(self, window: Window) -> list[dict[str, Any]]:
        """The rows of ``window``, through the loader, off the event loop."""
        try:
            from ..data import parquet  # optional dependency: the `data` extra
        except ModuleNotFoundError as exc:
            raise DriftError.no_data() from exc
        root = data_root(resolve_state_dir())

        def read() -> list[dict[str, Any]]:
            return parquet.frame(root, window.source, window.since, window.until).to_pylist()

        return await asyncio.to_thread(read)

    async def _follow_episodes(
        self,
        version: int,
        readings: list[Reading],
        window: Window,
        reference: Window,
        at: float,
    ) -> None:
        """Raise an episode for each alarm not yet raised, and settle those no longer over their threshold."""
        if self._version != version:
            if self._version is not None:
                await self.log(
                    f"{self.model}:{version} is served now; drift is measured from it afresh"
                )
            self._raised.clear()
            self._version = version
            self._recovered = False
        if not self._recovered:
            await self._recover(version, window.since)
        for reading in readings:
            if reading.alarmed and reading.key not in self._raised:
                await self._raise(version, reading, window, reference, at)
            elif not reading.alarmed and reading.key in self._raised:
                self._raised.pop(reading.key)
                await self.log(f"{self.model}: {reading.test} on {reading.column} settled")

    async def _recover(self, version: int, since: float) -> None:
        """Take the episodes of ``version`` the graph holds over the window ahead as still raised."""
        self._recovered = True
        db = get_db()
        if db is None:
            return
        try:
            found = await asyncio.to_thread(
                raised_episodes, Provenance(db), self.model, version=version, since=since
            )
        except Exception as exc:
            logger.warning(
                "[%s] cannot read %s's drift episodes back: %s", self.name, self.model, exc
            )
            return
        self._raised.update(found)

    async def _raise(
        self, version: int, reading: Reading, window: Window, reference: Window, at: float
    ) -> None:
        """Open the episode ``reading`` found: the observation, the line, the topic."""
        node_id = ""
        db = get_db()
        if db is not None:
            try:
                node = await asyncio.to_thread(
                    record_alarm,
                    Provenance(db),
                    model=self.model,
                    version=version,
                    reading=reading,
                    window=window,
                    reference=reference,
                    by=f"agent:{self.name}",
                    at=at,
                )
                node_id = node.id
            except Exception as exc:
                logger.warning("[%s] cannot record drift on %s: %s", self.name, self.model, exc)
        self._raised[reading.key] = node_id
        await self.log(f"{self.model}:{version} drifted: {reading.words()}", level="warning")
        await self.publish(
            drift_topic(self.model),
            {
                "what": "drift",
                "model": self.model,
                "version": version,
                "observation": node_id,
                "ts": reading.at if reading.at is not None else window.until,
                **reading.to_dict(),
                "window": window.to_dict(),
                "reference": reference.to_dict(),
            },
        )

    # ── Saying things ─────────────────────────────────────────────────────

    async def _reply(self, target: str, task_id: Any, reply: dict[str, Any]) -> None:
        if task_id is not None:
            reply = {**reply, "_task_id": task_id}
        await self.send(target, MessageType.RESULT, reply)

    async def log(self, message: str, level: str = "info") -> None:
        """Say something on the dashboard feed, under this agent's name."""
        getattr(logger, level, logger.info)("[%s] %s", self.name, message)
        await self._mqtt_publish(
            agent_topic(self.actor_id, "logs"),
            {"type": "log", "message": message, "level": level, "timestamp": time.time()},
        )


def _learned_window(store: ArtifactStore, model: str, version: int) -> Window | None:
    """The window ``model:version`` was trained on, from the `train` step's metadata; None when nothing says."""
    try:
        artifact = store.resolve(f"{model}:{version}")
    except ArtifactError:
        return None
    window = artifact.metadata.get("window")
    if not isinstance(window, Mapping):
        return None
    source = window.get("source")
    since, until = window.get("since"), window.get("until")
    if not isinstance(source, str) or not source:
        return None
    if isinstance(since, bool) or isinstance(until, bool):
        return None
    if not isinstance(since, (int, float)) or not isinstance(until, (int, float)):
        return None
    if until <= since:
        return None
    return Window(source, float(since), float(until))
