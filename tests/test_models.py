"""An agent names its models, finds them before it starts, and loads them safely.

A library agent names them with ``models=`` on ``@wactorz.agent`` or ``MODELS``
on an ``Actor``; a generated one through its spawn's ``artifacts``. Each is
found in main's store before ``on_start``, so an agent whose model is missing
does not start. ``load_model`` reads the data formats the way that runs no code
from the file, and refuses the formats that do unless told otherwise.
"""

import pickle
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest

import wactorz
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.function_agent import FunctionAgent, spec_of
from wactorz.core import actor as actor_module
from wactorz.core import models
from wactorz.core.actor import Actor, Message
from wactorz.core.artifacts import ArtifactError, ArtifactStore


@pytest.fixture(name="state")
def state_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Main's state directory, where an actor starting looks for its models."""
    state = tmp_path / "state"
    monkeypatch.setattr(actor_module, "resolve_state_dir", lambda: str(state))
    return state


@pytest.fixture(name="store")
def store_fixture(state: Path) -> ArtifactStore:
    return ArtifactStore.in_state_dir(state)


@pytest.fixture(name="imu")
def imu_fixture(store: ArtifactStore, tmp_path: Path) -> dict[str, np.ndarray]:
    """`imu-detector:1`: a mean and an inverse covariance, as the IMU example keeps them."""
    arrays = {"mean": np.array([0.0, 0.0, 9.81]), "inverse_covariance": np.eye(3)}
    path = tmp_path / "imu.npz"
    np.savez(path, **arrays)
    store.add(path, "imu-detector")
    return arrays


def entry(path: Path, fmt: str) -> dict[str, Any]:
    return {"name": "m", "version": 1, "format": fmt, "path": str(path)}


# ── Loading ───────────────────────────────────────────────────────────────────


class TestLoading:
    def test_npz_loads_as_a_dict_of_arrays(self, tmp_path: Path) -> None:
        path = tmp_path / "m.npz"
        np.savez(path, mean=np.arange(3), cov=np.eye(2))

        loaded = models.load(entry(path, "npz"))

        assert set(loaded) == {"mean", "cov"}
        np.testing.assert_array_equal(loaded["mean"], np.arange(3))

    def test_npy_loads_as_the_array(self, tmp_path: Path) -> None:
        path = tmp_path / "m.npy"
        np.save(path, np.arange(4))

        np.testing.assert_array_equal(models.load(entry(path, "npy")), np.arange(4))

    def test_an_array_of_objects_is_not_unpickled(self, tmp_path: Path) -> None:
        # Saved with pickle inside: loading it would run code, so it is refused.
        path = tmp_path / "m.npy"
        np.save(path, np.array([{"a": 1}], dtype=object), allow_pickle=True)

        with pytest.raises(ValueError, match="allow_pickle"):
            models.load(entry(path, "npy"))

    def test_raw_loads_as_the_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "m.bin"
        path.write_bytes(b"\x00\x01")

        assert models.load(entry(path, "raw")) == b"\x00\x01"

    @pytest.mark.parametrize("fmt", ["pickle", "joblib", "torch"])
    def test_what_runs_code_is_refused_unless_allowed(self, tmp_path: Path, fmt: str) -> None:
        path = tmp_path / "m"
        path.write_bytes(pickle.dumps({"weights": [1, 2]}))

        with pytest.raises(ArtifactError, match="runs code"):
            models.load(entry(path, fmt))

    def test_a_pickle_loads_when_allowed(self, tmp_path: Path) -> None:
        path = tmp_path / "m.pkl"
        path.write_bytes(pickle.dumps({"weights": [1, 2]}))

        assert models.load(entry(path, "pickle"), allow_code=True) == {"weights": [1, 2]}

    @pytest.mark.parametrize("fmt", ["gguf", "tflite", "skops", "dir"])
    def test_a_format_with_no_loader_points_at_the_path(self, tmp_path: Path, fmt: str) -> None:
        with pytest.raises(ArtifactError, match="artifact_path"):
            models.load(entry(tmp_path / "m", fmt))

    @pytest.mark.parametrize(
        ("fmt", "module"),
        [
            ("onnx", "onnxruntime"),
            ("safetensors", "safetensors"),
            ("state_dict", "torch"),
            ("npz", "numpy"),
        ],
    )
    def test_a_missing_library_is_named_with_how_to_install_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str, module: str
    ) -> None:
        # None in sys.modules makes the import fail, whether or not it is installed.
        monkeypatch.setitem(sys.modules, module, None)
        monkeypatch.setitem(sys.modules, f"{module}.numpy", None)

        with pytest.raises(ArtifactError, match=f"pip install {module}"):
            models.load(entry(tmp_path / "m", fmt))


# ── An Actor that names its models ────────────────────────────────────────────


