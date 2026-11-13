# SPDX-License-Identifier: Apache-2.0
"""A torch checkpoint is read without running code from it, numeric numpy arrays included.

torch is optional and CI does not install it. The rules -- what is admitted,
what a refusal says, what happens without torch -- are checked against a
stand-in for torch everywhere; the same rules against the real library run
where it is installed.
"""

import contextlib
import io
import logging
import pickle
import sys
import types
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest

from wactorz.core import models, torch_checkpoints
from wactorz.core.artifacts import ArtifactError, ArtifactStore
from wactorz.core.torch_checkpoints import CheckpointRefusedError

#: torch's refusal of a class it would have to build, as torch words it.
REFUSAL = (
    "Weights only load failed. In PyTorch 2.6, we changed the default value of the "
    "`weights_only` argument.\nPlease file an issue with the following so that we can make "
    "`weights_only=True` compatible with your use case: WeightsUnpickler error: \n\n"
    "Unsupported global: GLOBAL builtins.eval was not an allowed global by default. Please use "
    "`torch.serialization.add_safe_globals([eval])` to allowlist this global.\n\n"
    "Check the documentation of torch.load to learn more."
)


def entry(path: Path) -> dict[str, Any]:
    return {"name": "detector", "version": 2, "format": "state_dict", "path": str(path)}


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore.in_state_dir(tmp_path / "state")


# ── What is admitted ──────────────────────────────────────────────────────────


class TestAdmitted:
    def test_plain_numeric_arrays_and_scalars_are_admitted_and_nothing_of_objects(self) -> None:
        admitted = torch_checkpoints.numpy_globals()
        plain = [g for g in admitted if not isinstance(g, tuple)]

        assert np.ndarray in plain and np.dtype in plain
        assert np.dtypes.Float32DType in plain and np.dtypes.Complex64DType in plain
        for refused in ("ObjectDType", "StrDType", "BytesDType", "VoidDType", "DateTime64DType"):
            assert getattr(np.dtypes, refused) not in plain

    def test_numpy_1s_name_for_the_array_constructor_is_the_same_function(self) -> None:
        admitted = torch_checkpoints.numpy_globals()
        aliases = {name: obj for obj, name in (g for g in admitted if isinstance(g, tuple))}

        assert aliases["numpy.core.multiarray._reconstruct"] in admitted
        assert set(aliases) == {
            "numpy.core.multiarray._reconstruct",
            "numpy.core.multiarray.scalar",
        }

    def test_without_numpy_nothing_more_than_torch_admits_is_admitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "numpy", None)

        assert torch_checkpoints.numpy_globals() == []

    def test_a_numpy_that_keeps_its_constructor_elsewhere_admits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = types.ModuleType("numpy")
        monkeypatch.setitem(sys.modules, "numpy", fake)

        assert torch_checkpoints.numpy_globals() == []


# ── Against a stand-in for torch ──────────────────────────────────────────────


class FakeTorch(types.ModuleType):
    """Records how ``load`` was called and answers what a test sets, as the torch of ``version`` would.

    ``safe_globals`` came in torch 2.5 and ``load``'s ``mmap`` in 2.1; before
    that, ``load`` hands a keyword it does not know to its unpickler, which
    fails on it.
    """

    calls: ClassVar[list[dict[str, Any]]] = []
    admitted: ClassVar[list[list[Any]]] = []

    def __init__(self, answer: Any = None, *, version: str = "2.14.1") -> None:
        super().__init__("torch")
        self.answer = answer
        self.__version__ = version
        FakeTorch.calls, FakeTorch.admitted = [], []
        release = tuple(int(part) for part in version.split(".")[:2])
        serialization = types.SimpleNamespace()
        if release >= (2, 5):
            serialization.safe_globals = self._safe_globals
        self.serialization = serialization
        self.load = self._load if release >= (2, 1) else self._load_before_mmap

    @staticmethod
    @contextlib.contextmanager
    def _safe_globals(allowed: list[Any]) -> Iterator[None]:
        FakeTorch.admitted.append(allowed)
        yield

    def _load(
        self,
        path: Any,
        map_location: Any = None,
        *,
        weights_only: bool = False,
        mmap: bool | None = None,
    ) -> Any:
        given = {"map_location": map_location, "weights_only": weights_only}
        return self._answer(given if mmap is None else {**given, "mmap": mmap})

    def _load_before_mmap(
        self,
        path: Any,
        map_location: Any = None,
        *,
        weights_only: bool = False,
        **pickle_load_args: Any,
    ) -> Any:
        if pickle_load_args:
            raise TypeError(f"unexpected keyword arguments {sorted(pickle_load_args)}")
        return self._answer({"map_location": map_location, "weights_only": weights_only})

    def _answer(self, kwargs: dict[str, Any]) -> Any:
        FakeTorch.calls.append(kwargs)
        answer = self.answer(kwargs) if callable(self.answer) else self.answer
        if isinstance(answer, BaseException):
            raise answer
        return answer


