"""Edge paths the happy-path tests don't reach.

The F-key bindings, the guards that fire when a pane is missing, and the
defensive branches inside the logs pane. These are cheap to get wrong precisely
because they only run when something is already unusual.
"""

# pylint: disable=missing-function-docstring,protected-access

import logging
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Input, RichLog, TabbedContent

from wactorz.tui.app import WactorzTUI
from wactorz.tui.context import TUIContext
from wactorz.tui.panes import LogsPane


@pytest.fixture(name="app")
def app_fixture() -> WactorzTUI:
    return WactorzTUI(TUIContext())


# ── F-keys ──────────────────────────────────────────────────────────────────


async def test_f2_cycles_the_log_level(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await pilot.press("f2")
        assert pilot.app.query_one(LogsPane).min_level == logging.WARNING


async def test_f3_pauses_the_log_stream(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await pilot.press("f3")
        assert pilot.app.query_one(LogsPane).paused is True


async def test_f4_clears_the_log_view(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.records.append((logging.INFO, "gone"))
        await pilot.press("f4")
        assert not logs.records


# ── the logs pane may not be mounted yet ────────────────────────────────────


@pytest.mark.parametrize("action", ["action_log_level", "action_log_pause", "action_log_clear"])
async def test_the_log_keys_are_inert_before_the_pane_exists(app: WactorzTUI, action: str) -> None:
    # BINDINGS are live from the first frame; on_mount sets self.logs after.
    async with app.run_test() as pilot:
        pilot.app.logs = None  # pyright: ignore[reportAttributeAccessIssue]
        getattr(pilot.app, action)()  # must not raise


async def test_the_filter_command_is_inert_without_the_pane(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        pilot.app.logs = None  # pyright: ignore[reportAttributeAccessIssue]
        pilot.app._handle_command("filter mqtt")  # pyright: ignore[reportAttributeAccessIssue]
        assert pilot.app.query_one(TabbedContent).active == "logs"  # still navigates


async def test_a_log_command_without_the_pane_reports_unknown(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        pilot.app.logs = None  # pyright: ignore[reportAttributeAccessIssue]
        pilot.app._handle_command("clear")  # pyright: ignore[reportAttributeAccessIssue]
        await pilot.pause()
        assert any("unknown command" in n.message for n in pilot.app._notifications)


async def test_an_empty_command_reports_unknown(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        pilot.app._handle_command("")  # pyright: ignore[reportAttributeAccessIssue]
        await pilot.pause()
        assert any("unknown command" in n.message for n in pilot.app._notifications)


# ── logs pane internals ─────────────────────────────────────────────────────


async def test_a_buffer_drained_concurrently_does_not_raise(
    app: WactorzTUI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The drain timer and a clear can interleave; popleft() may lose the race.
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.bridge.buffer.append((logging.INFO, "one"))

        class _Vanishing:
            def __bool__(self) -> bool:
                return True

            def popleft(self) -> None:
                raise IndexError("emptied under us")

        monkeypatch.setattr(logs.bridge, "buffer", _Vanishing())
        logs._drain()  # must not raise
        assert not logs.records


async def test_rerendering_replays_only_matching_records(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.records.extend(
            [(logging.INFO, "mqtt up"), (logging.ERROR, "http down"), (logging.INFO, "mqtt down")]
        )
        # RichLog.lines only fills once it has rendered, so capture the writes.
        written: list = []
        # pylint: disable=line-too-long
        pilot.app.query_one("#logbox", RichLog).write = written.append  # type: ignore[method-assign]
        logs.set_filter("mqtt")
        assert [line.plain for line in written] == ["mqtt up", "mqtt down"]


async def test_resuming_replays_what_arrived_while_paused(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.toggle_pause()
        logs.bridge.buffer.append((logging.INFO, "buffered"))
        logs._drain()
        logs.toggle_pause()  # resume re-renders the retained history
        assert logs.records[-1][1] == "buffered"


async def test_an_unrelated_button_is_ignored(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        before = (logs.min_level, logs.paused)
        stray = Button("stray", id="not-ours")
        logs.on_button_pressed(SimpleNamespace(button=stray))  # type: ignore[arg-type]
        assert (logs.min_level, logs.paused) == before


async def test_a_button_without_an_id_is_ignored(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        before = (logs.min_level, logs.paused)
        logs.on_button_pressed(SimpleNamespace(button=Button("anon")))  # type: ignore[arg-type]
        assert (logs.min_level, logs.paused) == before


async def test_an_unrelated_input_does_not_change_the_filter(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.on_input_changed(  # type: ignore[arg-type]
            SimpleNamespace(  # type: ignore[arg-type]
                input=Input(id="cmd"),
                value="typed in the command bar",
            )
        )
        assert logs.search == ""


async def test_the_bridge_is_removed_when_the_pane_unmounts(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        bridge = logs.bridge
        assert bridge._installed is True
        await logs.remove()
        await pilot.pause()
        assert bridge._installed is False


# ── module entry point ──────────────────────────────────────────────────────


def test_the_module_entry_point_imports_cleanly() -> None:
    # `python -m wactorz.tui` — importing must not start an event loop.
    import wactorz.tui.__main__ as entry  # pylint: disable=import-outside-toplevel

    assert entry.run_async is not None
