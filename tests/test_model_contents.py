"""What a stored model holds is said by the names and shapes code reads it by.

Read from the headers alone, so listing a large model costs nothing and nothing
in it runs; a file that is not what its format says holds nothing that can be
said, and is listed as it was.
"""

import json
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from wactorz.core.artifacts import ArtifactStore, summary
from wactorz.core.model_contents import SHOWN_ARRAYS, contents


def npz(tmp_path: Path, arrays: dict[str, Any], *, compressed: bool = False) -> Path:
    path = tmp_path / "model.npz"
    if compressed:
        np.savez_compressed(path, **arrays)
    else:
        np.savez(path, **arrays)
    return path


def safetensors(tmp_path: Path, header: object) -> Path:
    data = json.dumps(header).encode()
    path = tmp_path / "model.safetensors"
    path.write_bytes(struct.pack("<Q", len(data)) + data)
    return path


class TestTheArraysAFileHolds:
    def test_an_npz_names_each_array_with_its_shape(self, tmp_path: Path) -> None:
        path = npz(tmp_path, {"mean": np.zeros(3), "inverse_covariance": np.eye(3)})

        assert contents(path, "npz") == "mean[3], inverse_covariance[3x3]"

    def test_a_compressed_npz_reads_the_same(self, tmp_path: Path) -> None:
        path = npz(tmp_path, {"w": np.zeros((2, 4))}, compressed=True)

        assert contents(path, "npz") == "w[2x4]"

    def test_a_scalar_says_so(self, tmp_path: Path) -> None:
        path = npz(tmp_path, {"threshold": np.float64(4.0)})

        assert contents(path, "npz") == "threshold (scalar)"

    def test_an_npy_is_one_array(self, tmp_path: Path) -> None:
        path = tmp_path / "a.npy"
        np.save(path, np.zeros((5, 2)))

        assert contents(path, "npy") == "array[5x2]"

    def test_safetensors_names_its_tensors_and_not_its_metadata(self, tmp_path: Path) -> None:
        path = safetensors(
            tmp_path,
            {
                "__metadata__": {"by": "train.py"},
                "weight": {"dtype": "F32", "shape": [4, 3], "data_offsets": [0, 48]},
            },
        )

        assert contents(path, "safetensors") == "weight[4x3]"

    def test_many_arrays_are_counted_past_the_first_few(self, tmp_path: Path) -> None:
        path = npz(tmp_path, {f"layer{i}": np.zeros(1) for i in range(SHOWN_ARRAYS + 2)})

        said = contents(path, "npz")

        assert said is not None and said.endswith(", and 2 more")

    @pytest.mark.parametrize("format_name", ["onnx", "raw", "pickle"])
    def test_a_format_without_named_arrays_says_nothing(
        self, tmp_path: Path, format_name: str
    ) -> None:
        path = npz(tmp_path, {"mean": np.zeros(3)})

        assert contents(path, format_name) is None

    @pytest.mark.parametrize(
        ("format_name", "data"),
        [
            ("npz", b"not a zip"),
            ("npy", b"\x93NUMPY\x01\x00\x10\x00{'shape': oops}"),
            ("npy", b"\x93NUMPY\x01\x00\x10\x00__import__('os')"),
            ("safetensors", b"\x04"),
            ("safetensors", struct.pack("<Q", 1 << 40)),
        ],
        ids=["not-a-zip", "bad-literal", "not-a-literal", "short", "huge-header"],
    )
    def test_a_file_that_is_not_its_format_says_nothing(
        self, tmp_path: Path, format_name: str, data: bytes
    ) -> None:
        path = tmp_path / "bad"
        path.write_bytes(data)

        assert contents(path, format_name) is None


class TestInTheSummary:
    def test_the_arrays_follow_the_format(self, tmp_path: Path) -> None:
        store = ArtifactStore.in_state_dir(tmp_path / "state")
        store.add(
            npz(tmp_path, {"mean": np.zeros(3), "inverse_covariance": np.eye(3)}), "imu-detector"
        )

        (line,) = summary(store)

        assert line.startswith("imu-detector — newest imu-detector:1 (npz, ")
        assert line.endswith("; arrays mean[3], inverse_covariance[3x3]")
