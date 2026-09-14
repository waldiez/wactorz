"""Starting the TUI from `wactorz --interface tui`.

The process has built its actor system by the time it reaches the TUI, and the
TUI's own entry point builds one when it is started on its own. Handing it
nothing from here would make a second system in the same process: a second main
actor, and broker connections under client ids the first set already holds.
"""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
from types import SimpleNamespace

import pytest

from wactorz import app as app_mod
from wactorz.tui import app as tui_app
from wactorz.tui.context import TUIContext


async def test_the_tui_is_given_the_system_this_process_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[TUIContext | None] = []

    async def fake_run(context: TUIContext | None = None) -> None:
        seen.append(context)

    async def second_system() -> TUIContext:
        raise AssertionError("the TUI built a second actor system")

    monkeypatch.setattr(tui_app, "run_async", fake_run)
    monkeypatch.setattr(tui_app, "create_context", second_system)
    system, main = SimpleNamespace(registry=None), SimpleNamespace()

    await app_mod._run_tui(system, main, [])

    assert len(seen) == 1
    context = seen[0]
    assert context is not None
    assert context.system is system
    assert context.main_actor is main


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

    async def fake_run(context: TUIContext | None = None) -> None:
        await started.wait()

    monkeypatch.setattr(tui_app, "run_async", fake_run)
    await app_mod._run_tui(SimpleNamespace(registry=None), SimpleNamespace(), [companion()])
    await asyncio.wait_for(cancelled.wait(), timeout=5)
