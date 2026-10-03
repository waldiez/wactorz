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

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from wactorz import config
from wactorz.core.paths import resolve_state_dir

from .log_redaction import install_redaction

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """One JSON object per record, on one line: JSON Lines.

    For a collector that parses logs rather than a person who reads them: the
    level and the logger are fields to filter on instead of text to match, and
    a traceback stays inside the record it belongs to, where the text format
    spreads it over lines a collector reads as separate events.

    The time is UTC with an offset, so lines from a server and its nodes sort
    together whatever zone each machine is in.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # As `logging.Formatter` does: the traceback is rendered once and kept
        # on the record, and a text already there -- the redacted one -- is used.
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exception"] = record.exc_text
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def formatter() -> logging.Formatter:
    """The formatter ``WACTORZ_LOG_FORMAT`` asks for: text unless it says ``json``."""
    if config.log_format() == "json":
        return JsonFormatter()
    return logging.Formatter(_FORMAT)


def setup_console_logging(level: int = logging.INFO) -> None:
    """Log to the console alone, in the configured format.

    For a process that keeps no log file of its own and has no dashboard to
    feed: a node, whose output goes to its service's journal or to the file its
    launcher redirects it into.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(formatter())
    logging.basicConfig(level=level, handlers=[handler])
    # A node is handed a broker password and a signing key, and runs agent
    # code that logs what it likes: its console is read like the server's.
    install_redaction()


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
    chosen = formatter()
    for handler in handlers:
        handler.setFormatter(chosen)
        root.addHandler(handler)
    root.setLevel(level)
    install_redaction()
