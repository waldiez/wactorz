"""A model, an array or raw bytes is kept beside an agent's state, not inside it.

Each is written to a file of its own when its key is persisted, and the state
file holds a marker in its place. On main that keeps a model from being
rewritten with every counter; on a node, whose state file is JSON, it is what
lets one be kept at all.
"""

import json
import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from wactorz.core import blobs
from wactorz.core.blobs import BLOB_MARK, encoder_for, file_name, is_marker
from wactorz.core.persistence.pickle_store import PickleStore, read_state_file
from wactorz.node.state import agent_state, flush_states, state_path

AGENT = "detector"


def _blob(base: Path, key: str) -> Path:
    return base / AGENT / "blobs" / file_name(key)


class TestWhatIsKeptAsABlob:
    def test_bytes_and_arrays(self) -> None:
        assert encoder_for(b"\x00\x01") is not None
        assert encoder_for(bytearray(b"x")) is not None
        found = encoder_for(np.arange(4))
        assert found is not None and found.name == "numpy" and not found.runs_code

    def test_not_an_array_of_objects(self) -> None:
        # `.npy` could only hold one pickled, so it stays in the state file.
        assert encoder_for(np.array([object()], dtype=object)) is None

    @pytest.mark.parametrize("value", [1, "text", [1, 2], {"a": 1}, {}, None, 0.5])
    def test_not_what_a_state_file_holds_anyway(self, value: Any) -> None:
        assert encoder_for(value) is None

    def test_tensors_load_without_running_code_and_a_module_does_not(self) -> None:
        torch = pytest.importorskip("torch")
        layer = torch.nn.Linear(2, 1)

        tensor = encoder_for(torch.zeros(2))
        weights = encoder_for(layer.state_dict())
        module = encoder_for(layer)

        assert tensor is not None and tensor.name == "torch-tensors" and not tensor.runs_code
        assert weights is not None and weights.name == "torch-tensors"
        assert module is not None and module.name == "torch-module" and module.runs_code

    def test_tensors_and_modules_are_told_apart_without_torch_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stands in for torch where it is not installed, as on CI, so what is
        # recognised as what is checked everywhere; the round trip through the
        # real library runs where it is.
        fake = types.ModuleType("torch")
        tensor_type = type("Tensor", (), {})
        module_type = type("Module", (), {})
        fake.Tensor = tensor_type  # pyright: ignore[reportAttributeAccessIssue]
        fake.nn = types.SimpleNamespace(Module=module_type)  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setitem(sys.modules, "torch", fake)

        def name(value: Any) -> str | None:
            found = encoder_for(value)
            return found.name if found is not None else None

        assert name(tensor_type()) == "torch-tensors"
        assert name({"layer.weight": tensor_type()}) == "torch-tensors"
        assert name({"layer.weight": tensor_type(), "step": 3}) is None
        assert name(module_type()) == "torch-module"

    def test_a_marker_naming_a_format_unknown_here_is_unreadable(self, tmp_path: Path) -> None:
        shelf = blobs.Blobs(tmp_path, blobs.DeferredWriter())

        unpacked = shelf.unpack({"model": {BLOB_MARK: "from-the-future"}})

        assert unpacked.values == {}
        assert "from-the-future" in unpacked.reasons["model"]

    def test_a_scikit_learn_model_goes_through_joblib(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "sklearn", types.ModuleType("sklearn"))
        estimator = type("Forest", (), {"__module__": "sklearn.ensemble"})()

        found = encoder_for(estimator)

        assert found is not None and found.name == "joblib" and found.runs_code

    def test_not_a_library_this_process_never_imported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delitem(sys.modules, "sklearn", raising=False)
        estimator = type("Forest", (), {"__module__": "sklearn.ensemble"})()

        assert encoder_for(estimator) is None


