"""Values an agent persists that are kept as files of their own.

A trained model, an array of readings, raw bytes: values like these are large,
change far less often than the counters stored beside them, and have formats
of their own. Inside the agent's state file they would be rewritten with every
counter, and on a node, where the state file is JSON, they could not be kept at
all.

So a value one of the encoders here recognises is written to a file of its own,
`<agent>/blobs/<key>.blob`, and the state file holds a small marker in its place
naming the format. Reading the state back puts the value where the marker was.
Agent code sees none of this: `persist` and `recall` take and give the value
itself. Only a value stored directly under a key is recognised; one inside a
dict or a list stays where it is.

A blob is written when its key is persisted, not when the state file is: a
model persisted once is not written again because a counter beside it changed.
The flip side is that a value changed in place is written only when it is
persisted again, which is what `persist` means everywhere.

Some formats load by running code -- joblib and a whole torch module are pickle
underneath. A blob read here is trusted whatever its format, because it is in
the agent's own directory, written by this process or one before it. Each
encoder still says whether it runs code, for the one case where that is not
so: a blob arriving from another machine.

The libraries are not imported here. A value can only be a numpy array if numpy
has been imported by whoever made it, so each encoder looks for its library in
`sys.modules` and stays out of the way when it is not there.
"""

import hashlib
import io
import logging
import shutil
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

from .deferred_write import DeferredWriter

logger = logging.getLogger(__name__)

#: The key of the marker a state file holds where a blob's value was.
BLOB_MARK = "__wactorz_blob__"

#: The ending of every blob's file name, which also keeps `.` and `..` from
#: being names in their own right.
SUFFIX = ".blob"

#: The most of a key a file name shows; a longer key is named by its hash.
_LONGEST_SHOWN = 160

#: How much of a key's hash its file name carries.
_HASH_CHARS = 12

#: The roots of the packages whose models joblib is the usual way to save.
_JOBLIB_FAMILIES = frozenset({"sklearn", "xgboost", "lightgbm", "catboost"})


@dataclass(frozen=True)
class Encoder:
    """One format a value can be kept in: how to recognise, write and read it."""

    #: The name the marker carries, which is how the value is read back.
    name: str
    matches: Callable[[Any], bool]
    encode: Callable[[Any], bytes]
    decode: Callable[[bytes], Any]
    #: Whether reading it back may run code from the file, as unpickling does.
    runs_code: bool


def _loaded(module: str) -> Any:
    """The module if something in this process has imported it, else None."""
    return sys.modules.get(module)


def _is_bytes(value: Any) -> bool:
    return isinstance(value, (bytes, bytearray))


def _is_array(value: Any) -> bool:
    np = _loaded("numpy")
    # An array of objects is a list of Python objects, which `.npy` can only
    # hold pickled; it stays in the state file with the other objects.
    return np is not None and isinstance(value, np.ndarray) and not value.dtype.hasobject


def _array_bytes(value: Any) -> bytes:
    import numpy as np  # optional dependency, present whenever an array is

    buffer = io.BytesIO()
    np.save(buffer, value, allow_pickle=False)
    return buffer.getvalue()


def _array(data: bytes) -> Any:
    import numpy as np  # optional dependency, needed only to read an array back

    return np.load(io.BytesIO(data), allow_pickle=False)


def _is_tensors(value: Any) -> bool:
    """A tensor, or a mapping of names to tensors as a model's `state_dict()` is."""
    torch = _loaded("torch")
    if torch is None:
        return False
    if isinstance(value, torch.Tensor):
        return True
    return (
        isinstance(value, dict)
        and bool(value)
        and all(isinstance(k, str) and isinstance(v, torch.Tensor) for k, v in value.items())
    )


def _is_module(value: Any) -> bool:
    torch = _loaded("torch")
    return torch is not None and isinstance(value, torch.nn.Module)


def _torch_bytes(value: Any) -> bytes:
    import torch  # pyright: ignore[reportMissingImports]  # optional, present with any tensor

    buffer = io.BytesIO()
    torch.save(value, buffer)
    return buffer.getvalue()


def _torch_load(data: bytes, *, weights_only: bool) -> Any:
    import torch  # pyright: ignore[reportMissingImports]  # optional, only to read one back

    # Onto the GPU it was saved from only where there is one; a model saved on
    # a GPU machine still loads on one without.
    where = None if torch.cuda.is_available() else "cpu"
    return torch.load(io.BytesIO(data), map_location=where, weights_only=weights_only)


def _tensors(data: bytes) -> Any:
    return _torch_load(data, weights_only=True)


def _module(data: bytes) -> Any:
    return _torch_load(data, weights_only=False)


def _is_joblib_model(value: Any) -> bool:
    root = type(value).__module__.partition(".")[0]
    return root in _JOBLIB_FAMILIES and _loaded(root) is not None


def _joblib_bytes(value: Any) -> bytes:
    import joblib  # pyright: ignore[reportMissingImports]  # optional, comes with what it saves

    buffer = io.BytesIO()
    joblib.dump(value, buffer)
    return buffer.getvalue()


def _joblib_load(data: bytes) -> Any:
    import joblib  # pyright: ignore[reportMissingImports]  # optional, only to read one back

    return joblib.load(io.BytesIO(data))


#: In the order they are tried: the first that matches a value keeps it.
ENCODERS: tuple[Encoder, ...] = (
    Encoder("bytes", _is_bytes, bytes, bytes, runs_code=False),
    Encoder("numpy", _is_array, _array_bytes, _array, runs_code=False),
    Encoder("torch-tensors", _is_tensors, _torch_bytes, _tensors, runs_code=False),
    Encoder("torch-module", _is_module, _torch_bytes, _module, runs_code=True),
    Encoder("joblib", _is_joblib_model, _joblib_bytes, _joblib_load, runs_code=True),
)

