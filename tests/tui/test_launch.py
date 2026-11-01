"""Starting the TUI from `wactorz --interface tui`.

The process serves the monitor, and the TUI connects to it over loopback like any
other client. All that is special is that this process's own logging would land on
the display.
"""

import asyncio
import logging
import sys
from argparse import Namespace
from typing import Any, cast

import pytest

from wactorz import app as app_mod
from wactorz.client import UnavailableError
from wactorz.errors import StartupError
from wactorz.tui import hosted
from wactorz.tui.context import TUIContext


class _Tty:
    def isatty(self) -> bool:
        return True

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        pass


async def test_the_tui_connects_to_this_process_with_its_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[TUIContext] = []

    async def fake_run(context: TUIContext) -> None:
        seen.append(context)

    monkeypatch.setattr(hosted, "run_async", fake_run)

    await app_mod._run_tui(18888, "the-key", [])

    assert seen[0].client.url == "http://127.0.0.1:18888"
    assert seen[0].client._transport._api_key == "the-key"


async def test_companions_stop_when_the_tui_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def companion() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def fake_run(_context: TUIContext) -> None:
        await started.wait()

    monkeypatch.setattr(hosted, "run_async", fake_run)

    await app_mod._run_tui(18888, "", [companion()])

    await asyncio.wait_for(cancelled.wait(), timeout=5)


async def test_a_monitor_that_does_not_answer_is_a_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(_context: TUIContext) -> None:
        raise UnavailableError("http://127.0.0.1:18888", "nothing there")

    monkeypatch.setattr(hosted, "run_async", fake_run)

    with pytest.raises(StartupError, match="could not connect"):
        await app_mod._run_tui(18888, "", [])


async def test_without_the_extra_it_is_a_startup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # As every other refused start is: the command reports it, and a host
    # program can catch it. The companions it was handed never start.
    monkeypatch.setitem(sys.modules, "wactorz.tui.hosted", None)  # what a missing textual does
    started: list[bool] = []

    async def companion() -> None:
        started.append(True)

    run = companion()
    with pytest.raises(StartupError, match=r"wactorz\[tui\]"):
        await app_mod._run_tui(18888, "", [run])

    assert started == []
    assert run.cr_frame is None  # closed, so never left un-awaited


async def test_it_cannot_run_without_the_monitor() -> None:
    args = Namespace(no_monitor=True, interface="tui", monitor_port=18888, port=None)

    with pytest.raises(StartupError, match="--no-monitor"):
        await app_mod._run_interface(args, cast(Any, None), None, cast(Any, object()), "tui", [])


class TestTheConsole:
    def test_console_handlers_go_for_the_length_of_the_tui_and_come_back(self) -> None:
        root = logging.getLogger()
        console = logging.StreamHandler(cast(Any, _Tty()))
        root.addHandler(console)
        try:
            with hosted.console_quiet():
                assert console not in root.handlers
            assert console in root.handlers
        finally:
            root.removeHandler(console)

    def test_a_log_file_keeps_being_written(self, tmp_path: Any) -> None:
        root = logging.getLogger()
        file = logging.FileHandler(tmp_path / "wactorz.log")
        root.addHandler(file)
        try:
            with hosted.console_quiet():
                assert file in root.handlers
        finally:
            root.removeHandler(file)
            file.close()

    def test_a_stream_that_is_closed_is_not_the_terminal(self) -> None:
        class _Closed:
            def isatty(self) -> bool:
                raise ValueError

        assert not hosted._writes_to_terminal(
            cast(logging.Handler, logging.StreamHandler(cast(Any, _Closed())))
        )

    def test_a_handler_with_no_stream_is_taken_for_the_terminal(self) -> None:
        assert hosted._writes_to_terminal(logging.NullHandler())
