"""Replacing a file's contents without a window where it is neither.

Writing in place truncates first, so a crash between the truncate and the last
byte leaves a file that exists but cannot be read. Every reader here treats an
unreadable state file as an absent one, which turns an interrupted save into a
silent reset to empty — the state is not merely stale, it is gone.

Imports nothing from ``wactorz``, so modules loaded during ``core`` package
initialisation can use it at file scope.
"""

import os
import pickle
import time
from pathlib import Path
from typing import Any


def write_pickle(path: Path, obj: Any) -> None:
    """Pickle ``obj`` into ``path``, replacing it in one step.

    The temporary sits beside the target so the rename stays within one
    filesystem, which is what makes it atomic; the pid keeps two processes
    writing the same agent from colliding on it. On any failure the temporary is
    removed and the previous contents are still there.

    Raises whatever the write raised — callers decide whether a lost save is
    worth reporting. On Windows the replace itself can fail, because it refuses
    to overwrite a file another handle has open; the previous contents survive
    that, which is the whole point.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            pickle.dump(obj, f)
            # The rename is atomic, but only orders against data the filesystem
            # has actually been handed. Without this, a power loss can leave the
            # rename applied over contents that never landed.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def quarantine_unreadable(path: Path) -> Path | None:
    """Move a file that could not be read aside, returning where it went.

    The counterpart to `write_pickle`: that makes a write survive a crash, this
    makes an unreadable file survive the *recovery*. Every load path treats a
    corrupt file as absent and carries on with empty state — which is right, an
    agent should still start — but the next save then writes over the only copy
    of whatever was in there. Renaming first puts it out of that path's reach.

    The name carries a timestamp so a second bad start cannot overwrite the
    evidence from the first. Returns None when there was nothing to move, or
    when the move itself failed — a caller recovering from a bad read must not
    be stopped by a failure to preserve it.
    """
    try:
        if not path.exists():
            return None
        target = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
        # Never clobber an earlier quarantine that landed in the same second.
        suffix = 1
        while target.exists():
            target = path.with_name(f"{path.name}.corrupt.{int(time.time())}.{suffix}")
            suffix += 1
        os.replace(path, target)
        return target
    except Exception:
        return None