_BY_NAME = {encoder.name: encoder for encoder in ENCODERS}


def encoder_for(value: Any) -> Encoder | None:
    """The encoder that keeps ``value`` as a blob, or None to keep it in the state file."""
    for encoder in ENCODERS:
        if encoder.matches(value):
            return encoder
    return None


def encoder_named(name: str) -> Encoder | None:
    """The encoder a marker or reference names, or None if there is none by that name."""
    return _BY_NAME.get(name)


def marker(encoder: Encoder) -> dict[str, str]:
    """What the state file holds in place of a value kept by ``encoder``."""
    return {BLOB_MARK: encoder.name}


def marker_for(value: Any) -> dict[str, str] | None:
    """The marker for ``value`` if it is kept as a blob, else None."""
    encoder = encoder_for(value)
    return marker(encoder) if encoder is not None else None


def is_marker(value: Any) -> bool:
    """Whether ``value`` is a marker a state file holds in place of a blob."""
    return isinstance(value, dict) and len(value) == 1 and isinstance(value.get(BLOB_MARK), str)


def file_name(key: str) -> str:
    """The name of the file that keeps ``key``'s blob.

    The key made safe for a file name, so a person looking in the directory can
    tell which is which, then a short hash of the key itself. The hash is what
    keeps two keys apart where a file system does not: `Model` and `model` are
    one name on macOS and Windows, and Windows reserves `aux` and `con`
    whatever follows them. A key too long to show is named by its hash alone.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    shown = quote(key, safe="")
    if len(shown) > _LONGEST_SHOWN:
        return digest + SUFFIX
    return f"{shown}-{digest[:_HASH_CHARS]}{SUFFIX}"


class UnknownFormatError(ValueError):
    """A marker names a format no encoder here reads: written by a later release."""

    def __init__(self, format_name: str) -> None:
        super().__init__(f"no encoder named {format_name!r}")


class Unpacked(NamedTuple):
    """A state read back, with each blob's value where its marker was."""

    values: dict[str, Any]
    #: The marker of each blob that could not be read, kept so it is written
    #: back and the file is not lost.
    unreadable: dict[str, Any]
    #: Why each of those could not be read, for the log.
    reasons: dict[str, str]


class Blobs:
    """One agent's blobs: the directory its state file's markers point into.

    Writes go through the same `DeferredWriter` as the agent's state file, so
    they are made off the event loop, a moment after they are asked for, and
    a flush at shutdown covers both.
    """

    def __init__(self, directory: Path, writer: DeferredWriter) -> None:
        self.directory = directory
        self._writer = writer

    def path(self, key: str) -> Path:
        """Where ``key``'s blob is kept."""
        return self.directory / file_name(key)

    def stow(self, key: str, value: Any) -> bool:
        """Have ``key``'s blob hold ``value``, shortly. False if it is not kept as one.

        Encoded when it is written rather than now, so several persists of the
        same key in quick succession are one encoding, of the last value.
        """
        encoder = encoder_for(value)
        if encoder is None:
            # Nothing to remove here: most values are never blobs, and a key
            # that stops being one is the caller's to `forget`, since only the
            # caller knows what it held before.
            return False
        self._writer.submit(self.path(key), lambda: encoder.encode(value))
        return True

    def stow_all(self, values: dict[str, Any]) -> None:
        """Stow every value in ``values``, and remove the blobs of every other key."""
        for key, value in values.items():
            self.stow(key, value)
        self.keep_only(values)

    def forget(self, key: str) -> None:
        """Remove ``key``'s blob, and any write of it still on its way."""
        path = self.path(key)
        self._writer.discard(path)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[blobs] Could not remove %s: %s", path, exc)

    def keep_only(self, keys: Iterable[str]) -> None:
        """Remove the blob of every key not in ``keys``."""
        if not self.directory.is_dir():
            return
        wanted = {file_name(key) for key in keys}
        for path in self.directory.glob(f"*{SUFFIX}"):
            if path.name not in wanted:
                self._writer.discard(path)
                path.unlink(missing_ok=True)

    def unpack(self, state: dict[str, Any]) -> Unpacked:
        """``state`` with each marker replaced by its blob's value.

        A blob that cannot be read -- its file gone, or its library not
        installed here -- costs its own key and not the rest. Its marker is
        kept, so the file is still there to be read once whatever it needs is.
        """
        values: dict[str, Any] = {}
        unreadable: dict[str, Any] = {}
        reasons: dict[str, str] = {}
        for key, value in state.items():
            if not is_marker(value):
                values[key] = value
                continue
            try:
                values[key] = self.read(key, value[BLOB_MARK])
            except Exception as exc:
                unreadable[key] = value
                reasons[key] = f"{type(exc).__name__}: {exc}"
        return Unpacked(values, unreadable, reasons)

    def read(self, key: str, format_name: str) -> Any:
        """The value of ``key``'s blob, read as ``format_name``.

        Every blob read here is trusted, whatever its format: it is in this
        agent's own directory, written by this process or one before it.
        """
        encoder = encoder_named(format_name)
        if encoder is None:
            raise UnknownFormatError(format_name)
        return encoder.decode(self.path(key).read_bytes())

    def delete(self) -> None:
        """Remove every blob, and the directory that held them."""
        if not self.directory.is_dir():
            return
        for path in self.directory.glob(f"*{SUFFIX}"):
            self._writer.discard(path)
        shutil.rmtree(self.directory, ignore_errors=True)
