"""The Textual app: tab navigation, the command bar and the refresh loop.

Driven through the real pilot, including actual keypresses — the bindings are
declared with `priority=True` to steal Tab from focus traversal, and only a real
key event proves that still works.
"""

# pylint: disable=missing-function-docstring,protected-access
# pyright: reportAttributeAccessIssue=false

import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, Static, TabbedContent

from wactorz.tui.app import TAB_IDS, WactorzTUI, run, run_async
from wactorz.tui.context import TUIContext
from wactorz.tui.meta import VERSION
from wactorz.tui.panes import ChatPane, LogsPane
from wactorz.tui.snapshot import HostStats, Snapshot


class _Ctx(TUIContext):
    """A context with a scriptable snapshot and chat stream."""

    def __init__(
        self,
        snap: Snapshot | None = None,
        chunks: tuple[str, ...] = ("hi",),
        fail: bool = False,
    ) -> None:
        super().__init__()
        self._snap = snap if snap is not None else Snapshot()
        self._chunks = chunks
        self._fail = fail
        self.asked: list[str] = []

    async def snapshot(self) -> Snapshot:
        if self._fail:
            raise RuntimeError("snapshot exploded")
        return self._snap

    async def chat_stream(self, text: str) -> AsyncIterator[str]:
        self.asked.append(text)
        for chunk in self._chunks:
            yield chunk


@pytest.fixture(name="ctx")
def ctx_fixture() -> _Ctx:
    return _Ctx()


@pytest.fixture(name="app")
def app_fixture(ctx: TUIContext) -> WactorzTUI:
    return WactorzTUI(ctx)


async def _submit(pilot: Pilot, text: str) -> None:
    """Type into the command bar and press Enter."""
    cmd = pilot.app.query_one("#cmd", Input)
    cmd.value = text
    await pilot.press("enter")
    await pilot.pause()


# ── composition ─────────────────────────────────────────────────────────────


