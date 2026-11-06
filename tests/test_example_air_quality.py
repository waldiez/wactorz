# SPDX-License-Identifier: Apache-2.0
"""The air-quality example does what its README says.

The building is the same on every run, its faults stand out from what is usual
for the hour and an ordinary busy day does not, the agent asks once per
episode with a label over its window, and the package brings its two agents
and its tab. Imported from its folder, as the README's `pip install -e` would
make it importable.
"""

import importlib
import re
import types
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.function_agent import spec_of
from wactorz.core.artifacts import ArtifactStore
from wactorz.core.paths import resolve_state_dir
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
        }
        assert points["wactorz.extensions"] == {"air": "air_quality.tab"}
        assert (
            spec_of(aq.agents.detect) is not None and aq.agents.AirWatch.AGENT_NAME == "air-watch"
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
