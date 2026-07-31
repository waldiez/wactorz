"""Root logging configuration: handlers, format, and redaction.

Called once at startup rather than run as an import side effect. The side-effect
form lived in ``_bootstrap`` on the premise that it had to execute before the
package body; it does not — ``import wactorz._bootstrap`` imports the ``wactorz``
package first, so the whole agent tree is already loaded by the time it runs.
Making the call explicit costs nothing in ordering and lets handlers be built
with redaction already attached, so no record reaches an unfiltered handler.

``_bootstrap`` keeps what genuinely must happen at import: the ``sys.path`` fixup
and the Windows event-loop and encoding fixups.
"""

import logging
import logging.handlers
import sys
from pathlib import Path

from wactorz.core.paths import resolve_state_dir

from .log_redaction import install_redaction

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# 50 MB across all files. The log used to grow without bound, which on a
# long-lived add-on or container ends as a full disk.
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

_configured = False


def _file_handler() -> logging.Handler | None:
    """A handler writing to the state directory, or ``None`` if it is unwritable.

    The log file lives beside the rest of the durable state rather than in the
    working directory: a cwd-relative path assumes the process can write wherever
    it happens to have been started, which is false for a container whose WORKDIR
    holds only read-only application files, and for any service run from a
    directory it does not own.

    Uses the shared resolver. ``_bootstrap`` had to inline this expression
    because it could not import from the package, and a set of tests existed
    purely to pin the copy to the original; as an ordinary module this has no
    such constraint, so the duplication is gone rather than guarded.

    Rotating: a rollover renames the file, which fails on Windows while another
    process holds it open — during a dev reload, say. ``emit`` routes that
    through ``handleError``, so it degrades to a complaint on stderr and a file
    that keeps growing until the next rollover succeeds, rather than an error
    that reaches the application.
    """
    try:
        log_dir = Path(resolve_state_dir())
        log_dir.mkdir(parents=True, exist_ok=True)
        return logging.handlers.RotatingFileHandler(
            log_dir / "wactorz.log",
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:  # unwritable target — console logging still works
        print(f"[logging] file logging disabled: {exc}", file=sys.stderr)
        return None


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging. Idempotent — a second call does nothing.

    Handlers are attached and filtered before any record can reach them, so the
    console and the log file never see an unredacted line.

    Built explicitly rather than through ``logging.basicConfig``, which does
    nothing at all when the root logger already carries a handler. That rule
    would make the outcome depend on whether something else configured logging
    first — an embedding application, or a test runner — and silently skipping
    redaction is the wrong way to lose an argument about ownership.
    """
    global _configured
    if _configured:
        return
    _configured = True

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_handler = _file_handler()
    if file_handler is not None:
        handlers.append(file_handler)

    root = logging.getLogger()
    formatter = logging.Formatter(_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(level)
    install_redaction()