async def test_all_five_tabs_are_composed(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        panes = pilot.app.query_one(TabbedContent).query("TabPane")
        assert [p.id for p in panes] == TAB_IDS


async def test_the_app_opens_on_home(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        assert pilot.app.query_one(TabbedContent).active == "home"


async def test_the_brand_bar_shows_the_version(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        assert VERSION in str(pilot.app.query_one("#brand", Static).content)


async def test_the_command_bar_takes_focus_on_start(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        assert pilot.app.focused is pilot.app.query_one("#cmd", Input)


async def test_the_brand_theme_is_applied(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        assert pilot.app.theme == "wactorz"


# ── refresh loop ────────────────────────────────────────────────────────────


async def test_the_status_bar_summarises_the_snapshot() -> None:
    snap = Snapshot(
        agents=[{"name": "a"}, {"name": "b"}],
        nodes=[{"id": "n"}],
        cost_usd=1.5,
        broker_connected=True,
        host=HostStats(cpu_pct=42.0),
    )
    async with WactorzTUI(_Ctx(snap)).run_test() as pilot:
        await pilot.app._refresh()
        status = str(pilot.app.query_one("#statusbar", Static).content)
        assert "agents 2" in status
        assert "nodes 1" in status
        assert "cost $1.50" in status
        assert "CPU 42%" in status


async def test_a_disconnected_broker_shows_a_hollow_dot() -> None:
    async with WactorzTUI(_Ctx(Snapshot(broker_connected=False))).run_test() as pilot:
        await pilot.app._refresh()
        assert "○" in str(pilot.app.query_one("#statusbar", Static).content)


async def test_a_failing_snapshot_does_not_crash_the_ui() -> None:
    # The next tick will try again; a bad agent must not take the app down.
    async with WactorzTUI(_Ctx(fail=True)).run_test() as pilot:
        await pilot.app._refresh()
        assert pilot.app.is_running


async def test_every_snapshot_pane_is_refreshed(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        seen = []
        pilot.app._views = tuple(
            type("V", (), {"refresh_view": lambda _s, snap, ctx, /: seen.append(snap)})()
            for _ in range(3)
        )
        await pilot.app._refresh()
        assert len(seen) == 3


# ── tab navigation ──────────────────────────────────────────────────────────


async def test_tab_moves_forward(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await pilot.press("tab")
        assert pilot.app.query_one(TabbedContent).active == "chat"


async def test_shift_tab_moves_backward_and_wraps(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await pilot.press("shift+tab")
        assert pilot.app.query_one(TabbedContent).active == TAB_IDS[-1]


async def test_tab_wraps_at_the_end(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        tabs = pilot.app.query_one(TabbedContent)
        tabs.active = TAB_IDS[-1]
        await pilot.pause()
        pilot.app.action_next_tab()
        assert tabs.active == "home"


async def test_an_unknown_active_tab_restarts_the_cycle(
    app: WactorzTUI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Textual refuses to set a bogus `active`, so stub the lookup: the point
    # is that _cycle_tab falls back to index 0 rather than raising.
    async with app.run_test() as pilot:
        stub = SimpleNamespace(active="not-a-tab")
        monkeypatch.setattr(pilot.app, "query_one", lambda *a, **k: stub)
        pilot.app.action_next_tab()
        assert stub.active == "chat"  # index 0 + 1


async def test_escape_returns_focus_to_the_command_bar(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        pilot.app.query_one(LogsPane).query_one("#log-search", Input).focus()
        await pilot.pause()
        await pilot.press("escape")
        assert pilot.app.focused is pilot.app.query_one("#cmd", Input)


async def test_the_key_hints_change_on_the_logs_tab(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        hints = pilot.app.query_one("#keyhints", Static)
        assert "switch tabs" in str(hints.content)
        pilot.app.query_one(TabbedContent).active = "logs"
        await pilot.pause()
        assert "F2" in str(hints.content)


# ── slash commands ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("tab", TAB_IDS)
async def test_a_tab_command_jumps_to_that_tab(app: WactorzTUI, tab: str) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, f"/{tab}")
        assert pilot.app.query_one(TabbedContent).active == tab


@pytest.mark.parametrize("word", ["quit", "exit", "q"])
async def test_the_quit_commands_exit(app: WactorzTUI, word: str) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, f"/{word}")
        assert not pilot.app.is_running


async def test_the_help_command_notifies(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "/help")
        assert any("Tabs:" in n.message for n in pilot.app._notifications)


async def test_an_unknown_command_warns(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "/wibble")
        # Not reserved, so it is sent to the agent rather than rejected.
        assert pilot.app.ctx.asked == ["/wibble"]


async def test_the_log_commands_reach_the_logs_pane(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        await _submit(pilot, "/level")
        assert logs.min_level == logging.WARNING
        await _submit(pilot, "/pause")
        assert logs.paused is True
        logs.records.append((logging.INFO, "x"))
        await _submit(pilot, "/clear")
        assert not logs.records


async def test_the_filter_command_jumps_and_filters(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "/filter mqtt")
        assert pilot.app.query_one(TabbedContent).active == "logs"
        assert pilot.app.query_one(LogsPane).search == "mqtt"


async def test_a_bare_filter_command_clears_the_filter(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "/filter broker")
        await _submit(pilot, "/filter")
        assert pilot.app.query_one(LogsPane).search == ""


# ── chat routing ────────────────────────────────────────────────────────────


async def test_plain_text_goes_to_the_agent(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "what is the weather")
        assert pilot.app.ctx.asked == ["what is the weather"]


async def test_chatting_switches_to_the_chat_tab(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        assert pilot.app.query_one(TabbedContent).active == "chat"


async def test_the_reply_is_rendered(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        chat = pilot.app.query_one(ChatPane)
        assert any("hi" in str(w.content) for w in chat.query(".chat-reply"))


async def test_an_at_prefix_labels_the_reply_with_the_target(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "@weather forecast?")
        chat = pilot.app.query_one(ChatPane)
        assert any("weather" in str(w.content) for w in chat.query(".chat-reply"))


async def test_a_double_slash_escapes_a_literal_slash(app: WactorzTUI) -> None:
    # "//help" must reach the agent, not open the TUI help.
    async with app.run_test() as pilot:
        await _submit(pilot, "//help")
        assert pilot.app.ctx.asked == ["/help"]


async def test_a_reserved_word_is_not_sent_to_the_agent(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "/agents")
        assert pilot.app.ctx.asked == []


async def test_an_empty_submission_does_nothing(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "   ")
        assert pilot.app.ctx.asked == []


async def test_a_bare_slash_does_nothing_harmful(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "/")
        assert pilot.app.query_one(TabbedContent).active == "chat"


async def test_the_command_bar_is_cleared_after_submitting(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        await _submit(pilot, "hello")
        assert pilot.app.query_one("#cmd", Input).value == ""


async def test_the_logs_search_box_is_not_treated_as_a_command(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        search = pilot.app.query_one(LogsPane).query_one("#log-search", Input)
        search.focus()
        search.value = "/quit"
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.is_running  # the app must not have exited


async def test_an_agent_failure_is_shown_in_the_transcript() -> None:
    class _Boom(_Ctx):
        async def chat_stream(self, text: str) -> AsyncIterator[str]:
            yield "partial"
            raise RuntimeError("agent died")

    async with WactorzTUI(_Boom()).run_test() as pilot:
        await _submit(pilot, "hello")
        chat = pilot.app.query_one(ChatPane)
        assert any("[error]" in str(w.content) for w in chat.query(".chat-reply"))
        assert pilot.app.is_running


# ── entry points ────────────────────────────────────────────────────────────


async def test_run_async_uses_the_context_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    started = []

    async def _fake_run(self: WactorzTUI) -> None:
        started.append(self.ctx)

    monkeypatch.setattr(WactorzTUI, "run_async", _fake_run)
    ctx = TUIContext()
    await run_async(ctx)
    assert started == [ctx]


async def test_run_async_builds_a_context_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wactorz.tui import app as app_mod  # pylint: disable=import-outside-toplevel

    made = TUIContext()

    async def _create() -> TUIContext:
        return made

    started = []

    async def _fake_run(self: WactorzTUI) -> None:
        started.append(self.ctx)

    monkeypatch.setattr(app_mod, "create_context", _create)
    monkeypatch.setattr(WactorzTUI, "run_async", _fake_run)
    await run_async()
    assert started == [made]


def test_run_drives_the_async_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    from wactorz.tui import app as app_mod  # pylint: disable=import-outside-toplevel

    seen = []
    monkeypatch.setattr(app_mod.asyncio, "run", seen.append)
    ctx = TUIContext()
    run(ctx)
    assert len(seen) == 1
    seen[0].close()  # never awaited; close it so there is no warning
