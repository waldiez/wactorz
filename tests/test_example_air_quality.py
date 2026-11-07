# SPDX-License-Identifier: Apache-2.0
"""The air-quality example does what its README says.

The building is the same on every run, its faults stand out from what is usual
for the hour and an ordinary busy day does not, the agent asks once per
episode with a label over its window, and the package brings its two agents,
the four of the loop and its tab. The loop runs without a person: the
recorder files the readings and the scores apart, the drift agent alarms on
the scores at the Tuesday's faults, the rule sends the trainer a task cut
where the alarm says, and the trainer fits the recipe on what was recorded.
Imported from its folder, as the README's `pip install -e` would make it
importable.
"""

import importlib
import re
import types
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.function_agent import spec_of
from wactorz.core.actor import Message
from wactorz.core.artifacts import ArtifactStore
from wactorz.core.paths import resolve_state_dir
from wactorz.data import data_root, sources
from wactorz.ext._panel import icon_file_text

pytest.importorskip("numpy")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "air_quality"


@pytest.fixture(name="aq")
def aq_fixture(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """The example's modules, imported from its folder."""
    monkeypatch.syspath_prepend(str(EXAMPLE))
    return types.SimpleNamespace(
        plant=importlib.import_module("air_quality.plant"),
        model=importlib.import_module("air_quality.model"),
        agents=importlib.import_module("air_quality.agents"),
        train=importlib.import_module("air_quality.train"),
        tab=importlib.import_module("air_quality.tab"),
        recipe=importlib.import_module("air_quality.recipe"),
        loop=importlib.import_module("air_quality.loop"),
    )


@pytest.fixture(name="fitted")
def fitted_fixture(aq: types.SimpleNamespace) -> dict[str, Any]:
    plant = aq.plant
    return aq.model.fit(plant.readings(plant.WEEK, plant.WEEK + 7 * plant.DAY_S, faults=False))


def _scores(aq: types.SimpleNamespace, fitted: dict[str, Any], *, faults: bool) -> list[Any]:
    """Every reading of the played Tuesday with its score."""
    plant = aq.plant
    tuesday = plant.readings(plant.TUESDAY, plant.TUESDAY + plant.DAY_S, faults=faults)
    return [(r, aq.model.score(fitted, r)[0]) for r in tuesday]


class TestTheBuilding:
    def test_a_reading_is_the_same_whoever_asks_and_in_any_order(
        self, aq: types.SimpleNamespace
    ) -> None:
        plant = aq.plant
        at = plant.TUESDAY + 10.5 * 3600

        later_first = [plant.reading("kitchen", at + 600), plant.reading("kitchen", at)]

        assert plant.reading("kitchen", at) == later_first[1]
        assert plant.reading("kitchen", at, seed=1) != later_first[1]

    def test_each_fault_is_where_it_says(self, aq: types.SimpleNamespace) -> None:
        plant = aq.plant
        window, ventilation, sensor = plant.FAULTS
        inside = plant.reading("meeting-room", window.start + 600)
        usual = plant.reading("meeting-room", window.start + 600, faults=False)

        assert inside["co2"] < usual["co2"] - 500 and inside["temperature"] < usual["temperature"]
        assert plant.reading("kitchen", sensor.start)["humidity"] == 99.0
        assert plant.reading("kitchen", sensor.end)["humidity"] < 99.0
        assert plant.fault_at("open-office", ventilation.start - 1) is None

    def test_nobody_is_in_at_night_or_at_the_weekend(self, aq: types.SimpleNamespace) -> None:
        plant = aq.plant
        saturday_noon = plant.WEEK + 5 * plant.DAY_S + 12 * 3600

        assert plant.occupancy("open-office", plant.TUESDAY + 3 * 3600) == 0.0
        assert plant.occupancy("open-office", saturday_noon) == 0.0
        assert plant.occupancy("open-office", plant.TUESDAY + 12 * 3600) > 0.8


class TestTheModel:
    def test_an_ordinary_tuesday_stays_under_the_threshold(
        self, aq: types.SimpleNamespace, fitted: dict[str, Any]
    ) -> None:
        worst = max(value for _, value in _scores(aq, fitted, faults=False))

        assert worst < aq.model.K_SIGMA

    def test_every_fault_is_past_it_almost_throughout_and_nothing_else_is(
        self, aq: types.SimpleNamespace, fitted: dict[str, Any]
    ) -> None:
        plant = aq.plant
        scored = _scores(aq, fitted, faults=True)

        for fault in plant.FAULTS:
            inside = [v for r, v in scored if plant.fault_at(r["device"], r["ts"]) is fault]
            assert sum(v >= aq.model.K_SIGMA for v in inside) >= len(inside) - 1, fault.kind
        outside = [v for r, v in scored if plant.fault_at(r["device"], r["ts"]) is None]
        assert max(outside) < aq.model.K_SIGMA

    def test_the_furthest_quantity_is_named(
        self, aq: types.SimpleNamespace, fitted: dict[str, Any]
    ) -> None:
        sensor = aq.plant.FAULTS[2]

        _, worst = aq.model.score(fitted, aq.plant.reading("kitchen", sensor.start + 600))

        assert worst == "humidity"


class TestTheDetector:
    async def test_it_publishes_every_score_and_returns_only_an_alarm(
        self, aq: types.SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aq.train.train(Path(resolve_state_dir()), k_sigma=5.0)
        spec = spec_of(aq.agents.detect)
        assert spec is not None and spec.models == {"model": "air-quality"}
        me = spec.build(persistence_dir=str(tmp_path / "agents"))
        sent: list[tuple[str, Any]] = []

        async def publish(topic: str, payload: Any, **_: Any) -> None:
            sent.append((topic, payload))

        monkeypatch.setattr(me, "publish", publish)
        await me._resolve_models()
        plant = aq.plant

        alarm = await aq.agents.detect(plant.reading("kitchen", plant.FAULTS[2].start), me)
        quiet = await aq.agents.detect(plant.reading("kitchen", plant.TUESDAY + 9 * 3600), me)

        assert alarm is not None and alarm["worst"] == "humidity"
        assert quiet is None
        assert [topic for topic, _ in sent] == ["air/kitchen/score"] * 2
        assert {payload["threshold"] for _, payload in sent} == {5.0}

    async def test_the_threshold_is_the_versions_own(
        self, aq: types.SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aq.train.train(Path(resolve_state_dir()), k_sigma=500.0)
        spec = spec_of(aq.agents.detect)
        assert spec is not None
        me = spec.build(persistence_dir=str(tmp_path / "agents"))
        monkeypatch.setattr(me, "publish", _ignored)
        await me._resolve_models()

        alarm = await aq.agents.detect(aq.plant.reading("kitchen", aq.plant.FAULTS[2].start), me)

        assert alarm is None


async def _ignored(*_: Any, **__: Any) -> None:
    return None


def _scored(ts: float, value: float, threshold: float = 5.0) -> dict[str, Any]:
    return {
        "device": "kitchen",
        "ts": ts,
        "score": value,
        "threshold": threshold,
        "worst": "humidity",
    }


class TestAnEpisode:
    def test_it_opens_after_two_readings_past_the_threshold_and_closes_after_three_below(
        self, aq: types.SimpleNamespace
    ) -> None:
        episode = aq.agents.Episode()
        said = [
            aq.agents.follow(episode, _scored(i * 300.0, v))
            for i, v in enumerate([1, 7, 9, 8, 1, 2, 1])
        ]

        assert said == [None, None, "opened", None, None, None, "closed"]
        assert (episode.start, episode.last, episode.peak) == (300.0, 900.0, 9)

    def test_a_reading_past_it_that_does_not_last_is_no_episode(
        self, aq: types.SimpleNamespace
    ) -> None:
        episode = aq.agents.Episode()
        said = [
            aq.agents.follow(episode, _scored(i * 300.0, v))
            for i, v in enumerate([7, 1, 1, 1, 6, 6])
        ]

        assert said == [None, None, None, None, None, "opened"]
        assert episode.start == 1200.0


class TestAskingAboutIt:
    async def test_it_asks_once_an_episode_ends_with_a_label_over_its_window(
        self, aq: types.SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        watch = aq.agents.AirWatch(name="air-watch", persistence_dir=str(tmp_path))
        watch.rooms = {}
        told: list[dict[str, Any]] = []
        asked: list[dict[str, Any]] = []

        async def publish(topic: str, payload: Any, **_: Any) -> None:
            told.append(payload)

        async def ask_operator(text: str, **kwargs: Any) -> str:
            asked.append({"text": text, **kwargs})
            return "Sensor fault"

        monkeypatch.setattr(watch, "publish", publish)
        monkeypatch.setattr(watch, "ask_operator", ask_operator)
        start = aq.plant.TUESDAY + 15 * 3600
        for i, v in enumerate([1, 60, 60, 60, 1, 1, 1, 1, 1]):
            await watch.on_score(_scored(start + i * 300, v))
        for task in list(watch._tasks):
            await task

        (question,) = asked
        assert re.match(r"Kitchen, 15:05–15:20 UTC: humidity", question["text"])
        assert question["options"] == list(aq.agents.ANSWERS)
        assert question["label"] == {
            "source": "kitchen",
            "start": start + 300,
            "end": start + 1200,
            "values": aq.agents.ANSWERS,
        }
        assert [t["state"] for t in told] == ["open", "closed", "answered"]
        assert told[-1]["answer"] == "Sensor fault"


class TestThePackage:
    def test_it_names_its_two_agents_and_its_tab(self, aq: types.SimpleNamespace) -> None:
        # The example needs Python 3.11, which is where tomllib comes from.
        tomllib = pytest.importorskip("tomllib")
        declared = tomllib.loads((EXAMPLE / "pyproject.toml").read_text(encoding="utf-8"))
        points = declared["project"]["entry-points"]

        assert points["wactorz.agents"] == {
            "air-detect": "air_quality.agents:detect",
            "air-watch": "air_quality.agents:AirWatch",
            "air-recorder": "air_quality.loop:AirRecorder",
            "air-drift": "air_quality.loop:AirDrift",
            "air-trainer": "air_quality.loop:AirTrainer",
            "air-retrain": "air_quality.loop:AirRetrain",
        }
        assert points["wactorz.extensions"] == {"air": "air_quality.tab"}
        assert (
            spec_of(aq.agents.detect) is not None and aq.agents.AirWatch.AGENT_NAME == "air-watch"
        )
        loop = aq.loop
        assert [
            c.AGENT_NAME
            for c in (loop.AirRecorder, loop.AirDrift, loop.AirTrainer, loop.AirRetrain)
        ] == [
            "air-recorder",
            "air-drift",
            "air-trainer",
            "air-retrain",
        ]
        assert all(
            c.AUTOSTART for c in (loop.AirRecorder, loop.AirDrift, loop.AirTrainer, loop.AirRetrain)
        )

    def test_its_tab_is_a_panel_with_a_script_that_mounts_and_an_icon_of_its_own(
        self, aq: types.SimpleNamespace
    ) -> None:
        aq.tab.setup(None)
        panel = aq.tab.panel(None)
        script = Path(panel.script).read_text(encoding="utf-8")

        assert panel.title == "Air"
        assert set(panel.topics) == {"sensors/air/#", "air/#"}
        assert "export const apiVersion = 1;" in script
        assert "export function mount(root, host)" in script
        assert "innerHTML" not in script
        icon = icon_file_text(panel)
        assert icon is not None and 'viewBox="0 0 24 24"' in icon

    def test_training_stores_the_threshold_with_the_version(
        self, aq: types.SimpleNamespace, tmp_path: Path
    ) -> None:
        state = tmp_path / "state"

        aq.train.main(["--state-dir", str(state)])
        aq.train.main(["--state-dir", str(state), "--k-sigma", "4", "--set", "shadow"])

        store = ArtifactStore.in_state_dir(state)
        assert [v.metadata["k_sigma"] for v in store.versions("air-quality")] == [5.0, 4.0]
        shadow = store.pointer("air-quality", "shadow")
        assert shadow is not None and shadow.version == 2
        plant = aq.plant
        assert store.versions("air-quality")[0].metadata["window"] == {
            "source": "sensors/air",
            "since": plant.WEEK,
            "until": plant.WEEK + 7 * plant.DAY_S,
        }

    def test_the_threshold_is_read_from_wherever_a_version_carries_it(
        self, aq: types.SimpleNamespace
    ) -> None:
        threshold_of = aq.agents.threshold_of

        assert threshold_of({"k_sigma": 4}, {}) == 4.0
        assert threshold_of({"settings": {"k_sigma": 3}}, {}) == 3.0
        assert threshold_of({}, {"k_sigma": 2}) == 2.0
        assert threshold_of({}, {}) == aq.model.K_SIGMA


# ── The loop without a person ─────────────────────────────────────────────────


class FakeRegistry:
    """What a rule sends its task through; stands in for the actor registry."""

    def __init__(self, *actors: Any) -> None:
        self._by_name = {a.name: a for a in actors}
        self.delivered: list[tuple[str, Message]] = []

    def find_by_name(self, name: str) -> Any:
        return self._by_name.get(name)

    async def deliver(self, target_id: str, msg: Message) -> bool:
        self.delivered.append((target_id, msg))
        return True


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name}-id"


@pytest.fixture(name="published")
def published_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """What the loop's agents say on the broker: the feed lines and the alarms."""
    seen: list[tuple[str, Any]] = []

    async def record(self: Any, topic: str, payload: Any, **_kwargs: Any) -> None:
        seen.append((topic, payload))

    from wactorz.core.actor import Actor

    monkeypatch.setattr(Actor, "_mqtt_publish", record)
    return seen


def _arrow() -> Any:
    return pytest.importorskip("pyarrow")


def _frame(rows: list[dict[str, Any]], labels: dict[str, str] | None = None) -> Any:
    """``rows`` as the loader hands them to a recipe, with a label column when ``labels`` names rooms."""
    pa = _arrow()
    kept = [
        {
            **row,
            "arrived": row["ts"],
            "time_source": "payload",
            "label": (labels or {}).get(row["device"]),
        }
        for row in rows
    ]
    return pa.Table.from_pylist(kept)


@pytest.fixture(name="recorded")
async def recorded_fixture(
    aq: types.SimpleNamespace, tmp_path: Path, published: list[tuple[str, Any]]
) -> AsyncIterator[Any]:
    """The healthy week and the Tuesday to 16:00, readings and v1's scores, filed as the recorder files them; v1 active."""
    _arrow()
    plant, model, loop = aq.plant, aq.model, aq.loop
    state = Path(resolve_state_dir())
    v1 = aq.train.train(state)
    fitted = aq.model.fit(plant.readings(plant.WEEK, plant.WEEK + 7 * plant.DAY_S, faults=False))
    recorder = loop.AirRecorder(persistence_dir=str(tmp_path / "agents"), batch_rows=100000)
    for reading in [
        *plant.readings(plant.WEEK, plant.WEEK + 7 * plant.DAY_S, faults=False),
        *plant.readings(plant.TUESDAY + 7 * 3600, plant.TUESDAY + 16 * 3600),
    ]:
        value, worst = model.score(fitted, reading)
        await recorder.record(dict(reading), "sensors/air/#")
        await recorder.record(
            {
                "device": reading["device"],
                "ts": reading["ts"],
                "score": round(value, 2),
                "worst": worst,
            },
            "air/+/score",
        )
    await recorder.flush()
    yield types.SimpleNamespace(version=v1, state=state)


class TestTheLoop:
    def test_the_recorder_files_the_readings_and_the_scores_apart(
        self, aq: types.SimpleNamespace, recorded: Any
    ) -> None:
        assert set(sources(data_root(recorded.state))) == {"sensors/air", "air/scores"}

    def test_the_recipe_fits_the_usual_and_scores_the_rest(self, aq: types.SimpleNamespace) -> None:
        plant, recipe = aq.plant, aq.recipe
        healthy = list(plant.readings(plant.WEEK, plant.WEEK + 7 * plant.DAY_S, faults=False))
        tuesday = list(plant.readings(plant.TUESDAY, plant.TUESDAY + plant.DAY_S))
        labelled = _frame(tuesday, {"kitchen": "sensor_fault"})

        model = recipe.train(_frame(healthy), {"k_sigma": 4})
        figures = recipe.score(model, labelled)

        assert float(model["k_sigma"]) == 4.0 and recipe.threshold_of(model) == 4.0
        assert set(model) == {
            "k_sigma",
            *(f"{r}.{p}" for r in plant.ROOMS for p in ("mean", "spread")),
        }
        assert set(figures) == {"false_alarms", "labelled_faults", "caught"}
        assert figures["labelled_faults"] == 288.0
        assert 0.0 < figures["false_alarms"] < 0.2 and figures["caught"] > 0.1
        # A fit leaves labelled faults out, so the kitchen's usual is the healthy week's.
        refit = recipe.train(labelled, {})
        assert refit["kitchen.mean"].sum() == 0.0

    async def test_the_drift_agent_alarms_on_the_scores_at_the_tuesdays_faults(
        self,
        aq: types.SimpleNamespace,
        recorded: Any,
        tmp_path: Path,
        published: list[tuple[str, Any]],
    ) -> None:
        plant, loop = aq.plant, aq.loop
        agent = loop.AirDrift(persistence_dir=str(tmp_path / "drift"))

        readings = await agent.check()

        # The source's own now: the Tuesday's newest reading, held back a minute.
        assert agent.checked_at == plant.TUESDAY + 16 * 3600 - 300 - 60
        by_key = {(r.test, r.column): r for r in readings}
        assert by_key[("adwin", "score")].alarmed
        cut = by_key[("adwin", "score")].at
        assert plant.TUESDAY + 12 * 3600 <= cut <= plant.TUESDAY + 14 * 3600
        alarms = [p for t, p in published if t == "models/air-quality/drift"]
        assert {a["column"] for a in alarms} >= {"score"}
        assert [a["at"] for a in alarms if a["test"] == "adwin"] == [cut]
        assert all(
            a["reference"]
            == {"source": "sensors/air", "since": plant.WEEK, "until": plant.WEEK + 7 * plant.DAY_S}
            for a in alarms
            if a["column"] != "score"
        )

    async def test_the_rule_sends_the_trainer_a_task_cut_at_the_alarm_once_a_day(
        self, aq: types.SimpleNamespace, tmp_path: Path, published: list[tuple[str, Any]]
    ) -> None:
        plant, loop = aq.plant, aq.loop
        rule = loop.AirRetrain(persistence_dir=str(tmp_path / "rule"))
        registry = FakeRegistry(_Agent("air-trainer"))
        rule._registry = registry  # type: ignore[assignment]  # stands in for ActorRegistry
        cut = plant.TUESDAY + 13 * 3600
        alarm = {
            "what": "drift",
            "model": "air-quality",
            "version": 1,
            "observation": "obs-1",
            "test": "adwin",
            "column": "score",
            "statistic": 3.0,
            "ts": cut,
            "at": cut,
        }

        assert not await rule.evaluate({**alarm, "test": "ks", "column": "co2", "at": None})
        assert await rule.evaluate(alarm)
        assert not await rule.evaluate({**alarm, "test": "psi", "ts": cut + 3600, "at": None})
        assert await rule.evaluate(
            {**alarm, "ts": cut + 86400, "at": cut + 86400, "observation": "obs-2"}
        )

        (target, first), (_, second) = registry.delivered
        assert target == "air-trainer-id"
        assert first.payload["split"] == {"kind": "time_split", "at": cut}
        assert first.payload["observation"] == "obs-1"
        assert first.payload["reason"] == "drift: adwin on score (3.0)"
        assert second.payload["observation"] == "obs-2"

    async def test_the_trainer_fits_the_recipe_on_what_was_recorded_and_moves_the_shadow(
        self,
        aq: types.SimpleNamespace,
        recorded: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plant, loop = aq.plant, aq.loop
        monkeypatch.setenv("PYTHONPATH", str(EXAMPLE))
        trainer = loop.AirTrainer(persistence_dir=str(tmp_path / "trainer"))
        cut = plant.TUESDAY + 13 * 3600

        spec = await trainer.spec_for_task(
            {"split": {"kind": "time_split", "at": cut}, "reason": "drift: adwin on score"}
        )
        outcome = await trainer.train(spec)

        # The window ends at the source's own now, the Tuesday's newest reading held back.
        assert spec.until == plant.TUESDAY + 16 * 3600 - 300 - 60
        assert spec.since == spec.until - 7 * plant.DAY_S
        assert outcome.ok, outcome.error
        assert outcome.artifact is not None and outcome.artifact.version == 2
        metadata = outcome.artifact.metadata
        assert metadata["window"] == {
            "source": "sensors/air",
            "since": spec.since,
            "until": spec.until,
        }
        assert metadata["settings"] == {"k_sigma": 5.0}
        assert set(metadata["metrics"]["new"]) >= {"false_alarms", "labelled_faults"}
        assert metadata["metrics"]["parent"] is not None
        store = ArtifactStore.in_state_dir(recorded.state)
        shadow = store.pointer("air-quality", "shadow")
        assert shadow is not None and shadow.version == 2, outcome.not_moved
