"""Windows platform fixups, applied at import."""

import asyncio
import io
import sys

WACTORZ_BOOTSTRAP = False


def _bootstrap() -> None:
    """Set the Windows event-loop policy and console encoding. No-op elsewhere."""
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


_bootstrap()


__all__ = ["WACTORZ_BOOTSTRAP"]
