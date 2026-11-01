"""The per-tab panes, driven through Textual's real test pilot.

`App.run_test()` runs a genuine app headless, so these exercise mounting,
composition and the widget queries the panes depend on — the parts a plain
unit test would miss. Each pane is mounted inside the real WactorzTUI, because
the panes look each other's widgets up by id through the app's DOM.
"""

# pylint: disable=missing-function-docstring,protected-access

import logging
from typing import Any

import pytest
from textual.widgets import Button, Input, RichLog, Static

from tests.tui.fake import context_over
from wactorz.tui.app import WactorzTUI
from wactorz.tui.context import TUIContext
from wactorz.tui.logo import LOGO_COMPACT
from wactorz.tui.panes import AgentsPane, ChatPane, HomePane, LogsPane, SettingsPane
from wactorz.tui.panes import chat as chat_mod
from wactorz.tui.panes import home as home_mod
from wactorz.tui.panes import logs as logs_mod
from wactorz.tui.panes import settings as settings_mod
from wactorz.tui.snapshot import HostStats, Snapshot


@pytest.fixture(name="ctx")
def ctx_fixture() -> TUIContext:
    """A standalone context — no actor system attached."""
    return context_over()


@pytest.fixture(name="app")
def app_fixture(ctx: TUIContext) -> WactorzTUI:
    return WactorzTUI(ctx)


def _snap(**kwargs: Any) -> Snapshot:
    host = kwargs.pop("host", None)
    snap = Snapshot(**kwargs)
    if host is not None:
        snap.host = host
    return snap


# ── home ────────────────────────────────────────────────────────────────────


