"""What a stored model holds, read from its headers alone.

Code written for a model has to read it by the names its arrays were saved
under. Whoever writes that code -- a person, or the model main and the planner
ask -- sees only the artifact's name and format otherwise, and guesses: ``std``
and ``threshold`` for a model that holds ``mean`` and ``inverse_covariance``.

So for the formats that are named arrays (npz, npy, safetensors) this reads the
names and shapes, from the headers and nothing else: no array is loaded, numpy
is not needed, and nothing in the file is run. ``ast.literal_eval`` reads an npy
header, which is a Python literal; it evaluates literals only. Any other format,
and any file that does not read as its format says, holds nothing that can be
said here.
"""

import ast
import functools
import json
import struct
import zipfile
from pathlib import Path
from typing import IO, Any

#: How many arrays are named before the rest are counted.
SHOWN_ARRAYS = 8

_NPY_MAGIC = b"\x93NUMPY"
#: Far above the headers numpy writes, so a longer one is not an npy header.
_NPY_HEADER_LIMIT = 1 << 16
#: The safetensors format's own limit on its header.
_SAFETENSORS_HEADER_LIMIT = 100 << 20


class _Unreadable(Exception):
    """The file is not what its format says."""

    @classmethod
    def not_npy(cls) -> "_Unreadable":
        return cls("not an npy file")

    @classmethod
    def cut_short(cls, fmt: str) -> "_Unreadable":
        return cls(f"{fmt} header cut short")

    @classmethod
    def too_long(cls, fmt: str) -> "_Unreadable":
        return cls(f"{fmt} header too long")

    @classmethod
    def not_a_literal(cls) -> "_Unreadable":
        return cls("npy header is not a literal")

    @classmethod
    def not_json(cls) -> "_Unreadable":
        return cls("safetensors header is not JSON")

    @classmethod
    def not_an_object(cls) -> "_Unreadable":
        return cls("safetensors header is not an object")

    @classmethod
    def no_shape(cls) -> "_Unreadable":
        return cls("no shape")


def contents(path: Path, format_name: str) -> str | None:
    """The arrays in the file at ``path``, as ``mean[3], inverse_covariance[3x3]``.

    None for a format that is not named arrays, and for a file that cannot be read
    as its format.
    """
    try:
        arrays = _read_arrays(path, format_name)
    except (OSError, _Unreadable, zipfile.BadZipFile):
        return None
    if arrays is None:
        return None
    return _described(arrays)


@functools.lru_cache(maxsize=256)
def stored_contents(sha256: str, path: Path, format_name: str) -> str | None:
    """`contents`, kept by hash: a stored file never changes, so it is read once."""
    del sha256  # the key: what is stored under a hash is always the same
    return contents(path, format_name)


def _read_arrays(path: Path, format_name: str) -> list[tuple[str, tuple[int, ...]]] | None:
    if format_name == "npy":
        with path.open("rb") as file:
            return [("array", _npy_shape(file))]
    if format_name == "npz":
        return _npz_arrays(path)
    if format_name == "safetensors":
        return _safetensors_arrays(path)
    return None


def _npz_arrays(path: Path) -> list[tuple[str, tuple[int, ...]]]:
    arrays: list[tuple[str, tuple[int, ...]]] = []
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            name = member.removesuffix(".npy")
            with archive.open(member) as file:
                arrays.append((name, _npy_shape(file)))
    return arrays


def _npy_shape(file: IO[bytes]) -> tuple[int, ...]:
    """The shape in the header of the npy file ``file`` is at the start of."""
    if file.read(len(_NPY_MAGIC)) != _NPY_MAGIC:
        raise _Unreadable.not_npy()
    major = file.read(2)[:1]
    # Version 1 gives the header's length in two bytes, later versions in four.
    size = 2 if major == b"\x01" else 4
    length_bytes = file.read(size)
    if len(length_bytes) != size:
        raise _Unreadable.cut_short("npy")
    (length,) = struct.unpack("<H" if size == 2 else "<I", length_bytes)
    if length > _NPY_HEADER_LIMIT:
        raise _Unreadable.too_long("npy")
    try:
        header: Any = ast.literal_eval(file.read(length).decode("latin-1"))
    except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
        raise _Unreadable.not_a_literal() from exc
    shape = header.get("shape") if isinstance(header, dict) else None
    return _checked_shape(shape)


def _safetensors_arrays(path: Path) -> list[tuple[str, tuple[int, ...]]]:
    with path.open("rb") as file:
        length_bytes = file.read(8)
        if len(length_bytes) != 8:
            raise _Unreadable.cut_short("safetensors")
        (length,) = struct.unpack("<Q", length_bytes)
        if length > _SAFETENSORS_HEADER_LIMIT:
            raise _Unreadable.too_long("safetensors")
        try:
            header: Any = json.loads(file.read(length))
        except ValueError as exc:
            raise _Unreadable.not_json() from exc
    if not isinstance(header, dict):
        raise _Unreadable.not_an_object()
    return [
        (name, _checked_shape(entry.get("shape") if isinstance(entry, dict) else None))
        for name, entry in header.items()
        if name != "__metadata__"
    ]


def _checked_shape(shape: Any) -> tuple[int, ...]:
    if not isinstance(shape, (tuple, list)) or not all(
        isinstance(dim, int) and dim >= 0 for dim in shape
    ):
        raise _Unreadable.no_shape()
    return tuple(shape)


def _described(arrays: list[tuple[str, tuple[int, ...]]]) -> str:
    shown = [
        f"{name}[{'x'.join(str(dim) for dim in shape)}]" if shape else f"{name} (scalar)"
        for name, shape in arrays[:SHOWN_ARRAYS]
    ]
    if len(arrays) > SHOWN_ARRAYS:
        shown.append(f"and {len(arrays) - SHOWN_ARRAYS} more")
    return ", ".join(shown) or "no arrays"
