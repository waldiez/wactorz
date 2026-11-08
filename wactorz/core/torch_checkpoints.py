# SPDX-License-Identifier: Apache-2.0
"""Reading a torch checkpoint without running code from it.

``torch.load(weights_only=True)`` builds tensors, containers and numbers, and
refuses everything else rather than run the file's pickle. A checkpoint is
often more than a ``state_dict``: the weights beside the input normalisation, a
loss, an epoch count, and those are as often numpy arrays as tensors. torch
refuses the arrays by default, though building one runs nothing but numpy's own
array constructor, so a load here also admits plain numeric numpy arrays and
scalars, and nothing more: an array of objects, strings or dates stays refused,
as does every other class.

Files written under numpy 1 name the array constructor by its old module path,
``numpy.core.multiarray``; both spellings are admitted, mapped to the same function.

torch is optional, and numpy with it: each is imported only when a checkpoint
is read. ``wactorz[ml]`` asks for a torch that has both things used here --
``safe_globals`` (2.5) to admit the arrays, ``mmap`` (2.1) to look at a large
file without reading it -- but an agent's own requirements may bring an older
one. On that, a file is read whole, and a checkpoint holding arrays is refused
with a reason that says which torch reads it.
"""

import contextlib
import inspect
import pickle
from pathlib import Path
from typing import Any

#: numpy's dtype classes for the numbers and booleans an array may hold. The
#: aliases among them (``IntDType``, ``LongDType``, ...) name the same classes
#: on some platforms and other widths on others, so all are listed.
_NUMERIC_DTYPES = (
    "BoolDType",
    "ByteDType",
    "UByteDType",
    "ShortDType",
    "UShortDType",
    "IntDType",
    "UIntDType",
    "LongDType",
    "ULongDType",
    "LongLongDType",
    "ULongLongDType",
    "Int8DType",
    "Int16DType",
    "Int32DType",
    "Int64DType",
    "UInt8DType",
    "UInt16DType",
    "UInt32DType",
    "UInt64DType",
    "Float16DType",
    "Float32DType",
    "Float64DType",
    "LongDoubleDType",
    "Complex64DType",
    "Complex128DType",
    "CLongDoubleDType",
)

#: The marker torch's refusal puts before the reason it gives.
_REASON_MARK = "WeightsUnpickler error:"


#: The first torch whose safe load can be told to admit numpy arrays.
ADMITS_ARRAYS_FROM = "2.5"


class CheckpointRefusedError(pickle.UnpicklingError):
    """torch's safe load would not read the file, with the reason it gave."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"torch's safe load refused it: {reason}")
        self.reason = reason

    @classmethod
    def by_an_older_torch(cls, reason: str, version: str) -> "CheckpointRefusedError":
        """The error for a checkpoint refused for ``reason`` that a newer torch than this one,
        ``version``, would read.
        """
        return cls(
            f"{reason}; numpy arrays in a checkpoint are read by torch {ADMITS_ARRAYS_FROM} "
            f"or later, and this is torch {version}"
        )


def numpy_globals() -> list[Any]:
    """What a safe load may build besides tensors: plain numeric numpy arrays and scalars.

    Empty when numpy is not installed, or keeps its array constructor where
    neither known path finds it; arrays are then refused as torch refuses them.
    """
    try:
        import numpy as np  # optional: comes with `wactorz[ml]`
    except ImportError:
        return []
    multiarray = getattr(getattr(np, "_core", None), "multiarray", None)
    if multiarray is None:  # numpy 1, where `numpy.core` is the module itself
        multiarray = getattr(getattr(np, "core", None), "multiarray", None)
    reconstruct = getattr(multiarray, "_reconstruct", None)
    scalar = getattr(multiarray, "scalar", None)
    if reconstruct is None or scalar is None:
        return []
    dtypes = getattr(np, "dtypes", None)
    classes = [getattr(dtypes, name) for name in _NUMERIC_DTYPES if hasattr(dtypes, name)]
    return [
        reconstruct,
        scalar,
        (reconstruct, "numpy.core.multiarray._reconstruct"),
        (scalar, "numpy.core.multiarray.scalar"),
        np.ndarray,
        np.dtype,
        *dict.fromkeys(classes),
    ]


def load(path: Path, *, mmap: bool = False) -> Any:
    """The checkpoint at ``path``, on the CPU, read without running code from it.

    ``mmap`` maps the tensors' storage rather than reading it, for a look at a
    large file, where torch can; it needs a file in torch's zip format (every
    torch since 1.6 writes one). Raises `CheckpointRefusedError` when torch's
    safe load refuses the file, saying what it refused, and ImportError without
    torch.
    """
    import torch  # pyright: ignore[reportMissingImports]  # optional: `pip install 'wactorz[ml]'`

    allowed = getattr(torch.serialization, "safe_globals", None)
    admitting = allowed(numpy_globals()) if allowed is not None else contextlib.nullcontext()
    extra = {"mmap": True} if mmap and _takes_mmap(torch.load) else {}
    with admitting:
        try:
            return torch.load(path, map_location="cpu", weights_only=True, **extra)
        except pickle.UnpicklingError as exc:
            if allowed is None:
                version = str(getattr(torch, "__version__", "unknown"))
                raise CheckpointRefusedError.by_an_older_torch(_reason(exc), version) from exc
            raise CheckpointRefusedError(_reason(exc)) from exc


def check(path: Path) -> bool:
    """Whether the file at ``path`` was read as `load` reads it; False when torch is not installed.

    Raises `CheckpointRefusedError` for a file the safe load refuses, and
    whatever torch raises for one that is not a checkpoint at all.
    """
    try:
        load(path, mmap=True)
    except ImportError:
        return False
    except RuntimeError:
        # A file in torch's older, pre-zip format cannot be mapped: read it whole.
        load(path)
    return True


def _takes_mmap(load_function: Any) -> bool:
    """Whether this torch's ``load`` has ``mmap``; an older one hands an unknown keyword to its unpickler."""
    try:
        return "mmap" in inspect.signature(load_function).parameters
    except (TypeError, ValueError):  # a callable with no signature to read
        return False


def _reason(exc: BaseException) -> str:
    """The line of torch's refusal that says what it refused, without the advice around it."""
    text = str(exc)
    _, found, after = text.partition(_REASON_MARK)
    lines = [line.strip() for line in (after if found else text).splitlines() if line.strip()]
    if not lines:
        return text
    return lines[0].split(". Please")[0].removesuffix(".")