def fake_torch(monkeypatch: pytest.MonkeyPatch, answer: Any, **options: Any) -> FakeTorch:
    fake = FakeTorch(answer, **options)
    monkeypatch.setitem(sys.modules, "torch", fake)
    return fake


class TestLoad:
    def test_it_loads_safely_on_the_cpu_admitting_numeric_arrays(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_torch(monkeypatch, {"w": 1})

        assert torch_checkpoints.load(tmp_path / "m.pt") == {"w": 1}
        assert FakeTorch.calls == [{"map_location": "cpu", "weights_only": True}]
        assert FakeTorch.admitted == [torch_checkpoints.numpy_globals()]

    def test_a_torch_without_safe_globals_loads_with_its_own_rules(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_torch(monkeypatch, {"w": 1}, version="2.4.1")

        assert torch_checkpoints.load(tmp_path / "m.pt") == {"w": 1}
        assert FakeTorch.admitted == []

    def test_a_refusal_by_an_older_torch_says_which_torch_reads_arrays(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_torch(monkeypatch, pickle.UnpicklingError(REFUSAL), version="2.4.1")

        with pytest.raises(CheckpointRefusedError) as caught:
            torch_checkpoints.load(tmp_path / "m.pt")

        assert caught.value.reason.startswith("Unsupported global: GLOBAL builtins.eval")
        assert caught.value.reason.endswith("read by torch 2.5 or later, and this is torch 2.4.1")

    def test_a_refusal_says_what_was_refused_without_torchs_advice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_torch(monkeypatch, pickle.UnpicklingError(REFUSAL))

        with pytest.raises(CheckpointRefusedError) as caught:
            torch_checkpoints.load(tmp_path / "m.pt")

        assert caught.value.reason == (
            "Unsupported global: GLOBAL builtins.eval was not an allowed global by default"
        )
        assert "add_safe_globals" not in str(caught.value)

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("invalid load key, 'x'.", "invalid load key, 'x'"),
            ("WeightsUnpickler error: ", "WeightsUnpickler error: "),
        ],
    )
    def test_a_refusal_in_other_words_is_given_as_it_came(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str, reason: str
    ) -> None:
        fake_torch(monkeypatch, pickle.UnpicklingError(text))

        with pytest.raises(CheckpointRefusedError) as caught:
            torch_checkpoints.load(tmp_path / "m.pt")

        assert caught.value.reason == reason


class TestCheck:
    def test_without_torch_nothing_is_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setitem(sys.modules, "torch", None)

        assert torch_checkpoints.check(tmp_path / "m.pt") is False

    def test_it_maps_the_file_rather_than_reading_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_torch(monkeypatch, {"w": 1})

        assert torch_checkpoints.check(tmp_path / "m.pt") is True
        assert [c.get("mmap") for c in FakeTorch.calls] == [True]

    @pytest.mark.parametrize("version", ["2.0.1", "2.4.1"])
    def test_an_older_torch_checks_a_tensor_only_checkpoint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str
    ) -> None:
        fake_torch(monkeypatch, {"w": 1}, version=version)

        assert torch_checkpoints.check(tmp_path / "m.pt") is True
        mapped = [c.get("mmap") for c in FakeTorch.calls]
        assert mapped == ([None] if version == "2.0.1" else [True])

    def test_a_load_whose_signature_cannot_be_read_is_not_asked_to_map(self) -> None:
        assert torch_checkpoints._takes_mmap(object()) is False  # pyright: ignore[reportPrivateUsage]

    def test_a_file_that_cannot_be_mapped_is_read_whole(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def answer(kwargs: dict[str, Any]) -> Any:
            return (
                RuntimeError("mmap can only be used with the zip format")
                if kwargs.get("mmap")
                else {}
            )

        fake_torch(monkeypatch, answer)

        assert torch_checkpoints.check(tmp_path / "m.pt") is True
        assert [c.get("mmap") for c in FakeTorch.calls] == [True, None]


class TestOnTheWayIn:
    def test_a_refused_checkpoint_is_not_stored(
        self, monkeypatch: pytest.MonkeyPatch, store: ArtifactStore, tmp_path: Path
    ) -> None:
        path = tmp_path / "m.pt"
        path.write_bytes(b"checkpoint")
        fake_torch(monkeypatch, pickle.UnpicklingError(REFUSAL))

        with pytest.raises(
            ArtifactError, match=r"not a state_dict agents can load .*builtins\.eval"
        ):
            store.add(path, "detector", format_name="state_dict")
        assert store.names() == []

    def test_a_file_torch_cannot_read_is_not_stored(
        self, monkeypatch: pytest.MonkeyPatch, store: ArtifactStore, tmp_path: Path
    ) -> None:
        path = tmp_path / "m.pt"
        path.write_bytes(b"checkpoint")
        fake_torch(monkeypatch, RuntimeError("not a ZIP archive"))

        with pytest.raises(ArtifactError, match="is not a torch checkpoint: not a ZIP archive"):
            store.add(path, "detector", format_name="state_dict")

    def test_without_torch_it_is_stored_with_a_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        store: ArtifactStore,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = tmp_path / "m.pt"
        path.write_bytes(b"checkpoint")
        monkeypatch.setitem(sys.modules, "torch", None)

        with caplog.at_level(logging.WARNING, logger="wactorz.core.artifacts"):
            stored = store.add(path, "detector", format_name="state_dict")

        assert stored.format == "state_dict"
        assert "stored unread: torch is not installed here" in caplog.text

    def test_other_formats_are_not_read(
        self, monkeypatch: pytest.MonkeyPatch, store: ArtifactStore, tmp_path: Path
    ) -> None:
        path = tmp_path / "m.npy"
        np.save(path, np.zeros(2))
        fake_torch(monkeypatch, AssertionError("read"))

        store.add(path, "detector")

        assert FakeTorch.calls == []


class TestInAnAgent:
    def test_a_refusal_names_the_model_and_what_to_do(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake_torch(monkeypatch, pickle.UnpicklingError(REFUSAL))

        with pytest.raises(
            ArtifactError,
            match=r"detector:2 cannot be read without running code \(Unsupported global",
        ):
            models.load(entry(tmp_path / "m.pt"))


# ── Against the real torch ────────────────────────────────────────────────────


def saved(path: Path, value: Any) -> Path:
    torch = pytest.importorskip("torch")
    torch.save(value, path)
    return path


def as_numpy_1_wrote_it(path: Path) -> Path:
    """The checkpoint at ``path`` with numpy's module named as numpy 1 named it."""
    source = io.BytesIO(path.read_bytes())
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(path, "w") as new:
        for item in old.infolist():
            data = old.read(item.filename)
            if item.filename.endswith("data.pkl"):
                data = data.replace(b"numpy._core.multiarray", b"numpy.core.multiarray")
            new.writestr(item, data)
    return path


class TestRealTorch:
    def test_a_checkpoint_with_arrays_beside_the_weights_loads(self, tmp_path: Path) -> None:
        torch = pytest.importorskip("torch")
        path = saved(
            tmp_path / "m.pt",
            {
                "state": {"w": torch.ones(2)},
                "mu": np.full((1, 6, 1), 0.5, np.float32),
                "classes": np.arange(3),
                "complex": np.ones(2, np.complex64),
                "mask": np.array([True, False]),
                "loss": np.float64(0.25),
            },
        )

        loaded = models.load(entry(path))

        assert torch.equal(loaded["state"]["w"], torch.ones(2))
        np.testing.assert_array_equal(loaded["mu"], np.full((1, 6, 1), 0.5, np.float32))
        np.testing.assert_array_equal(loaded["classes"], np.arange(3))
        assert loaded["complex"].dtype == np.complex64
        assert loaded["loss"] == 0.25

    def test_a_checkpoint_numpy_1_wrote_loads(self, tmp_path: Path) -> None:
        path = as_numpy_1_wrote_it(
            saved(tmp_path / "m.pt", {"mu": np.ones(3, np.float32), "loss": np.float64(1.0)})
        )

        loaded = models.load(entry(path))

        np.testing.assert_array_equal(loaded["mu"], np.ones(3, np.float32))

    @pytest.mark.parametrize(
        ("value", "named"),
        [
            (np.array([1, "a"], dtype=object), "ObjectDType"),
            (np.array(["a"]), "StrDType"),
            (np.array(["2026-01-01"], dtype="datetime64[D]"), "DateTime64DType"),
        ],
    )
    def test_arrays_that_are_not_numbers_stay_refused(
        self, tmp_path: Path, value: Any, named: str
    ) -> None:
        path = saved(tmp_path / "m.pt", {"x": value})

        with pytest.raises(ArtifactError, match=named):
            models.load(entry(path))

    def test_a_good_checkpoint_is_stored_and_a_bad_one_is_not(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        good = saved(tmp_path / "good.pt", {"w": np.ones(2, np.float32)})
        bad = saved(tmp_path / "bad.pt", {"x": np.array([1, "a"], dtype=object)})
        garbage = tmp_path / "garbage.pt"
        garbage.write_bytes(b"x")

        assert store.add(good, "detector", format_name="state_dict").version == 1
        with pytest.raises(ArtifactError, match="ObjectDType"):
            store.add(bad, "detector", format_name="state_dict")
        with pytest.raises(ArtifactError, match=r"garbage\.pt"):
            store.add(garbage, "detector", format_name="state_dict")

    def test_a_checkpoint_in_the_older_format_is_read_whole(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        torch = pytest.importorskip("torch")
        path = tmp_path / "old.pt"
        torch.save({"w": torch.ones(2)}, path, _use_new_zipfile_serialization=False)

        assert store.add(path, "detector", format_name="state_dict").version == 1
