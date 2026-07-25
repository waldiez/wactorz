"""The per-tab panes, driven through Textual's real test pilot.

`App.run_test()` runs a genuine app headless, so these exercise mounting,
composition and the widget queries the panes depend on — the parts a plain
unit test would miss. Each pane is mounted inside the real WactorzTUI, because
the panes look each other's widgets up by id through the app's DOM.
"""

# pylint: disable=missing-function-docstring,protected-access

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from textual.widgets import Button, Input, RichLog, Static

from wactorz.tui.app import WactorzTUI
from wactorz.tui.context import TUIContext
from wactorz.tui.logo import LOGO_COMPACT
from wactorz.tui.panes import AgentsPane, ChatPane, HomePane, LogsPane, SettingsPane
from wactorz.tui.panes import home as home_mod
from wactorz.tui.panes import settings as settings_mod
from wactorz.tui.snapshot import HostStats, Snapshot


@pytest.fixture(name="ctx")
def ctx_fixture() -> TUIContext:
    """A standalone context — no actor system attached."""
    return TUIContext()


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
    assert "connected" in "\n".join(home_mod._identity_lines(snap_up, TUIContext()))
    assert "offline" in "\n".join(home_mod._identity_lines(snap_down, TUIContext()))


def test_home_shows_a_cost_limit_only_when_one_is_set() -> None:
    with_limit = "\n".join(home_mod._stats_lines(_snap(cost_usd=1.0, cost_limit_usd=10.0)))
    without = "\n".join(home_mod._stats_lines(_snap(cost_usd=1.0)))
    assert "/ $10.00" in with_limit
    assert "/ $" not in without


def test_home_humanizes_the_host_stats() -> None:
    host = HostStats(cpu_pct=12.5, cpu_cores=8, net_down_bps=2048, uptime_s=5 * 3600)
    lines = "\n".join(home_mod._stats_lines(_snap(host=host)))
    assert "12.5%" in lines and "(8 cores)" in lines
    assert "2.0 KB/s" in lines
    assert "5h 0m" in lines


def test_home_reports_the_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WACTORZ_STATE_DIR", "/var/lib/wactorz")
    assert home_mod.state_dir() == "/var/lib/wactorz"


def test_the_state_dir_has_a_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WACTORZ_STATE_DIR", raising=False)
    assert home_mod.state_dir() == "./state"


