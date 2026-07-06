"""Development auto-reload: restart the process when source files change."""

import logging
import os
import sys
import threading
import time
from pathlib import Path

_RELOAD_PATTERNS = {".py", ".json", ".yaml", ".yml"}
_RELOAD_IGNORE = {"__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
_PKG_DIR = Path(__file__).resolve().parent  # wactorz/
_RELOAD_CWD = os.getcwd()


def start_reloader(logger: logging.Logger) -> None:
    """Watch wactorz/ for source changes and restart the process via os.execv."""
    try:
        from watchdog.events import FileSystemEvent, FileSystemEventHandler
        from watchdog.observers import Observer

        class _RestartHandler(FileSystemEventHandler):
            def __init__(self) -> None:
                super().__init__()
                self._timer: threading.Timer | None = None
                self._last = 0.0

            def _should_watch(self, path: str) -> bool:
                p = Path(path)
                if not any(p.name.endswith(ext) for ext in _RELOAD_PATTERNS):
                    return False
                return not any(part in _RELOAD_IGNORE for part in p.parts)

            def _schedule(self) -> None:
                if time.time() - self._last < 2.0:
                    return
                if self._timer:
                    self._timer.cancel()
                self._timer = threading.Timer(0.5, self._restart)
                self._timer.start()

            def _restart(self) -> None:
                self._last = time.time()
                logger.info("[reload] restarting …")
                try:
                    os.chdir(_RELOAD_CWD)
                    time.sleep(0.1)
                    os.execv(sys.executable, [sys.executable, *sys.argv])  # nosec
                except Exception as exc:
                    logger.error("[reload] restart failed: %s", exc)
                    os._exit(1)  # nosec

            def on_modified(self, event: FileSystemEvent) -> None:
                if not event.is_directory and self._should_watch(str(event.src_path)):
                    logger.info("[reload] changed: %s", event.src_path)
                    self._schedule()

            def on_created(self, event: FileSystemEvent) -> None:
                if not event.is_directory and self._should_watch(str(event.src_path)):
                    self._schedule()

            def on_deleted(self, event: FileSystemEvent) -> None:
                if not event.is_directory and self._should_watch(str(event.src_path)):
                    self._schedule()

        observer = Observer()
        observer.schedule(_RestartHandler(), str(_PKG_DIR), recursive=True)
        observer.daemon = True
        observer.start()
        logger.info("[reload] watching %s", _PKG_DIR)

    except ImportError:
        logger.warning("[reload] watchdog not installed — pip install watchdog")