class TestFileNames:
    def test_a_key_is_readable_in_its_file_name(self) -> None:
        assert file_name("model").startswith("model-")
        assert file_name("model").endswith(".blob")

    def test_keys_that_differ_only_in_case_have_different_files(self) -> None:
        # One name on macOS and Windows, were the key the whole of it.
        assert file_name("Model").lower() != file_name("model").lower()

    def test_a_name_windows_reserves_is_not_the_whole_stem(self) -> None:
        assert file_name("aux").split(".")[0] != "aux"

    @pytest.mark.parametrize("key", ["../escape", "a/b", "..", ".", "a\\b"])
    def test_no_key_climbs_out_or_names_a_directory(self, key: str) -> None:
        name = file_name(key)
        assert "/" not in name and "\\" not in name and name not in {".", ".."}

    def test_a_key_too_long_for_a_file_name_is_named_by_its_hash(self) -> None:
        name = file_name("k" * 500)
        assert len(name) < 100 and name.endswith(".blob")
        assert name != file_name("k" * 499)


class TestOnMain:
    def test_the_value_comes_back_after_a_restart(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        store.update(AGENT, "weights", np.arange(6.0).reshape(2, 3))
        store.update(AGENT, "count", 3)
        store.flush()
        del store

        back = PickleStore(str(tmp_path)).load(AGENT)

        assert back["count"] == 3
        np.testing.assert_array_equal(back["weights"], np.arange(6.0).reshape(2, 3))

    def test_a_models_weights_and_the_model_come_back(self, tmp_path: Path) -> None:
        torch = pytest.importorskip("torch")
        layer = torch.nn.Linear(2, 1)
        store = PickleStore(str(tmp_path))
        store.update(AGENT, "weights", layer.state_dict())
        store.update(AGENT, "model", layer)
        store.flush()
        del store

        back = PickleStore(str(tmp_path)).load(AGENT)

        assert torch.equal(back["weights"]["weight"], layer.weight.detach())
        assert isinstance(back["model"], torch.nn.Linear)
        assert torch.equal(back["model"].bias, layer.bias)

    def test_the_state_file_holds_a_marker_and_the_blob_its_own_file(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        store.update(AGENT, "raw", b"\x89PNG")
        store.flush()

        on_disk = read_state_file(tmp_path / AGENT / "state.pkl").values
        assert on_disk == {"raw": {BLOB_MARK: "bytes"}}
        assert _blob(tmp_path, "raw").read_bytes() == b"\x89PNG"

    def test_a_counter_beside_it_does_not_write_it_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        encoded: list[int] = []
        original = blobs._array_bytes

        def counting(value: Any) -> bytes:
            encoded.append(1)
            return original(value)

        monkeypatch.setattr(
            blobs,
            "ENCODERS",
            tuple(
                blobs.Encoder(e.name, e.matches, counting, e.decode, e.runs_code)
                if e.name == "numpy"
                else e
                for e in blobs.ENCODERS
            ),
        )
        store = PickleStore(str(tmp_path))
        store.update(AGENT, "weights", np.zeros(3))
        store.flush()
        for tick in range(5):
            store.update(AGENT, "count", tick)
            store.flush()

        assert len(encoded) == 1

    def test_persisting_ordinary_values_touches_no_blob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Most persists are counters; each one must not cost a file removal.
        forgotten: list[str] = []
        monkeypatch.setattr(blobs.Blobs, "forget", lambda _self, key: forgotten.append(key))
        store = PickleStore(str(tmp_path))
        for tick in range(3):
            store.update(AGENT, "count", tick)
            store.update(AGENT, "label", f"t{tick}")
        store.flush()

        assert forgotten == []

    def test_persisting_it_again_writes_the_new_value(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        store.update(AGENT, "raw", b"one")
        store.flush()
        store.update(AGENT, "raw", b"two")
        store.flush()

        assert _blob(tmp_path, "raw").read_bytes() == b"two"

    def test_a_key_that_stops_being_a_blob_loses_its_file(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        store.update(AGENT, "raw", b"bytes")
        store.flush()
        store.update(AGENT, "raw", "now text")
        store.flush()
        del store

        assert not _blob(tmp_path, "raw").exists()
        assert PickleStore(str(tmp_path)).load(AGENT) == {"raw": "now text"}

    def test_removing_the_key_removes_its_file(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        store.update(AGENT, "raw", b"bytes")
        store.flush()
        store.remove(AGENT, "raw")
        store.flush()

        assert not _blob(tmp_path, "raw").exists()

    def test_replacing_the_state_removes_the_blobs_it_no_longer_has(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        store.save(AGENT, {"old": b"1", "kept": b"2"})
        store.flush()
        store.save(AGENT, {"kept": b"3"})
        store.flush()

        assert not _blob(tmp_path, "old").exists()
        assert _blob(tmp_path, "kept").read_bytes() == b"3"

    def test_deleting_the_agent_deletes_its_blobs(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        store.update(AGENT, "raw", b"bytes")
        store.flush()

        store.delete(AGENT)

        assert not (tmp_path / AGENT).exists()

    def test_a_blob_that_cannot_be_read_costs_its_own_key_and_is_kept(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = PickleStore(str(tmp_path))
        store.save(AGENT, {"raw": b"bytes", "count": 2})
        store.flush()
        del store
        _blob(tmp_path, "raw").write_bytes(b"\x93NUMPY broken")
        state = tmp_path / AGENT / "state.pkl"
        # Make the marker claim a format its file does not hold.
        data = state.read_bytes().replace(b"bytes", b"numpy")
        state.write_bytes(data)

        restarted = PickleStore(str(tmp_path))
        assert restarted.load(AGENT) == {"count": 2}
        assert "raw" in caplog.text

        # Written back as it was, so the blob is there to be read once it can be.
        restarted.update(AGENT, "count", 3)
        restarted.flush()
        assert is_marker(read_state_file(state).values["raw"])
        assert _blob(tmp_path, "raw").exists()

    def test_setting_a_key_that_could_not_be_read_replaces_its_blob(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path))
        store.update(AGENT, "raw", b"bytes")
        store.flush()
        del store
        _blob(tmp_path, "raw").unlink()

        restarted = PickleStore(str(tmp_path))
        assert restarted.load(AGENT) == {}
        restarted.update(AGENT, "raw", 7)
        restarted.flush()

        assert read_state_file(tmp_path / AGENT / "state.pkl").values == {"raw": 7}


class TestOnANode:
    def test_an_array_is_kept_where_json_could_not_keep_it(self, tmp_path: Path) -> None:
        state = agent_state(tmp_path, AGENT)
        values: dict[str, Any] = {"count": 1, "weights": np.ones(3)}
        state.save(values)
        flush_states()

        on_disk = json.loads(state_path(tmp_path, AGENT).read_text())
        assert on_disk == {"count": 1, "weights": {BLOB_MARK: "numpy"}}

        back = agent_state(tmp_path, AGENT).load()
        assert back["count"] == 1
        np.testing.assert_array_equal(back["weights"], np.ones(3))

    def test_only_the_key_that_changed_is_written_again(self, tmp_path: Path) -> None:
        state = agent_state(tmp_path, AGENT)
        values: dict[str, Any] = {"raw": b"one"}
        state.save(values)
        flush_states()
        values["raw"] = b"two"
        values["count"] = 1
        state.save(values, changed=("count",))
        flush_states()

        # `raw` was not said to have changed, so its blob is as it was.
        assert _blob(tmp_path, "raw").read_bytes() == b"one"

        state.save(values, changed=("raw",))
        flush_states()
        assert _blob(tmp_path, "raw").read_bytes() == b"two"

    def test_a_key_that_stops_being_a_blob_loses_its_file(self, tmp_path: Path) -> None:
        state = agent_state(tmp_path, AGENT)
        values: dict[str, Any] = {"raw": b"one"}
        state.save(values)
        flush_states()
        values["raw"] = "text"
        state.save(values, changed=("raw",))
        flush_states()

        assert not _blob(tmp_path, "raw").exists()

    def test_a_blob_that_cannot_be_read_is_kept_for_later(self, tmp_path: Path) -> None:
        state = agent_state(tmp_path, AGENT)
        state.save({"raw": b"one", "count": 1})
        flush_states()
        _blob(tmp_path, "raw").unlink()

        again = agent_state(tmp_path, AGENT)
        values = again.load()
        assert values == {"count": 1}
        values["count"] = 2
        again.save(values, changed=("count",))
        flush_states()

        on_disk = json.loads(state_path(tmp_path, AGENT).read_text())
        assert on_disk == {"count": 2, "raw": {BLOB_MARK: "bytes"}}

    def test_deleting_the_state_deletes_its_blobs(self, tmp_path: Path) -> None:
        state = agent_state(tmp_path, AGENT)
        state.save({"raw": b"one"})
        flush_states()

        assert state.delete()

        assert not (tmp_path / AGENT / "blobs").exists()
