"""Root logging configuration, now an explicit call rather than an import.

The regression pinned here: handlers must be built with redaction already
attached. Configuring first and filtering afterwards leaves a window in which
records reach an unfiltered console and log file, and that window is startup —
where connection strings and credentials are most likely to be logged.

``_bootstrap`` must no longer configure logging; if it starts again, a process
gets two sets of handlers and every line is duplicated.
"""

import logging
import logging.handlers

import pytest

from wactorz.monitoring import log_setup
from wactorz.monitoring.log_redaction import SecretRedactingFilter


@pytest.fixture(autouse=True)
def _restore_root():
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    configured = log_setup._configured
    root.handlers = []
    log_setup._configured = False
    yield
    root.handlers = handlers
    root.setLevel(level)
    log_setup._configured = configured


class TestSetupLogging:
    def test_installs_handlers(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        log_setup.setup_logging()
        assert logging.getLogger().handlers

    def test_writes_into_the_state_dir(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        log_setup.setup_logging()
        assert (tmp_path / "wactorz.log").exists()

    def test_every_handler_redacts(self, tmp_path, monkeypatch) -> None:
        """Attached at construction, so nothing reaches an unfiltered handler."""
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        log_setup.setup_logging()
        for handler in logging.getLogger().handlers:
            assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters)

    def test_secret_never_reaches_the_file(self, tmp_path, monkeypatch) -> None:
        """Driven through the file handler itself.

        ``Handler.handle`` runs the filters and then emits, which is the path
        under test; going via the root logger would instead measure pytest's
        log-capture plugin, which swaps root handlers around each test phase.
        """
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        log_setup.setup_logging()
        file_handler = next(
            h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)
        )
        file_handler.handle(
            logging.LogRecord(
                name="wactorz.test.setup",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="broker mqtt://u:s3cr3t@host",
                args=None,
                exc_info=None,
            )
        )
        file_handler.flush()
        written = (tmp_path / "wactorz.log").read_text(encoding="utf-8")
        assert "s3cr3t" not in written
        assert "mqtt://u:" in written, "only the credential is removed"

    def test_idempotent(self, tmp_path, monkeypatch) -> None:
        """A second call must not double every line."""
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        log_setup.setup_logging()
        count = len(logging.getLogger().handlers)
        log_setup.setup_logging()
        assert len(logging.getLogger().handlers) == count

    def test_file_handler_rotates(self, tmp_path, monkeypatch) -> None:
        """Unbounded growth ends as a full disk on a long-lived add-on."""
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        log_setup.setup_logging()
        handler = next(
            h
            for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        assert handler.maxBytes == log_setup.MAX_BYTES
        assert handler.backupCount == log_setup.BACKUP_COUNT

    def test_rollover_failure_does_not_reach_the_caller(self, tmp_path, monkeypatch) -> None:
        """A rollover renames the file, which fails on Windows while another
        process holds it open. ``emit`` must absorb that: logging is the one
        subsystem that has to keep working while everything else is failing."""
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        log_setup.setup_logging()
        handler = next(
            h
            for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        monkeypatch.setattr(handler, "shouldRollover", lambda record: True, raising=False)
        monkeypatch.setattr(
            handler,
            "doRollover",
            lambda: (_ for _ in ()).throw(PermissionError("file in use")),
            raising=False,
        )
        monkeypatch.setattr(logging, "raiseExceptions", False)
        handler.handle(
            logging.LogRecord(
                name="wactorz.test.setup",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="still running",
                args=None,
                exc_info=None,
            )
        )  # must not raise

    def test_unwritable_state_dir_still_logs_to_console(self, tmp_path, monkeypatch) -> None:
        """A read-only target disables the file, it does not stop the process."""
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(blocker / "state"))
        log_setup.setup_logging()
        handlers = logging.getLogger().handlers
        assert handlers
        assert not any(isinstance(h, logging.FileHandler) for h in handlers)
