"""Per-tab view widgets for the Wactorz TUI.

Each pane owns one tab's rendering and (where relevant) its controls, so the
:class:`~wactorz.tui.app.WactorzTUI` app stays a thin orchestrator. Snapshot-fed
panes implement :class:`TUIView`; the logs pane is self-driven.
"""

from __future__ import annotations

from ..context import TUIContext, TUIView
from .agents import AgentsPane
from .chat import ChatPane
from .home import HomePane
from .logs import LogsPane
from .settings import SettingsPane

__all__ = [
    "AgentsPane",
    "ChatPane",
    "HomePane",
    "LogsPane",
    "SettingsPane",
    "TUIContext",
    "TUIView",
]
