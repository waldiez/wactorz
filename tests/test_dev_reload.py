"""The development auto-reloader: what it watches, and what it refuses to do.

Everything worth pinning here is a *guard*, because the thing this module does
when it acts is `os.execv` — it replaces the running process. So the tests are
mostly about the paths that must not reach that call: a directory event, a file
whose suffix is not source, anything under `__pycache__`, and a second change
arriving while a restart is already scheduled.

`_RestartHandler` is defined inside `start_reloader`, so there is nothing to
import. The tests reach it the way the observer does — by standing in for the
observer and keeping the handler it is handed. That also keeps the assertions
honest: the handler under test is the one the real code path constructs, not a
copy written here.

Nothing in this file may let a timer fire. A real `threading.Timer` reaching
`_restart` would `execv` the pytest process, so the fake below records what was
scheduled and never runs it.
"""

import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from wactorz import dev_reload


class _FakeTimer:
    """Records what would have been scheduled. Never fires.

    `_schedule` builds a real `threading.Timer(0.5, self._restart)`; letting one
    run would replace the test process. Instances register themselves on the
    class so a test can assert how many were created.
    """

    created: ClassVar[list["_FakeTimer"]] = []

    def __init__(self, interval: float, function: Callable[[], None]) -> None:
        self.interval = interval
        self.function = function
        self.started = False
        self.cancelled = False
        _FakeTimer.created.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class _FakeObserver:
    """Stands in for `watchdog.observers.Observer`, and keeps the handler."""

    instances: ClassVar[list["_FakeObserver"]] = []

    def __init__(self) -> None:
        self.scheduled: list[tuple[Any, str, bool]] = []
        self.started = False
        self.daemon = False
        _FakeObserver.instances.append(self)

    def schedule(self, handler: Any, path: str, recursive: bool = False) -> None:
        self.scheduled.append((handler, path, recursive))

    def start(self) -> None:
        self.started = True


