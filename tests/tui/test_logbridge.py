"""Routing Python logging into the logs pane.

This one mutates global state — the root logger — so every test restores it, and
the restoration itself is what most of these assert: the TUI must hand logging
back exactly as it found it, or a later `wactorz` run in the same process loses
its console output.
"""

# pylint: disable=missing-function-docstring,protected-access

import io
import logging
import sys
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import pytest

from wactorz.tui.logbridge import LogBridge, _BufferHandler, _writes_to_terminal


@pytest.fixture(name="root")
def root_fixture() -> Iterator[logging.Logger]:
    """Give each test a pristine root logger and put the real one back."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers.clear()
    root.setLevel(logging.WARNING)  # Python's default
    yield root
    root.handlers.clear()
    root.handlers.extend(saved_handlers)
    root.setLevel(saved_level)


class _Tty(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    """A stream that is not a terminal (a file, a pipe)."""

    def isatty(self) -> bool:
        return False


# ── terminal detection ──────────────────────────────────────────────────────


def test_a_handler_with_no_stream_counts_as_a_terminal() -> None:
    # Unknown target: assume the worst, muting it is the safe default.
    assert _writes_to_terminal(logging.Handler()) is True


def test_stdout_and_stderr_handlers_are_terminals() -> None:
    assert _writes_to_terminal(logging.StreamHandler(sys.stdout)) is True
    assert _writes_to_terminal(logging.StreamHandler(sys.stderr)) is True


def test_a_tty_stream_is_detected_even_when_it_is_not_sys_stdout() -> None:
    # Textual swaps sys.stdout at startup, so a handler made earlier by cli.py
    # no longer matches it — yet it is exactly the one that corrupts the UI.
    assert _writes_to_terminal(logging.StreamHandler(_Tty())) is True


def test_a_redirected_stream_is_left_alone() -> None:
    assert _writes_to_terminal(logging.StreamHandler(_Pipe())) is False


def test_a_closed_stream_is_left_alone() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    stream.close()  # isatty() on a closed StringIO raises ValueError
    assert _writes_to_terminal(handler) is False


def test_a_stream_without_isatty_is_left_alone() -> None:
    handler = logging.Handler()
    handler.stream = object()  # type: ignore[attr-defined]
    assert _writes_to_terminal(handler) is False


# ── the buffer handler ──────────────────────────────────────────────────────


def test_records_are_buffered_with_their_level() -> None:
    buffer: deque = deque()
    handler = _BufferHandler(buffer)
    handler.emit(logging.LogRecord("x", logging.WARNING, __file__, 1, "careful", None, None))
    (levelno, line) = buffer[0]
    assert levelno == logging.WARNING
    assert line.endswith("x: careful")


def test_the_buffer_is_bounded() -> None:
    bridge = LogBridge(maxlen=3)
    for i in range(10):
        bridge._handler.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "m%d", (i,), None))
    assert len(bridge.buffer) == 3
    assert bridge.buffer[-1][1].endswith("m9")


def test_a_bad_log_call_does_not_escape_into_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    # "%d" with a string arg raises inside format(); the app must not see it.
    handled = []
    handler = _BufferHandler(deque())
    monkeypatch.setattr(handler, "handleError", handled.append)
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%d", ("not-a-number",), None)
    handler.emit(record)
    assert handled == [record]


# ── install / uninstall ─────────────────────────────────────────────────────


# pylint: disable=unused-argument
def test_installing_captures_records(
    root: logging.Logger,  # pyright: ignore[reportUnusedParameter]
) -> None:
    bridge = LogBridge()
    bridge.install()
    logging.getLogger("wactorz.test").info("hello")
    assert any("hello" in line for _lvl, line in bridge.buffer)


def test_installing_lowers_a_root_level_that_would_swallow_info(root: logging.Logger) -> None:
    # The root logger defaults to WARNING, which drops INFO before any handler.
    assert root.level == logging.WARNING
    LogBridge().install()
    assert root.level == logging.INFO


def test_a_deliberately_verbose_root_level_is_kept(root: logging.Logger) -> None:
    root.setLevel(logging.DEBUG)
    bridge = LogBridge()
    bridge.install()
    assert root.level == logging.DEBUG  # not raised back up to INFO
    bridge.uninstall()
    assert root.level == logging.DEBUG  # and not reset on the way out


def test_console_handlers_are_muted_while_installed(root: logging.Logger) -> None:
    console = logging.StreamHandler(_Tty())
    root.addHandler(console)
    LogBridge().install()
    assert console not in root.handlers


def test_file_handlers_are_left_attached(root: logging.Logger, tmp_path: Path) -> None:
    # wactorz.log must keep receiving records while the TUI is up.
    file_handler = logging.FileHandler(tmp_path / "wactorz.log")
    root.addHandler(file_handler)
    LogBridge().install()
    assert file_handler in root.handlers
    file_handler.close()


def test_redirected_stream_handlers_are_left_attached(root: logging.Logger) -> None:
    piped = logging.StreamHandler(_Pipe())
    root.addHandler(piped)
    LogBridge().install()
    assert piped in root.handlers


def test_uninstall_restores_everything(root: logging.Logger) -> None:
    console = logging.StreamHandler(_Tty())
    root.addHandler(console)
    bridge = LogBridge()

    bridge.install()
    bridge.uninstall()

    assert console in root.handlers
    assert bridge._handler not in root.handlers
    assert root.level == logging.WARNING


def test_records_stop_being_captured_after_uninstall(root: logging.Logger) -> None:
    bridge = LogBridge()
    bridge.install()
    bridge.uninstall()
    bridge.buffer.clear()
    logging.getLogger("wactorz.test").warning("after")
    assert not bridge.buffer


def test_installing_twice_is_a_no_op(root: logging.Logger) -> None:
    console = logging.StreamHandler(_Tty())
    root.addHandler(console)
    bridge = LogBridge()

    bridge.install()
    bridge.install()  # must not mute the same handler twice

    bridge.uninstall()
    assert root.handlers.count(console) == 1


def test_uninstalling_without_installing_is_safe(root: logging.Logger) -> None:
    LogBridge().uninstall()  # must not raise
    assert root.level == logging.WARNING


def test_uninstalling_twice_is_safe(root: logging.Logger) -> None:
    bridge = LogBridge()
    bridge.install()
    bridge.uninstall()
    bridge.uninstall()  # must not re-add the muted handlers a second time
    assert bridge._handler not in root.handlers


def test_a_reinstall_after_uninstall_captures_again(root: logging.Logger) -> None:
    bridge = LogBridge()
    bridge.install()
    bridge.uninstall()
    bridge.install()
    logging.getLogger("wactorz.test").info("second run")
    assert any("second run" in line for _lvl, line in bridge.buffer)