async def test_home_picks_the_compact_logo_when_narrow(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test(size=(40, 30)) as pilot:
        pane = pilot.app.query_one(HomePane)
        pane.refresh_view(_snap(), ctx)
        assert LOGO_COMPACT.splitlines()[0] in str(pane.content)


# ── agents ──────────────────────────────────────────────────────────────────


async def test_the_agents_table_has_its_columns(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        assert len(pilot.app.query_one(AgentsPane).columns) == 8


async def test_agents_are_listed_alphabetically(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        table.refresh_view(_snap(agents=[{"name": "zeta"}, {"name": "alpha"}]), ctx)
        first_col = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        assert first_col == ["alpha", "zeta"]


async def test_agent_rows_carry_their_vitals(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        table.refresh_view(
            _snap(
                agents=[
                    {
                        "name": "worker",
                        "state": "running",
                        "agent_type": "llm",
                        "messages_processed": 12,
                        "restart_count": 2,
                        "uptime": 3600,
                        "node": "edge-1",
                    }
                ]
            ),
            ctx,
        )
        row = [str(cell) for cell in table.get_row_at(0)]
        assert row[:5] == ["worker", "running", "llm", "12", "2"]
        assert row[5] == "1h 0m"
        assert row[6] == "edge-1"


async def test_missing_agent_fields_get_defaults(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        table.refresh_view(_snap(agents=[{}]), ctx)
        row = [str(cell) for cell in table.get_row_at(0)]
        assert row[0] == "?"  # no name
        assert row[2] == "core"  # no type
        assert row[6] == "local"  # no node


async def test_a_protected_agent_is_marked(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        table = pilot.app.query_one(AgentsPane)
        table.refresh_view(_snap(agents=[{"name": "a", "protected": True}]), ctx)
        assert str(table.get_row_at(0)[7]) == "🔒"


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


async def test_a_user_message_is_added(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        chat = pilot.app.query_one(ChatPane)
        chat.add_user("hello there")
        await pilot.pause()
        assert any("hello there" in str(w.content) for w in chat.query(Static))


async def test_a_reply_streams_in_place(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        chat = pilot.app.query_one(ChatPane)
        chat.begin_reply("main")
        await pilot.pause()
        before = len(chat.query(".chat-reply"))
        chat.stream("Hel")
        chat.stream("lo")
        await pilot.pause()
        assert len(chat.query(".chat-reply")) == before  # updated, not re-mounted
        assert "Hello" in str(chat.query_one(".chat-reply", Static).content)


async def test_the_reply_is_attributed_to_its_target(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        chat = pilot.app.query_one(ChatPane)
        chat.begin_reply("weather")
        chat.stream("sunny")
        await pilot.pause()
        assert "weather" in str(chat.query_one(".chat-reply", Static).content)


async def test_streaming_without_begin_opens_a_reply(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        chat = pilot.app.query_one(ChatPane)
        chat.stream("cold start")
        await pilot.pause()
        assert "cold start" in str(chat.query_one(".chat-reply", Static).content)


async def test_an_empty_reply_is_marked_as_such(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        chat = pilot.app.query_one(ChatPane)
        chat.begin_reply("main")
        chat.end_reply()
        await pilot.pause()
        assert "(no response)" in str(chat.query_one(".chat-reply", Static).content)


async def test_a_finished_reply_keeps_its_text(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        chat = pilot.app.query_one(ChatPane)
        chat.begin_reply("main")
        chat.stream("all done")
        chat.end_reply()
        await pilot.pause()
        assert "all done" in str(chat.query_one(".chat-reply", Static).content)


async def test_ending_twice_is_harmless(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        chat = pilot.app.query_one(ChatPane)
        chat.begin_reply()
        chat.end_reply()
        chat.end_reply()  # must not raise


async def test_chat_content_is_not_parsed_as_markup(app: WactorzTUI) -> None:
    # A reply containing "[bold]" must render literally, not restyle the pane.
    async with app.run_test() as pilot:
        chat = pilot.app.query_one(ChatPane)
        chat.begin_reply("main")
        chat.stream("[bold]not markup[/]")
        await pilot.pause()
        assert "[bold]not markup[/]" in str(chat.query_one(".chat-reply", Static).content)


async def test_chat_ignores_the_snapshot(app: WactorzTUI, ctx: TUIContext) -> None:
    # It is event-driven; refresh_view exists only to satisfy TUIView.
    async with app.run_test() as pilot:
        chat = pilot.app.query_one(ChatPane)
        before = len(chat.query(Static))
        chat.refresh_view(_snap(agents=[{"name": "a"}]), ctx)
        assert len(chat.query(Static)) == before


# ── settings ────────────────────────────────────────────────────────────────


async def test_settings_renders_the_grouped_config(app: WactorzTUI, ctx: TUIContext) -> None:
    async with app.run_test() as pilot:
        pane = pilot.app.query_one(SettingsPane)
        pane.refresh_view(_snap(), ctx)
        body = str(pilot.app.query_one("#settings-body", Static).content)
        for heading in ("LLM", "MQTT", "Home Assistant", "Observability", "Runtime"):
            assert heading in body


def test_settings_never_prints_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings_mod,
        "CONFIG",
        SimpleNamespace(
            llm_provider="openai",
            llm_model="gpt-9",
            llm_api_key="sk-SUPERSECRET",
            llm_cost_limit_period="daily",
            ollama_url="",
            mqtt_host="broker",
            mqtt_port=1883,
            ws_port=9001,
            mqtt_username="user",
            mqtt_password="hunter2",
            ha_url="http://ha",
            ha_token="ha-TOKEN",
            discord_token="disc-TOKEN",
            telegram_token="tg-TOKEN",
            interface="cli",
            api_key="api-SECRET",
        ),
    )
    body = "\n".join(
        settings_mod._llm_lines(_snap())
        + settings_mod._mqtt_lines()
        + settings_mod._integration_lines()
        + settings_mod._runtime_lines()
    )
    for secret in ("SUPERSECRET", "hunter2", "ha-TOKEN", "disc-TOKEN", "tg-TOKEN", "api-SECRET"):
        assert secret not in body
    assert "openai" in body  # non-secrets still shown


def test_an_anonymous_broker_is_labelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings_mod,
        "CONFIG",
        SimpleNamespace(
            mqtt_host="b", mqtt_port=1883, ws_port=9001, mqtt_username="", mqtt_password=""
        ),
    )
    assert "(anonymous)" in "\n".join(settings_mod._mqtt_lines())


def test_a_cost_limit_of_zero_reads_as_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings_mod,
        "CONFIG",
        SimpleNamespace(
            llm_provider="",
            llm_model="",
            llm_api_key="",
            llm_cost_limit_period="daily",
            ollama_url="",
        ),
    )
    assert "disabled" in "\n".join(settings_mod._llm_lines(_snap(cost_limit_usd=0)))
    assert "$25.00" in "\n".join(settings_mod._llm_lines(_snap(cost_limit_usd=25)))


def test_optional_endpoints_read_as_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_ENDPOINT", raising=False)
    assert settings_mod._optional_env("OTEL_ENDPOINT") == "[dim](disabled)[/]"
    monkeypatch.setenv("OTEL_ENDPOINT", "http://otel:4317")
    assert "http://otel:4317" in settings_mod._optional_env("OTEL_ENDPOINT")


def test_config_rows_are_aligned() -> None:
    assert settings_mod._row("host", "x") == "    [b]host          [/] x"


# ── logs ────────────────────────────────────────────────────────────────────


def _record(level: int, message: str) -> tuple[int, str]:
    return (level, message)


async def test_the_logs_toolbar_is_composed(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        assert {b.id for b in logs.query(Button)} == {"log-level", "log-pause", "log-clear"}
        assert logs.query_one("#log-search", Input)
        assert logs.query_one("#logbox", RichLog)


async def test_captured_records_reach_the_view(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.bridge.buffer.append(_record(logging.INFO, "hello world"))
        logs._drain()
        assert logs.records[-1][1] == "hello world"


async def test_draining_an_empty_buffer_does_nothing(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs._drain()
        assert not logs.records


async def test_records_below_the_level_are_retained_but_not_shown(app: WactorzTUI) -> None:
    # Retained, so raising the level later can re-render them.
    async with app.run_test() as pilot:
        logs = pilot.app.query_one(LogsPane)
        logs.min_level = logging.ERROR
        logs.bridge.buffer.append(_record(logging.INFO, "chatter"))
        logs._drain()
        assert logs.records[-1][1] == "chatter"
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
        logs.bridge.buffer.append(_record(logging.INFO, "while paused"))
        logs._drain()
        assert logs.records[-1][1] == "while paused"  # still captured


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
        logs.bridge.buffer.append(_record(logging.INFO, "gone"))
        logs._drain()
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


async def test_the_bridge_is_installed_while_the_pane_lives(app: WactorzTUI) -> None:
    async with app.run_test() as pilot:
        assert pilot.app.query_one(LogsPane).bridge._installed is True
