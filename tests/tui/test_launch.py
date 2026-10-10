"""Starting the TUI from `wactorz --interface tui`.

The process has built its actor system by the time it reaches the TUI, and the
TUI's own entry point builds one when it is started on its own. Handing it
nothing from here would make a second system in the same process: a second main
actor, and broker connections under client ids the first set already holds.
"""

# pylint: disable=missing-function-docstring,protected-access

import asyncio
import sys
from types import SimpleNamespace
from typing import cast

import pytest

from wactorz import app as app_mod
from wactorz.errors import StartupError
from wactorz.orchestration import Orchestrator
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
    orchestrator = cast(Orchestrator, object())

    await app_mod._run_tui(system, main, orchestrator, [])

    assert len(seen) == 1
    context = seen[0]
    assert context is not None
    assert context.system is system
    assert context.main_actor is main
    assert context.orchestrator is orchestrator


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
    await app_mod._run_tui(
        SimpleNamespace(registry=None),
        SimpleNamespace(),
        cast(Orchestrator, object()),
        [companion()],
    )
    await asyncio.wait_for(cancelled.wait(), timeout=5)


async def test_without_the_extra_it_is_a_startup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # As every other refused start is: the command reports it, and a host
    # program can catch it. The companions it was handed never start.
    monkeypatch.setitem(sys.modules, "wactorz.tui.app", None)  # what a missing textual does
    started: list[bool] = []

    async def companion() -> None:
        started.append(True)

    run = companion()
    with pytest.raises(StartupError, match=r"wactorz\[tui\]"):
        await app_mod._run_tui(
            SimpleNamespace(registry=None), SimpleNamespace(), cast(Orchestrator, object()), [run]
        )

    assert started == []
    assert run.cr_frame is None  # closed, so never left un-awaited
