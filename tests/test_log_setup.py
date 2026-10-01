"""Root logging configuration, now an explicit call rather than an import.

The regression pinned here: handlers must be built with redaction already
attached. Configuring first and filtering afterwards leaves a window in which
records reach an unfiltered console and log file, and that window is startup —
where connection strings and credentials are most likely to be logged.

``_bootstrap`` must no longer configure logging; if it starts again, a process
gets two sets of handlers and every line is duplicated.
"""

import argparse
import json
import logging
import logging.handlers
import sys
from datetime import datetime, timedelta

import pytest

from wactorz.monitoring import log_setup
from wactorz.monitoring.log_redaction import SecretRedactingFilter
from wactorz.node import cli as node_cli


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

    def test_secret_in_a_traceback_never_reaches_the_file(self, tmp_path, monkeypatch) -> None:
        """The regression this pins: rewriting ``msg`` leaves the traceback alone.

        ``Formatter.format`` renders ``exc_info`` itself, so a secret inside an
        exception's *message* — an SSH auth failure, a URL error — reached the
        file and console untouched while only the in-memory buffer was clean.
        Credential-bearing text is more likely in an exception than in a plain
        message, so this is the path that mattered most.
        """
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        log_setup.setup_logging()
        file_handler = next(
            h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)
        )
        try:
            raise ValueError("auth failed for mqtt://u:s3cr3t@broker password=hunter2")
        except ValueError:
            file_handler.handle(
                logging.LogRecord(
                    name="wactorz.test.setup",
                    level=logging.ERROR,
                    pathname=__file__,
                    lineno=1,
                    msg="connect failed",
                    args=None,
                    exc_info=sys.exc_info(),
                )
            )
        file_handler.flush()
        written = (tmp_path / "wactorz.log").read_text(encoding="utf-8")
        assert "s3cr3t" not in written
        assert "hunter2" not in written
        assert "ValueError" in written, "the traceback is redacted, not discarded"
        assert "connect failed" in written

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


def _record(msg: str, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="wactorz.test.setup",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=exc_info,
    )


def _written_by_the_file_handler(tmp_path, record: logging.LogRecord) -> str:
    """What `setup_logging`'s file handler writes for `record`, filters included."""
    log_setup.setup_logging()
    handler = next(h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler))
    handler.handle(record)
    handler.flush()
    return (tmp_path / "wactorz.log").read_text(encoding="utf-8")


class TestTheFormat:
    """`WACTORZ_LOG_FORMAT=json` writes one JSON object per record.

    For a collector that parses logs. Text stays the default, and what is
    redacted in text is redacted in JSON: the filter runs before either
    formatter sees the record.
    """

    def test_text_unless_asked_otherwise(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("WACTORZ_LOG_FORMAT", raising=False)

        written = _written_by_the_file_handler(tmp_path, _record("plain words"))

        assert "[ERROR] wactorz.test.setup: plain words" in written

    def test_json_is_one_object_per_line_with_fields_to_filter_on(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("WACTORZ_LOG_FORMAT", "json")

        written = _written_by_the_file_handler(tmp_path, _record("two\nlines, and ünïcode"))

        (line,) = written.splitlines()
        entry = json.loads(line)
        assert entry["level"] == "ERROR"
        assert entry["logger"] == "wactorz.test.setup"
        assert entry["message"] == "two\nlines, and ünïcode"
        assert "ünïcode" in line, "written as it is, not as escapes"
        # A time with its offset, so a server's and a node's lines sort together.
        assert datetime.fromisoformat(entry["ts"]).utcoffset() == timedelta(0)

    def test_a_traceback_stays_inside_its_record(self, tmp_path, monkeypatch) -> None:
        # In text a traceback is a line per frame, which a collector reads as
        # that many separate events.
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("WACTORZ_LOG_FORMAT", "json")
        try:
            raise ValueError("no such thing")
        except ValueError:
            record = _record("lookup failed", sys.exc_info())

        written = _written_by_the_file_handler(tmp_path, record)

        (line,) = written.splitlines()
        entry = json.loads(line)
        assert entry["message"] == "lookup failed"
        assert entry["exception"].startswith("Traceback")
        assert "ValueError: no such thing" in entry["exception"]

    def test_json_is_redacted_like_text(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("WACTORZ_LOG_FORMAT", "json")
        try:
            raise ValueError("auth failed for mqtt://u:s3cr3t@broker password=hunter2")
        except ValueError:
            record = _record("broker mqtt://u:s3cr3t@host", sys.exc_info())

        written = _written_by_the_file_handler(tmp_path, record)

        assert "s3cr3t" not in written
        assert "hunter2" not in written
        entry = json.loads(written)
        assert "mqtt://u:" in entry["message"], "only the credential is removed"
        assert "ValueError" in entry["exception"], "the traceback is redacted, not discarded"

    @pytest.mark.parametrize("value", ["JSON", " json ", '"json"'])
    def test_the_setting_is_read_forgivingly(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("WACTORZ_LOG_FORMAT", value)

        assert isinstance(log_setup.formatter(), log_setup.JsonFormatter)

    def test_a_value_that_is_not_a_format_is_named_and_text_is_used(self, monkeypatch) -> None:
        # A collector expecting JSON would otherwise get text with nothing saying why.
        monkeypatch.setenv("WACTORZ_LOG_FORMAT", "yaml")

        with pytest.warns(RuntimeWarning, match="WACTORZ_LOG_FORMAT='yaml'"):
            chosen = log_setup.formatter()

        assert not isinstance(chosen, log_setup.JsonFormatter)

    def test_a_node_logs_to_its_console_in_the_same_format(self, monkeypatch) -> None:
        # What it asks of `basicConfig` is what is checked: the test runner keeps
        # handlers of its own on the root logger, and `basicConfig` leaves a
        # root logger that has any alone.
        monkeypatch.setenv("WACTORZ_LOG_FORMAT", "json")
        asked: dict = {}
        monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: asked.update(kwargs))

        node_cli.configure_logging(argparse.Namespace(loglevel="warning"))

        (handler,) = asked["handlers"]
        assert isinstance(handler, logging.StreamHandler)
        assert isinstance(handler.formatter, log_setup.JsonFormatter)
        assert asked["level"] == logging.WARNING