async def test_home_renders_identity_and_stats(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        pane = pilot.app.query_one(HomePane)
        pane.refresh_view(_snap(agents=[{"name": "a"}], nodes=[{"id": "n"}]), ctx)
        rendered = str(pane.content)
        assert ctx.user in rendered
        assert "Agents" in rendered and "Nodes" in rendered


def test_home_shows_the_broker_state() -> None:
    snap_up = _snap(broker_connected=True)
    snap_down = _snap(broker_connected=False)
    assert "connected" in "\n".join(home_mod._identity_lines(snap_up, context_over()))
    assert "offline" in "\n".join(home_mod._identity_lines(snap_down, context_over()))


def test_home_sets_the_limit_against_the_periods_spend_not_the_all_time_total() -> None:
    snap = _snap(cost_usd=100.0, period_spend_usd=5.0, cost_limit_usd=10.0, cost_period="monthly")
    lines = "\n".join(home_mod._stats_lines(snap))
    assert "$100.00" in lines
    assert "$5.00[/] / $10.00" in lines
    assert "Monthly" in lines


def test_home_shows_no_limit_when_none_is_set() -> None:
    assert "/ $" not in "\n".join(home_mod._stats_lines(_snap(cost_usd=1.0)))


def test_home_shows_the_servers_vitals() -> None:
    host = HostStats(cpu_pct=12.5, mem_used_mb=512, mem_total_mb=2048)
    lines = "\n".join(home_mod._stats_lines(_snap(host=host)))
    assert "12.5%" in lines
    assert "512 / 2048 MB" in lines


def test_home_says_when_the_connection_is_down() -> None:
    down = "\n".join(home_mod._identity_lines(_snap(connected=False), context_over()))
    assert "reconnecting" in down


async def test_home_picks_the_compact_logo_when_narrow(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test(size=(40, 30)) as pilot:
        pane = pilot.app.query_one(HomePane)
        pane.refresh_view(_snap(), ctx)
        assert LOGO_COMPACT.splitlines()[0] in str(pane.content)


# ── agents ──────────────────────────────────────────────────────────────────


async def test_the_agents_table_has_its_columns(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        assert len(pilot.app.query_one(AgentsPane).columns) == 7


async def test_agents_are_listed_alphabetically(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        table.refresh_view(_snap(agents=[{"name": "zeta"}, {"name": "alpha"}]), ctx)
        first_col = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        assert first_col == ["alpha", "zeta"]


async def test_agent_rows_carry_their_vitals(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        agent = {
            "name": "worker",
            "state": "running",
            "task": "reading",
            "messages_processed": 12,
            "cost_usd": 0.5,
            "node": "edge-1",
        }
        table.refresh_view(_snap(agents=[agent]), ctx)
        row = [str(cell) for cell in table.get_row_at(0)]
        assert row[:6] == ["worker", "running", "reading", "12", "$0.5000", "edge-1"]


async def test_missing_agent_fields_get_defaults(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        table.refresh_view(_snap(agents=[{}]), ctx)
        row = [str(cell) for cell in table.get_row_at(0)]
        assert row[0] == "?"  # no name
        assert row[2] == "idle"  # no task
        assert row[5] == "local"  # no node


async def test_a_protected_agent_is_marked(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        table.refresh_view(_snap(agents=[{"name": "a", "protected": True}]), ctx)
        assert str(table.get_row_at(0)[6]) == "🔒"


async def test_the_table_is_rebuilt_not_appended(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        table.refresh_view(_snap(agents=[{"name": "a"}, {"name": "b"}]), ctx)
        table.refresh_view(_snap(agents=[{"name": "a"}]), ctx)
        assert table.row_count == 1


async def test_the_cursor_survives_a_refresh(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        agents = [{"name": n} for n in "abcde"]
        table.refresh_view(_snap(agents=agents), ctx)
        table.move_cursor(row=3)
        table.refresh_view(_snap(agents=agents), ctx)
        assert table.cursor_row == 3


async def test_the_cursor_clamps_when_the_list_shrinks(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        table.refresh_view(_snap(agents=[{"name": n} for n in "abcde"]), ctx)
        table.move_cursor(row=4)
        table.refresh_view(_snap(agents=[{"name": "a"}, {"name": "b"}]), ctx)
        assert table.cursor_row == 1


async def test_an_empty_agent_list_leaves_an_empty_table(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        table.refresh_view(_snap(agents=[{"name": "a"}]), ctx)
        table.refresh_view(_snap(), ctx)
        assert table.row_count == 0


# ── chat ────────────────────────────────────────────────────────────────────


async def test_the_chat_pane_opens_with_a_hint(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        chat = pilot.app.query_one(ChatPane)
        assert chat.query(".chat-hint")


# ── settings ────────────────────────────────────────────────────────────────


async def test_settings_renders_the_grouped_config(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        pane = pilot.app.query_one(SettingsPane)
        pane.refresh_view(_snap(), ctx)
        body = str(pilot.app.query_one("#settings-body", Static).content)
        for heading in ("LLM", "Server", "1.2.3", ctx.address):
            assert heading in body


def test_a_cost_limit_of_zero_reads_as_disabled() -> None:
    ctx = context_over()
    assert "disabled" in "\n".join(settings_mod._llm_lines(_snap(cost_limit_usd=0), ctx))
    assert "$25.00" in "\n".join(settings_mod._llm_lines(_snap(cost_limit_usd=25), ctx))


def test_a_log_entry_keeps_its_level_and_a_strange_one_reads_as_info() -> None:
    assert logs_mod.record_of({"level": "ERROR", "text": "x"})[0] == logging.ERROR
    assert logs_mod.record_of({"level": "ODD", "text": "x"})[0] == logging.INFO


def test_config_rows_are_aligned() -> None:
    assert settings_mod._row("host", "x") == "    [b]host          [/] x"


# ── logs ────────────────────────────────────────────────────────────────────


def _entry(level: str, text: str) -> dict[str, Any]:
    return {"level": level, "text": text, "origin": "wactorz.test", "ts": 1760000000.0}


async def test_the_logs_toolbar_is_composed(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        assert {b.id for b in logs.query(Button)} == {"log-level", "log-pause", "log-clear"}
        assert logs.query_one("#log-search", Input)
        assert logs.query_one("#logbox", RichLog)


async def test_captured_records_reach_the_view(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.add([_entry("INFO", "hello world")])
        assert "hello world" in logs.records[-1][1]


async def test_records_below_the_level_are_retained_but_not_shown(app: WactorzTUI) -> None:
    # Retained, so raising the level later can re-render them.
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.min_level = logging.ERROR
        logs.add([_entry("INFO", "chatter")])
        assert "chatter" in logs.records[-1][1]
        assert logs._matches(logging.INFO, "chatter") is False


async def test_the_level_cycles_and_wraps(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        assert logs.min_level == logging.INFO
        logs.cycle_level()
        assert logs.min_level == logging.WARNING
        logs.cycle_level()
        assert logs.min_level == logging.ERROR
        logs.cycle_level()
        assert logs.min_level == logging.INFO


async def test_the_level_button_shows_the_current_level(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.cycle_level()
        assert "WARNING" in str(logs.query_one("#log-level", Button).label)


async def test_pausing_stops_new_lines_but_keeps_recording(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.toggle_pause()
        assert logs.paused is True
        logs.add([_entry("INFO", "while paused")])
        assert "while paused" in logs.records[-1][1]  # still captured


async def test_the_pause_button_flips_its_label(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.toggle_pause()
        assert "resume" in str(logs.query_one("#log-pause", Button).label)
        logs.toggle_pause()
        assert "pause" in str(logs.query_one("#log-pause", Button).label)


async def test_clearing_drops_the_history(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.add([_entry("INFO", "gone")])
        logs.clear_logs()
        assert not logs.records


async def test_a_filter_narrows_the_view(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.set_filter("mqtt")
        assert logs.search == "mqtt"
        assert logs._matches(logging.INFO, "mqtt connected") is True
        assert logs._matches(logging.INFO, "http request") is False


async def test_filtering_is_case_insensitive(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.set_filter("  MQTT  ")
        assert logs.search == "mqtt"
        assert logs._matches(logging.INFO, "mqtt up") is True


async def test_an_empty_filter_matches_everything(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.set_filter("")
        assert logs._matches(logging.INFO, "anything") is True


async def test_setting_a_filter_updates_the_search_box(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.set_filter("broker")
        assert logs.query_one("#log-search", Input).value == "broker"


async def test_typing_in_the_search_box_filters(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        search = logs.query_one("#log-search", Input)
        search.value = "Broker"
        await pilot.pause()
        assert logs.search == "broker"


async def test_the_toolbar_buttons_drive_the_same_actions(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.query_one("#log-level", Button).press()
        await pilot.pause()
        assert logs.min_level == logging.WARNING
        logs.query_one("#log-pause", Button).press()
        await pilot.pause()
        assert logs.paused is True


def test_a_reply_over_several_lines_stays_under_its_text() -> None:
    line = chat_mod._reply_line("counter", "first\nsecond")

    assert line.plain == "counter  first\n         second"


def test_so_does_a_message_over_several_lines() -> None:
    line = chat_mod._user_line("first\nsecond", [{"name": "a.pdf", "size": 2048}])

    assert line.plain.splitlines() == ["you  first", "     second", "     📎 a.pdf (2 KB)"]
