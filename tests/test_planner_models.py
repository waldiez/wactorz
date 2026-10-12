"""The planner names the models it uses, from the ones that are kept.

A pipeline request goes to the planner, not to main, so the planner is told
what main is: the model API, the ``artifacts`` field, and which models are kept
now. A step whose code reads a model its spawn does not name would start and
then fail in ``setup()``, where nobody watches; that is reported with the plan.
"""

import logging
from pathlib import Path
from typing import Any

import pytest

from tests import test_planner_pipeline_context as planner_context
from wactorz.agents.planner import pipeline as pipeline_module
from wactorz.agents.planner.validation import validate_pipeline_code
from wactorz.agents.prompts.planner_prompts import PIPELINE_DESIGN_PROMPT
from wactorz.core.artifacts import ArtifactStore


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    state = tmp_path / "state"
    monkeypatch.setattr(pipeline_module, "resolve_state_dir", lambda: str(state))
    return ArtifactStore.in_state_dir(state)


def stored(store: ArtifactStore, tmp_path: Path, name: str) -> None:
    path = tmp_path / f"{name}.npz"
    path.write_bytes(b"x")
    store.add(path, name)


async def designed_prompt(tmp_path: Path) -> str:
    # Through the module: a test class imported by name would be collected here again.
    decompose = planner_context.TestDecomposePipeline
    planner = planner_context._planner(tmp_path, script={"flag odd imu readings": decompose.PLAN})
    decompose._grounded(planner)
    await planner._decompose_pipeline("flag odd imu readings", [])
    return planner_context._prompt(planner)


class TestWhatThePlannerIsTold:
    def test_the_model_api_is_among_the_only_methods_there_are(self) -> None:
        assert 'await agent.load_model("key")' in PIPELINE_DESIGN_PROMPT
        assert 'agent.artifact_path("key")' in PIPELINE_DESIGN_PROMPT
        assert '"artifacts": {"<key>": "<model>"}' in PIPELINE_DESIGN_PROMPT

    def test_it_reads_a_model_by_the_arrays_listed_for_it(self) -> None:
        assert "read it by exactly those names and shapes" in PIPELINE_DESIGN_PROMPT

    def test_it_reads_a_payload_by_fields_it_was_shown(self) -> None:
        assert "FIELDS: read a payload only by the field names" in PIPELINE_DESIGN_PROMPT
        assert "log its keys with agent.log() the first time" in PIPELINE_DESIGN_PROMPT

    async def test_it_is_shown_the_models_that_are_kept(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        stored(store, tmp_path, "imu-detector")

        prompt = await designed_prompt(tmp_path)

        assert "═══ STORED MODELS ═══" in prompt
        assert "imu-detector — newest imu-detector:1 (npz, 1 B)" in prompt

    async def test_with_none_kept_it_is_told_to_name_none(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        prompt = await designed_prompt(tmp_path)

        assert 'STORED MODELS: none. A step must not name "artifacts".' in prompt

    async def test_a_store_it_cannot_read_is_none_rather_than_no_plan(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        stored(store, tmp_path, "imu-detector")
        store.index_path.write_text("{not json", encoding="utf-8")

        prompt = await designed_prompt(tmp_path)

        assert "STORED MODELS: none." in prompt


def a_step(code: str, artifacts: Any = None) -> dict[str, Any]:
    spawn_config: dict[str, Any] = {"type": "dynamic", "code": code}
    if artifacts is not None:
        spawn_config["artifacts"] = artifacts
    return {"name": "imu-anomaly-detector", "spawn_config": spawn_config}


LOADS = "async def setup(agent):\n    m = await agent.load_model('model')\n"


class TestAPlanThatReadsAModel:
    def test_one_its_spawn_does_not_name_is_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            validate_pipeline_code([a_step(LOADS)], "planner")

        assert "reads model(s) model that its spawn does not name in 'artifacts'" in caplog.text

    def test_one_it_names_is_not(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            validate_pipeline_code([a_step(LOADS, {"model": "imu-detector"})], "planner")

        assert "artifacts" not in caplog.text

    def test_a_path_read_counts_too(self, caplog: pytest.LogCaptureFixture) -> None:
        code = 'p = agent.artifact_path("labels")\nm = await agent.load_model("model")\n'

        with caplog.at_level(logging.WARNING):
            validate_pipeline_code([a_step(code, {"model": "x"})], "planner")

        assert "reads model(s) labels that" in caplog.text

    def test_code_that_reads_none_is_left_alone(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            validate_pipeline_code([a_step("x = 1")], "planner")

        assert not caplog.text


class TestAKeyThatDoesNotMatch:
    """The one model a spawn names, read by another key, is named under that key too."""

    def test_it_is_named_under_the_key_the_code_reads(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        code = "async def setup(agent):\n    m = await agent.load_model('imu_model')\n"
        step = a_step(code, {"model": "imu-detector"})

        with caplog.at_level(logging.WARNING):
            validate_pipeline_code([step], "planner")

        assert step["spawn_config"]["artifacts"] == {
            "model": "imu-detector",
            "imu_model": "imu-detector",
        }
        assert "named in 'artifacts' as 'model': named under both" in caplog.text
        assert "does not name" not in caplog.text

    def test_with_two_models_named_it_is_only_reported(self) -> None:
        step = a_step(LOADS.replace("'model'", "'third'"), {"a": "x", "b": "y"})

        validate_pipeline_code([step], "planner")

        assert step["spawn_config"]["artifacts"] == {"a": "x", "b": "y"}

    def test_with_two_keys_read_it_is_only_reported(self) -> None:
        code = 'a = await agent.load_model("a")\nb = await agent.load_model("b")\n'
        step = a_step(code, {"model": "x"})

        validate_pipeline_code([step], "planner")

        assert step["spawn_config"]["artifacts"] == {"model": "x"}