class Detector(Actor):
    MODELS: ClassVar[dict[str, Any]] = {"model": "imu-detector"}

    #: What `on_start` found, so a test can see models are there by then.
    seen_at_start: str | None = None

    async def on_start(self) -> None:
        self.seen_at_start = self.artifact_path("model")

    async def handle_message(self, msg: Message) -> None:
        return None


@pytest.fixture(name="detector")
async def detector_fixture(tmp_path: Path) -> AsyncIterator[Detector]:
    detector = Detector(name="detector", persistence_dir=str(tmp_path / "agents"))
    yield detector
    await detector.stop()


class TestAnActorsModels:
    async def test_they_are_found_before_on_start(
        self, detector: Detector, store: ArtifactStore, imu: dict[str, np.ndarray]
    ) -> None:
        await detector.start()

        held = store.resolve("imu-detector:1")
        assert detector.seen_at_start == str(store.path_of(held))
        assert detector.artifacts["model"]["version"] == 1

    async def test_load_model_reads_it_once(
        self,
        detector: Detector,
        imu: dict[str, np.ndarray],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await detector.start()
        calls: list[str] = []
        real = models.load

        def counting(found: dict[str, Any], **kwargs: Any) -> Any:
            calls.append(found["name"])
            return real(found, **kwargs)

        monkeypatch.setattr(models, "load", counting)

        first = await detector.load_model("model")
        again = await detector.load_model("model")

        assert first is again
        assert calls == ["imu-detector"]
        np.testing.assert_array_equal(first["mean"], imu["mean"])

    async def test_one_that_is_not_stored_stops_it_starting(
        self, detector: Detector, store: ArtifactStore
    ) -> None:
        with pytest.raises(ArtifactError, match="detector cannot start without its models"):
            await detector.start()

        assert detector.seen_at_start is None

    async def test_a_key_it_does_not_name_says_which_it_does(
        self, detector: Detector, imu: dict[str, np.ndarray]
    ) -> None:
        await detector.start()

        with pytest.raises(KeyError, match="it names: model"):
            detector.artifact_path("weights")

    async def test_models_found_for_it_already_are_kept(
        self, detector: Detector, store: ArtifactStore
    ) -> None:
        # A node, or a spawn of generated code, finds them before the agent starts.
        detector._artifacts = {"model": {"name": "imu-detector", "path": "/on/the/node"}}

        await detector.start()

        assert detector.seen_at_start == "/on/the/node"

    async def test_an_actor_that_names_none_starts_as_before(self, tmp_path: Path) -> None:
        class Plain(Actor):
            async def handle_message(self, msg: Message) -> None:
                return None

        plain = Plain(name="plain", persistence_dir=str(tmp_path))
        await plain.start()
        try:
            assert plain.artifacts == {}
        finally:
            await plain.stop()


# ── A decorated function that names its models ────────────────────────────────


@wactorz.agent(name="scorer", models={"model": "imu-detector:1"})
async def score(reading: dict, me: FunctionAgent) -> dict:
    model = await me.load_model("model")
    return {"distance": float(abs(reading["az"] - model["mean"][2]))}


class TestADecoratedFunction:
    def test_the_decorator_keeps_what_it_names(self) -> None:
        spec = spec_of(score)

        assert spec is not None and spec.models == {"model": "imu-detector:1"}

    async def test_its_function_loads_the_model(
        self, imu: dict[str, np.ndarray], tmp_path: Path
    ) -> None:
        spec = spec_of(score)
        assert spec is not None
        agent = spec.build(persistence_dir=str(tmp_path / "agents"))
        await agent.start()
        try:
            assert await score({"az": 10.81}, agent) == {"distance": pytest.approx(1.0)}
            assert agent.artifact_path("model").endswith(agent.artifacts["model"]["sha256"])
        finally:
            await agent.stop()

    async def test_one_never_started_finds_its_models_on_the_first_load(
        self, imu: dict[str, np.ndarray], tmp_path: Path
    ) -> None:
        # Built in a notebook or a test and called as a function, with no system around it.
        spec = spec_of(score)
        assert spec is not None
        agent = spec.build(persistence_dir=str(tmp_path / "agents"))

        assert await score({"az": 9.81}, agent) == {"distance": pytest.approx(0.0)}
        assert agent.artifacts["model"]["version"] == 1


# ── Generated code ────────────────────────────────────────────────────────────


async def test_generated_code_loads_what_its_spawn_names(
    tmp_path: Path, store: ArtifactStore, imu: dict[str, np.ndarray]
) -> None:
    found = models.resolve_in_store({"model": "imu-detector"}, tmp_path / "state")
    agent = DynamicAgent(code="", name="gen", persistence_dir=str(tmp_path), artifacts=found)

    loaded = await agent._api.load_model("model")

    np.testing.assert_array_equal(loaded["inverse_covariance"], imu["inverse_covariance"])


def test_resolving_names_the_one_that_is_missing(store: ArtifactStore, state: Path) -> None:
    with pytest.raises(ArtifactError, match="missing"):
        models.resolve_in_store({"model": "missing"}, state)
