"""Settings tab — read-only view of the running config, secrets masked.

Every value goes through :func:`~wactorz.tui.format.mask_secret` or
:func:`~wactorz.tui.format.plain`; nothing here may print a credential.
"""

from __future__ import annotations

import os

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ...config import CONFIG
from ..context import Snapshot, TUIContext
from ..format import mask_secret, plain


def _row(label: str, value: str) -> str:
    """One aligned ``label  value`` line."""
    return f"    [b]{label:<14}[/] {value}"


def _optional_env(name: str) -> str:
    """An env-backed endpoint, or a dim "(disabled)" when unset."""
    value = os.environ.get(name, "")
    return plain(value) if value else "[dim](disabled)[/]"


def _llm_lines(snap: Snapshot) -> list[str]:
    """Provider, model, key state and the spend cap."""
    limit = (
        f"[green]${snap.cost_limit_usd:.2f}[/] / {CONFIG.llm_cost_limit_period}"
        if snap.cost_limit_usd > 0
        else "[dim]disabled[/]"
    )
    return [
        "  [yellow]LLM[/]",
        _row("provider", plain(CONFIG.llm_provider)),
        _row("model", plain(CONFIG.llm_model)),
        _row("api key", mask_secret(CONFIG.llm_api_key)),
        _row("cost limit", limit),
        _row("ollama url", plain(CONFIG.ollama_url)),
    ]


def _mqtt_lines() -> list[str]:
    """Broker endpoint and credentials state."""
    username = plain(CONFIG.mqtt_username) if CONFIG.mqtt_username else "[dim](anonymous)[/]"
    return [
        "  [yellow]MQTT[/]",
        _row("host", plain(CONFIG.mqtt_host)),
        _row("port", plain(CONFIG.mqtt_port)),
        _row("ws port", plain(CONFIG.ws_port)),
        _row("username", username),
        _row("password", mask_secret(CONFIG.mqtt_password)),
    ]


def _integration_lines() -> list[str]:
    """Home Assistant, observability sinks and chat integrations."""
    return [
        "  [yellow]Home Assistant[/]",
        _row("url", plain(CONFIG.ha_url)),
        _row("token", mask_secret(CONFIG.ha_token)),
        "",
        "  [yellow]Observability[/]",
        _row("otel", _optional_env("OTEL_ENDPOINT")),
        _row("influx", _optional_env("INFLUX_URL")),
        "",
        "  [yellow]Integrations[/]",
        _row("discord", mask_secret(CONFIG.discord_token)),
        _row("telegram", mask_secret(CONFIG.telegram_token)),
    ]


def _runtime_lines() -> list[str]:
    """How this process itself is configured."""
    return [
        "  [yellow]Runtime[/]",
        _row("interface", plain(CONFIG.interface)),
        _row("api key", mask_secret(CONFIG.api_key)),
        _row("state dir", plain(os.environ.get("WACTORZ_STATE_DIR", "./state"))),
    ]


class SettingsPane(VerticalScroll):
    """Scrollable, grouped config readout. Reflects live config each refresh."""

    def compose(self) -> ComposeResult:
        """A single body Static, rewritten on every refresh."""
        yield Static(id="settings-body")

    def refresh_view(self, snap: Snapshot, _ctx: TUIContext, /) -> None:
        """Re-render the config readout from the live CONFIG singleton."""
        body = "\n".join(
            [
                "  [dim]read-only — reflects the running config[/]",
                "",
                *_llm_lines(snap),
                "",
                *_mqtt_lines(),
                "",
                *_integration_lines(),
                "",
                *_runtime_lines(),
            ]
        )
        self.query_one("#settings-body", Static).update(body)
