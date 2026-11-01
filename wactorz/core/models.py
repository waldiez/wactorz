"""The models an agent names: finding them on main, and loading them.

An agent names artifacts -- models kept with ``wactorz-artifacts`` -- under keys
of its own: a spawn config's ``artifacts``, ``models=`` on ``@wactorz.agent``,
``MODELS`` on an ``Actor``. Where it runs, each key resolves to a path: on main
from the store, on a node from what the node fetched. ``artifact_path`` gives
that path to whatever library reads the format; ``load_model`` reads the
formats that need nothing but a well-known library, and reads them the way that
runs no code from the file:

- ``npz`` and ``npy`` with numpy, ``allow_pickle=False``: a dict of arrays, or one;
- ``safetensors`` with ``safetensors.numpy``: a dict of arrays, no torch needed;
- ``state_dict`` with ``torch.load(weights_only=True)``, on the CPU;
- ``onnx`` as an ``onnxruntime.InferenceSession``;
- ``raw`` as the bytes themselves.

A pickle, a joblib file and a whole torch module run code when they load. They
are loaded only when the caller says ``allow_code=True``, which an agent should
say only of a model it would run the maker's code for. Every other format --
GGUF, TFLite, skops, a directory -- is opened through its path by the library
that reads it.

Loading reads a file that may be large: the callers do it off the event loop.
"""

import pickle
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, ArtifactStore, pinned_in


class ModelError(ArtifactError):
    """A model an agent names that cannot be loaded here, and why."""

    @classmethod
    def runs_code(cls, entry: Mapping[str, Any], fmt: str) -> "ModelError":
        return cls(
            f"{entry.get('name')}:{entry.get('version')} is {fmt}, which runs code when it "
            "loads: load it with allow_code=True only if you would run its maker's code"
        )

    @classmethod
    def no_loader(cls, fmt: str) -> "ModelError":
        return cls(
            f"there is no loader for {fmt}: open artifact_path() with the library that reads it"
        )

    @classmethod
    def needs(cls, library: str) -> "ModelError":
        return cls(f"loading it needs {library}: pip install {library}")

    @classmethod
    def cannot_start(cls, agent: str, exc: ArtifactError) -> "ModelError":
        return cls(f"{agent} cannot start without its models: {exc}")


def resolve_in_store(wanted: Mapping[str, Any], state_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Each artifact ``wanted`` names, pinned in main's store, with the path it is read at.

    Raises `ArtifactError` saying which one is not stored, or will not unpack.
    """
    store = ArtifactStore.in_state_dir(state_dir)
    pinned = store.pin_all(wanted)
    return {
        key: {**entry, "path": str(store.readable_path(entry))}
        for key, entry in pinned_in({"artifacts": pinned}).items()
    }


def resolve_shadow(entry: Mapping[str, Any], state_dir: str | Path) -> dict[str, Any] | None:
    """The version on trial beside the model ``entry`` names, with its path here; None when none is.

    For an entry that follows ``active``: where ``NAME:shadow`` stands, pinned
    as a spawn would pin it and unpacked once, so a serving agent can run it
    beside what it serves. None for an entry that named a version or follows
    ``shadow`` itself, and while the pointer is unset. Raises `ArtifactError`
    when the pointer names bytes the store no longer holds.
    """
    if entry.get("follows") != "active":
        return None
    store = ArtifactStore.in_state_dir(state_dir)
    name = str(entry["name"])
    if store.pointer(name, "shadow") is None:
        return None
    pinned = store.pin(f"{name}:shadow")
    return {**pinned, "path": str(store.readable_path(pinned))}


def load(entry: Mapping[str, Any], *, allow_code: bool = False) -> Any:
    """The model the resolved ``entry`` holds, read by the loader for its format.

    Raises `ArtifactError` for a format with no loader here, for one that runs
    code without ``allow_code``, and for a loader whose library is not installed.
    """
    fmt = str(entry.get("format", ""))
    path = Path(str(entry["path"]))
    if fmt in _RUNS_CODE:
        if not allow_code:
            raise ModelError.runs_code(entry, fmt)
        return _RUNS_CODE[fmt](path)
    loader = _LOADERS.get(fmt)
    if loader is None:
        raise ModelError.no_loader(fmt)
    return loader(path)


def _numpy() -> Any:
    try:
        import numpy  # optional: `pip install 'wactorz[ml]'`
    except ImportError as exc:
        raise ModelError.needs("numpy") from exc
    return numpy


def _npz(path: Path) -> dict[str, Any]:
    with _numpy().load(path, allow_pickle=False) as arrays:
        return {name: arrays[name] for name in arrays.files}


def _npy(path: Path) -> Any:
    return _numpy().load(path, allow_pickle=False)


def _safetensors(path: Path) -> dict[str, Any]:
    try:
        from safetensors.numpy import (  # pyright: ignore[reportMissingImports]  # optional: `pip install safetensors`
            load_file,
        )
    except ImportError as exc:
        raise ModelError.needs("safetensors") from exc
    return load_file(str(path))


def _torch() -> Any:
    try:
        import torch  # pyright: ignore[reportMissingImports]  # optional: `pip install 'wactorz[ml]'`
    except ImportError as exc:
        raise ModelError.needs("torch") from exc
    return torch


def _state_dict(path: Path) -> Any:
    return _torch().load(path, map_location="cpu", weights_only=True)


def _onnx(path: Path) -> Any:
    try:
        import onnxruntime  # pyright: ignore[reportMissingImports]  # optional: `pip install onnxruntime`
    except ImportError as exc:
        raise ModelError.needs("onnxruntime") from exc
    return onnxruntime.InferenceSession(str(path))


def _raw(path: Path) -> bytes:
    return path.read_bytes()


def _pickle(path: Path) -> Any:
    return pickle.loads(path.read_bytes())  # noqa: S301  # only with allow_code, see `load`


def _joblib(path: Path) -> Any:
    try:
        import joblib  # pyright: ignore[reportMissingImports]  # optional: `pip install joblib`
    except ImportError as exc:
        raise ModelError.needs("joblib") from exc
    return joblib.load(path)


def _torch_module(path: Path) -> Any:
    return _torch().load(path, map_location="cpu", weights_only=False)


_LOADERS: dict[str, Callable[[Path], Any]] = {
    "npz": _npz,
    "npy": _npy,
    "safetensors": _safetensors,
    "state_dict": _state_dict,
    "onnx": _onnx,
    "raw": _raw,
}

_RUNS_CODE: dict[str, Callable[[Path], Any]] = {
    "pickle": _pickle,
    "joblib": _joblib,
    "torch": _torch_module,
}
