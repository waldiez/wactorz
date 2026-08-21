"""Signal handling and the readiness banner.

The signal path is the one that decides whether `docker stop` unwinds or kills
mid-write: cancelling the app task is what runs the shutdown `finally`, and
without a handler SIGTERM does nothing at all when the process is PID 1.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any

import pytest

from wactorz.app import _install_signal_handlers, _print_ready_banner


class TestSignalHandlers:
    async def test_it_registers_for_both_stop_signals(self) -> None:
        installed: list[int] = []
        loop = asyncio.get_running_loop()
        original = loop.add_signal_handler

        def record(sig: int, cb: Any, *args: Any) -> None:
            installed.append(sig)
            original(sig, cb, *args)

        loop.add_signal_handler = record  # type: ignore[method-assign]
        try:
            _install_signal_handlers()
        finally:
            loop.add_signal_handler = original  # type: ignore[method-assign]
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)

        assert set(installed) == {signal.SIGTERM, signal.SIGINT}

    async def test_a_signal_cancels_the_running_task(self) -> None:
        """Cancellation is the mechanism — it unwinds into the shutdown
        `finally`, rather than the handler tearing anything down itself.
        """
        handlers: dict[int, Any] = {}
        loop = asyncio.get_running_loop()
        original = loop.add_signal_handler

        def capture(sig: int, cb: Any, *args: Any) -> None:
            handlers[sig] = cb

        loop.add_signal_handler = capture  # type: ignore[method-assign]

        async def body() -> str:
            _install_signal_handlers()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                return "unwound"
            return "slept"

        task = asyncio.create_task(body())
        await asyncio.sleep(0.05)
        loop.add_signal_handler = original  # type: ignore[method-assign]

        handlers[signal.SIGTERM]()
        assert await task == "unwound"

    async def test_a_second_signal_says_so_instead_of_cancelling_twice(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Impatient double Ctrl-C during a slow shutdown must not restart the
        teardown; it says the first one is still in progress.
        """
        handlers: dict[int, Any] = {}
        loop = asyncio.get_running_loop()
        original = loop.add_signal_handler
        loop.add_signal_handler = lambda sig, cb, *a: handlers.__setitem__(sig, cb)  # type: ignore[method-assign,assignment]

        async def body() -> None:
            _install_signal_handlers()
            await asyncio.sleep(5)

        task = asyncio.create_task(body())
        await asyncio.sleep(0.05)
        loop.add_signal_handler = original  # type: ignore[method-assign]

        with caplog.at_level("INFO", logger="wactorz.app"):
            handlers[signal.SIGTERM]()
            handlers[signal.SIGTERM]()

        assert "already in progress" in caplog.text
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_windows_style_fallback_is_used_when_the_loop_refuses(self) -> None:
        """`add_signal_handler` is POSIX-only, so a refusal falls back to
        `signal.signal` rather than leaving the process unstoppable.
        """
        loop = asyncio.get_running_loop()
        original_add = loop.add_signal_handler
        fallback: list[int] = []

        def refuse(*_: Any) -> None:
            raise NotImplementedError

        loop.add_signal_handler = refuse  # type: ignore[method-assign]
        original_signal = signal.signal
        signal.signal = lambda sig, cb: fallback.append(sig)  # type: ignore[assignment]
        try:
            _install_signal_handlers()
        finally:
            loop.add_signal_handler = original_add  # type: ignore[method-assign]
            signal.signal = original_signal  # type: ignore[assignment]

        assert set(fallback) == {signal.SIGTERM, signal.SIGINT}


class TestReadyBanner:
    def test_it_prints_the_dashboard_address(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_ready_banner(8888)
        assert "http://localhost:8888/" in capsys.readouterr().out

    def test_the_box_encloses_every_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_ready_banner(8888)
        out = capsys.readouterr().out
        assert "┌" in out and "└" in out
        body = [ln for ln in out.splitlines() if ln.strip().startswith("│")]
        assert body
        assert len({len(ln) for ln in body}) == 1, "rows must be padded to one width"

    def test_a_sign_in_line_is_included_when_there_is_one(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wactorz.web import login

        monkeypatch.setattr(login, "sign_in_line", lambda _port: "Sign in    http://x/login")
        _print_ready_banner(8888)
        assert "Sign in" in capsys.readouterr().out

    def test_it_is_omitted_when_there_is_none(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wactorz.web import login

        monkeypatch.setattr(login, "sign_in_line", lambda _port: None)
        _print_ready_banner(8888)
        assert "Sign in" not in capsys.readouterr().out