@pytest.fixture(name="quiet_timers", autouse=True)
def quiet_timers_fixture(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """No test may leave a live timer pointing at `os.execv`."""
    _FakeTimer.created.clear()
    monkeypatch.setattr(threading, "Timer", _FakeTimer)
    yield
    _FakeTimer.created.clear()


@pytest.fixture(name="logger")
def logger_fixture() -> Generator[logging.Logger, None, None]:
    """Provide a logger without leaking its mutable state into later tests."""
    log = logging.getLogger("test.dev_reload")
    previous_handlers = list(log.handlers)
    previous_propagate = log.propagate
    log.handlers.clear()
    log.propagate = True
    try:
        yield log
    finally:
        log.handlers[:] = previous_handlers
        log.propagate = previous_propagate


@pytest.fixture(name="handler")
def handler_fixture(monkeypatch: pytest.MonkeyPatch, logger: logging.Logger) -> Any:
    """The real `_RestartHandler`, taken from the observer it is scheduled on."""
    import watchdog.observers

    _FakeObserver.instances.clear()
    monkeypatch.setattr(watchdog.observers, "Observer", _FakeObserver)
    dev_reload.start_reloader(logger)
    return _FakeObserver.instances[0].scheduled[0][0]


class _Event:
    """The two attributes the handler reads off a watchdog event."""

    def __init__(self, src_path: str, is_directory: bool = False) -> None:
        self.src_path = src_path
        self.is_directory = is_directory


class TestWhatItWatches:
    def test_it_watches_the_package_directory_recursively(
        self, monkeypatch: pytest.MonkeyPatch, logger: logging.Logger
    ) -> None:
        """Recursive, or a change in any subpackage goes unnoticed."""
        import watchdog.observers

        _FakeObserver.instances.clear()
        monkeypatch.setattr(watchdog.observers, "Observer", _FakeObserver)

        dev_reload.start_reloader(logger)

        observer = _FakeObserver.instances[0]
        _handler, path, recursive = observer.scheduled[0]
        assert Path(path) == Path(dev_reload.__file__).resolve().parent
        assert recursive is True

    def test_the_observer_runs_as_a_started_daemon(
        self, monkeypatch: pytest.MonkeyPatch, logger: logging.Logger
    ) -> None:
        """Daemon, so a watcher thread cannot keep a stopped process alive."""
        import watchdog.observers

        _FakeObserver.instances.clear()
        monkeypatch.setattr(watchdog.observers, "Observer", _FakeObserver)

        dev_reload.start_reloader(logger)

        observer = _FakeObserver.instances[0]
        assert observer.daemon is True
        assert observer.started is True

    def test_without_watchdog_it_warns_and_returns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Reloading is a convenience, so its dependency is not a hard one.

        Raising here would take down every `--reload` start on a machine that
        never installed the dev extra.
        """
        monkeypatch.setitem(sys.modules, "watchdog.events", None)
        monkeypatch.setitem(sys.modules, "watchdog.observers", None)

        with caplog.at_level(logging.WARNING):
            dev_reload.start_reloader(logging.getLogger("test.dev_reload.missing"))

        assert "watchdog" in caplog.text
        assert not _FakeTimer.created


class TestWhichPathsCount:
    @pytest.mark.parametrize("name", ["a.py", "b.json", "c.yaml", "d.yml"])
    def test_source_suffixes_are_watched(self, handler: Any, name: str) -> None:
        assert handler._should_watch(f"/src/wactorz/{name}") is True

    @pytest.mark.parametrize("name", ["notes.md", "image.png", "wactorz.log", "archive.py.bak"])
    def test_everything_else_is_not(self, handler: Any, name: str) -> None:
        """A log file is the one that matters: the app writes it while running,
        so watching it would restart the process on its own output."""
        assert handler._should_watch(f"/src/wactorz/{name}") is False

    @pytest.mark.parametrize(
        "path",
        [
            "/src/wactorz/__pycache__/actor.py",
            "/src/.git/hooks/thing.py",
            "/src/.mypy_cache/x.json",
            "/src/.ruff_cache/y.yaml",
            "/src/.pytest_cache/z.yml",
        ],
    )
    def test_generated_directories_are_ignored(self, handler: Any, path: str) -> None:
        """`__pycache__` is written *by* the import the reload triggers, so
        watching it is a restart loop rather than a reload."""
        assert handler._should_watch(path) is False


class TestWhenItSchedulesARestart:
    def test_a_source_change_schedules_one(self, handler: Any) -> None:
        handler.on_modified(_Event("/src/wactorz/app.py"))

        assert len(_FakeTimer.created) == 1
        assert _FakeTimer.created[0].started is True

    @pytest.mark.parametrize("method", ["on_modified", "on_created", "on_deleted"])
    def test_every_event_kind_schedules(self, handler: Any, method: str) -> None:
        """A new file and a deleted one change what imports resolve to just as
        an edit does, so all three arrive at the same place."""
        getattr(handler, method)(_Event("/src/wactorz/app.py"))

        assert len(_FakeTimer.created) == 1

    @pytest.mark.parametrize("method", ["on_modified", "on_created", "on_deleted"])
    def test_a_directory_event_is_ignored(self, handler: Any, method: str) -> None:
        getattr(handler, method)(_Event("/src/wactorz/agents", is_directory=True))

        assert not _FakeTimer.created

    def test_an_uninteresting_file_is_ignored(self, handler: Any) -> None:
        handler.on_modified(_Event("/src/wactorz/wactorz.log"))

        assert not _FakeTimer.created

    def test_a_second_change_replaces_the_pending_restart(self, handler: Any) -> None:
        """Editors write in bursts, and a save-all is several events. Without the
        cancel each would leave a timer and the process would restart repeatedly.
        """
        handler.on_modified(_Event("/src/wactorz/app.py"))
        handler.on_modified(_Event("/src/wactorz/actor.py"))

        assert len(_FakeTimer.created) == 2
        assert _FakeTimer.created[0].cancelled is True
        assert _FakeTimer.created[1].cancelled is False

    def test_a_change_just_after_a_restart_is_dropped(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The window exists because the restart itself touches files.

        `_last` is set as the process re-execs; an event arriving inside the next
        two seconds is the tail of that restart, not a new edit.
        """
        monkeypatch.setattr(handler, "_last", time.time())

        handler.on_modified(_Event("/src/wactorz/app.py"))

        assert not _FakeTimer.created


class TestRestarting:
    def test_it_re_execs_the_same_interpreter_and_argv(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same interpreter and same arguments, or the process comes back as
        something other than what was running."""
        calls: list[tuple[str, list[str]]] = []
        monkeypatch.setattr(os, "chdir", lambda _p: None)
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        monkeypatch.setattr(os, "execv", lambda path, args: calls.append((path, args)))

        handler._restart()

        assert calls == [(sys.executable, [sys.executable, *sys.argv])]

    def test_it_returns_to_the_directory_it_started_in(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reload that inherits a moved cwd resolves relative paths — the state
        directory among them — somewhere other than where it began."""
        seen: list[str] = []
        monkeypatch.setattr(os, "chdir", lambda p: seen.append(p))
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        monkeypatch.setattr(os, "execv", lambda _p, _a: None)

        handler._restart()

        assert seen == [dev_reload._RELOAD_CWD]

    def test_a_failed_exec_exits_rather_than_carrying_on(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`execv` only returns if it failed, and what is left is a process the
        supervisor believes is fine. Exiting lets something restart it."""
        codes: list[int] = []
        monkeypatch.setattr(os, "chdir", lambda _p: None)
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        monkeypatch.setattr(os, "execv", _raise_oserror)
        monkeypatch.setattr(os, "_exit", lambda code: codes.append(code))

        with caplog.at_level(logging.ERROR):
            handler._restart()

        assert codes == [1]
        assert "restart failed" in caplog.text


def _raise_oserror(_path: str, _args: list[str]) -> None:
    raise OSError("exec format error")
