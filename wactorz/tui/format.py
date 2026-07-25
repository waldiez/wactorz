"""Shared presentation helpers for the TUI.

Single home for the bits every pane would otherwise re-implement: byte/duration
humanizers, agent-state and log-level colour maps, and config-value masking.
Keeping them here is the DRY backstop for the per-tab panes.
"""

from __future__ import annotations

import logging

from rich.text import Text

# ── Colour maps ──────────────────────────────────────────────────────────────

# Agent lifecycle state → Rich style.
STATE_STYLE: dict[str, str] = {
    "running": "green",
    "idle": "green",
    "starting": "cyan",
    "paused": "yellow",
    "stopping": "yellow",
    "stopped": "red",
    "failed": "bold red",
}

# Log level → Rich style.
LOG_STYLE: dict[int, str] = {
    logging.CRITICAL: "bold red",
    logging.ERROR: "red",
    logging.WARNING: "yellow",
    logging.INFO: "",
    logging.DEBUG: "dim",
}

# Min-level filter cycle for the logs tab (the bridge captures INFO and above).
LEVEL_CYCLE: list[int] = [logging.INFO, logging.WARNING, logging.ERROR]


# ── Renderables ──────────────────────────────────────────────────────────────


def state_text(state: str) -> Text:
    """Colour an agent-state label by its lifecycle state."""
    return Text(state, style=STATE_STYLE.get(state.lower(), ""))


def log_line(levelno: int, line: str) -> Text:
    """Colour a formatted log line by its level."""
    return Text(line, style=LOG_STYLE.get(levelno, ""))


# ── Humanizers ───────────────────────────────────────────────────────────────


def human_bytes(n: float) -> str:
    """Compact bytes/sec formatter: 359 B/s, 1.2 KB/s, 3.4 MB/s."""
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}/s" if unit == "B" else f"{n:.1f} {unit}/s"
        n /= 1024
    return f"{n:.1f} GB/s"


def human_duration(seconds: float) -> str:
    """Compact duration: 5h 38m, 2d 4h, 12m."""
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


# ── Config-value masking (settings tab) ──────────────────────────────────────


def mask_secret(value: object) -> str:
    """Never reveal a secret — only whether it is set."""
    return "[green]•••• set[/]" if value else "[dim](not set)[/]"


def plain(value: object) -> str:
    """Show a non-secret value, or an em-dash placeholder when empty."""
    return f"[cyan]{value}[/]" if value not in (None, "") else "[dim]—[/]"
