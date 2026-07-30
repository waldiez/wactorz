"""Process bootstrap: import path, platform fixups, and root logging.

Importing this module prepares the interpreter to run Wactorz as an application.
The work happens at import time and is effectively idempotent: Python caches the
module, so the side effects apply exactly once regardless of how many entry
points import it.
"""

import asyncio
import io
import logging
import os
import sys
from pathlib import Path

# Make the package importable when launched directly (not via the console script).
_PKG_DIR = str(Path(__file__).parent)
if sys.path[0] != _PKG_DIR:
    sys.path.insert(0, _PKG_DIR)

WACTORZ_BOOTSTRAP = False


def _bootstrap() -> None:
    """Handle windows event loop and encoding cases, setup logging."""
    global WACTORZ_BOOTSTRAP  # pylint: disable=global-statement
    if not WACTORZ_BOOTSTRAP:
        WACTORZ_BOOTSTRAP = True
        # Windows: MUST run before any async library is imported or started.
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            # Force UTF-8 on the real Windows console only. Skip when stdio has been
            # replaced (pytest capture, test runners, etc.) since re-wrapping a
            # capture stream breaks the harness on Python 3.13.
            # pytest capture / pythonw replace stdio
            # with objects that have no .buffer.
            _need_wrap = (
                (getattr(sys.stdout, "encoding", "") or "").lower() != "utf-8"
                and hasattr(sys.stdout, "buffer")
                and hasattr(sys.stderr, "buffer")
            )
            if _need_wrap:
                sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
                sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

        # The log file lives beside the rest of the durable state rather than in
        # the working directory: a cwd-relative path assumes the process can
        # write wherever it happens to have been started, which is false for a
        # container whose WORKDIR holds only read-only application files, and for
        # any service run from a directory it does not own. Falls back to a
        # stream-only setup rather than refusing to start.
        # Resolved inline rather than through ``core.paths.resolve_state_dir``:
        # this module must stay importable before anything else in the package
        # (the sys.path fixup above exists for launches where ``wactorz`` is not
        # yet importable), and a package-internal import here would both defeat
        # that and pull the whole agent tree in at bootstrap. ``tests/
        # test_state_dir.py`` asserts the two resolutions agree, so they cannot
        # drift apart silently.
        handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
        try:
            log_dir = Path(os.environ.get("WACTORZ_STATE_DIR", "").strip() or "./state")
            log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_dir / "wactorz.log", encoding="utf-8"))
        except OSError as exc:  # unwritable target — console logging still works
            print(f"[bootstrap] file logging disabled: {exc}", file=sys.stderr)

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=handlers,
        )


_bootstrap()


__all__ = ["WACTORZ_BOOTSTRAP"]
