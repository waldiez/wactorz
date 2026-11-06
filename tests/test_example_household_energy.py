# SPDX-License-Identifier: Apache-2.0
"""The household-energy example does what its README says.

The house is the same on every run, the agent finds each run the meter shows
and only those, the first version mistakes the two runs written to fool the
watts and the second, which also looks at how long a run lasted, names every
one, the agent asks once per run with a label over exactly it, and the package
brings its two agents, its tab and the tab's icon. Imported from its folder, as
the README's `pip install -e` would make it importable.
"""

import importlib
import itertools
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

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "household_energy"


@pytest.fixture(name="he")
def he_fixture(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """The example's modules, imported from its folder."""
    monkeypatch.syspath_prepend(str(EXAMPLE))
    return types.SimpleNamespace(
        plant=importlib.import_module("household_energy.plant"),
        model=importlib.import_module("household_energy.model"),
        agents=importlib.import_module("household_energy.agents"),
        train=importlib.import_module("household_energy.train"),
        tab=importlib.import_module("household_energy.tab"),
    )


def _saturday(he: types.SimpleNamespace) -> list[dict[str, Any]]:
    plant = he.plant
    return he.model.find_runs(plant.readings(plant.SATURDAY, plant.SATURDAY + plant.DAY_S))


def _guesses(he: types.SimpleNamespace, features: tuple[str, ...]) -> list[str]:
    fitted = he.model.fit(he.train.noted_week(), features)
    return [he.model.classify(fitted, run)[0] for run in _saturday(he)]


class TestTheHouse:
    def test_a_reading_is_the_same_whoever_asks_and_in_any_order(
        self, he: types.SimpleNamespace
    ) -> None:
        plant = he.plant
        at = plant.SATURDAY + 7 * 3600

        later_first = [plant.reading(at + 600), plant.reading(at)]

        assert plant.reading(at) == later_first[1]
        assert plant.reading(at, seed=1) != later_first[1]

    def test_each_run_is_on_while_it_says(self, he: types.SimpleNamespace) -> None:
        plant = he.plant
        kettle = plant.PLAYED[0]

        assert plant.run_at(kettle.start) is kettle
        assert plant.run_at(kettle.end) is None
        assert plant.watts(kettle.start) > plant.watts(kettle.start - 60) + 1500

    def test_no_two_runs_of_a_day_overlap(self, he: types.SimpleNamespace) -> None:
        plant = he.plant
        for day in range(7):
            runs = plant.day_runs(plant.WEEK + day * plant.DAY_S)
            assert all(a.end <= b.start for a, b in itertools.pairwise(runs)), day


class TestFindingRuns:
    def test_it_finds_every_run_of_the_saturday_and_nothing_else(
        self, he: types.SimpleNamespace
    ) -> None:
        found = _saturday(he)

        assert [(r["ts"], r["end"], r["minutes"]) for r in found] == [
            (run.start, run.end, run.minutes) for run in he.plant.PLAYED
        ]
        for run, written in zip(found, he.plant.PLAYED):
            assert abs(run["watts"] - written.watts) < 100
            assert run["device"] == he.plant.METER

    def test_the_fridge_is_not_a_run(self, he: types.SimpleNamespace) -> None:
        plant = he.plant
        night = plant.readings(plant.SATURDAY, plant.SATURDAY + 6 * 3600)

        assert he.model.find_runs(night) == []

    def test_the_noted_week_has_every_appliance(self, he: types.SimpleNamespace) -> None:
        noted = he.train.noted_week()

        assert {r["appliance"] for r in noted} == set(he.plant.APPLIANCES)


class TestTheModel:
    def test_the_watts_alone_mistake_the_two_runs_written_to_fool_them(
        self, he: types.SimpleNamespace
    ) -> None:
        guessed = _guesses(he, he.model.FIRST_FEATURES)
        wrong = [
            (run.appliance, g) for run, g in zip(he.plant.PLAYED, guessed) if g != run.appliance
        ]

        assert wrong == [("kettle", "washer"), ("oven", "kettle")]

    def test_how_long_it_ran_names_every_one(self, he: types.SimpleNamespace) -> None:
        assert _guesses(he, he.model.FEATURES) == [run.appliance for run in he.plant.PLAYED]


class TestTheClassifier:
    async def test_it_answers_with_the_appliance_in_its_own_field(
        self, he: types.SimpleNamespace, tmp_path: Path
    ) -> None:
        he.train.train(Path(resolve_state_dir()), features=he.model.FEATURES)
        spec = spec_of(he.agents.guess)
        assert spec is not None and spec.models == {"model": "appliances"}
        me = spec.build(persistence_dir=str(tmp_path / "agents"))
        await me._resolve_models()
        washer = _saturday(he)[3]

        guessed = await he.agents.guess(washer, me)

        assert guessed == {**washer, "appliance": "washer"}

    async def test_what_it_looks_at_is_the_versions_own(
        self, he: types.SimpleNamespace, tmp_path: Path
    ) -> None:
        he.train.train(Path(resolve_state_dir()))
        spec = spec_of(he.agents.guess)
        assert spec is not None
        me = spec.build(persistence_dir=str(tmp_path / "agents"))
        await me._resolve_models()
        weak_kettle = _saturday(he)[4]

        assert (await he.agents.guess(weak_kettle, me))["appliance"] == "washer"


class TestAskingAboutIt:
    async def test_it_publishes_each_run_and_asks_about_its_guess_with_a_label_over_it(
        self, he: types.SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = he.agents.EnergyRuns(name="energy-runs", persistence_dir=str(tmp_path))
        agent.finder = he.model.RunFinder()
        told: list[tuple[str, Any]] = []
        asked: list[dict[str, Any]] = []

        async def publish(topic: str, payload: Any, **_: Any) -> None:
            told.append((topic, payload))

        async def ask_operator(text: str, **kwargs: Any) -> str:
            asked.append({"text": text, **kwargs})
            return "Kettle"

        monkeypatch.setattr(agent, "publish", publish)
        monkeypatch.setattr(agent, "ask_operator", ask_operator)
        plant = he.plant
        for r in plant.readings(plant.SATURDAY + 11 * 3600, plant.SATURDAY + 11.5 * 3600):
            await agent.on_reading(r)
        ((topic, run),) = told
        assert topic == "energy/runs" and run["minutes"] == 4

        await agent.on_guess({**run, "appliance": "washer"})
        for task in list(agent._tasks):
            await task

        (question,) = asked
        assert re.match(
            r"11:15–11:19 UTC: about 2,0\d\d W for 4 minutes\. My guess: washing", question["text"]
        )
        assert question["options"][0] == "Washing machine"
        assert sorted(question["options"]) == sorted(he.agents.ANSWERS)
        assert question["label"] == {
            "source": "meter",
            "start": run["ts"],
            "end": run["end"],
            "values": he.agents.ANSWERS,
        }
        assert told[-1] == (
            "energy/answers",
            {
                "device": "meter",
                "ts": run["ts"],
                "end": run["end"],
                "guess": "washer",
                "answer": "kettle",
            },
        )

    async def test_it_ignores_what_is_not_a_reading_or_a_guess(
        self, he: types.SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = he.agents.EnergyRuns(name="energy-runs", persistence_dir=str(tmp_path))
        agent.finder = he.model.RunFinder()
        monkeypatch.setattr(agent, "publish", _refused)
        monkeypatch.setattr(agent, "ask_operator", _refused)

        await agent.on_reading("not a reading")
        await agent.on_guess({"ts": 1.0})

        assert not agent._tasks


async def _refused(*_: Any, **__: Any) -> Any:
    raise AssertionError("nothing should be sent")


class TestThePackage:
    def test_it_names_its_two_agents_and_its_tab(self, he: types.SimpleNamespace) -> None:
        # The example needs Python 3.11, which is where tomllib comes from.
        tomllib = pytest.importorskip("tomllib")
        declared = tomllib.loads((EXAMPLE / "pyproject.toml").read_text(encoding="utf-8"))
        points = declared["project"]["entry-points"]

        assert points["wactorz.agents"] == {
            "energy-guess": "household_energy.agents:guess",
            "energy-runs": "household_energy.agents:EnergyRuns",
        }
        assert points["wactorz.extensions"] == {"energy": "household_energy.tab"}
        assert (
            spec_of(he.agents.guess) is not None
            and he.agents.EnergyRuns.AGENT_NAME == "energy-runs"
        )

    def test_its_tab_is_a_panel_with_a_script_that_mounts_and_an_icon_of_its_own(
        self, he: types.SimpleNamespace
    ) -> None:
        he.tab.setup(None)
        panel = he.tab.panel(None)
        script = Path(panel.script).read_text(encoding="utf-8")

        assert panel.title == "Energy"
        assert set(panel.topics) == {"sensors/energy/#", "energy/#"}
        assert "export const apiVersion = 1;" in script
        assert "export function mount(root, host)" in script
        assert "innerHTML" not in script
        icon = icon_file_text(panel)
        assert icon is not None and 'viewBox="0 0 24 24"' in icon

    def test_training_stores_what_each_version_looks_at(
        self, he: types.SimpleNamespace, tmp_path: Path
    ) -> None:
        state = tmp_path / "state"

        he.train.main(["--state-dir", str(state)])
        he.train.main(["--state-dir", str(state), "--features", "watts,minutes", "--set", "shadow"])

        store = ArtifactStore.in_state_dir(state)
        assert [v.metadata["features"] for v in store.versions("appliances")] == [
            ["watts"],
            ["watts", "minutes"],
        ]
        shadow = store.pointer("appliances", "shadow")
        assert shadow is not None and shadow.version == 2

    def test_training_refuses_a_feature_it_does_not_have(
        self, he: types.SimpleNamespace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            he.train.main(["--state-dir", str(tmp_path), "--features", "colour"])

        assert "no feature 'colour'" in capsys.readouterr().err
