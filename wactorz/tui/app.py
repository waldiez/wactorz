"""Wactorz TUI — a Textual app: tabbed monitor + chat over the actor system."""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, Static, TabbedContent, TabPane

from .context import TUIContext, TUIView, create_context
from .meta import SUBTITLE, VERSION
from .panes import (
    AgentsPane,
    ChatPane,
    HomePane,
    LogsPane,
    SettingsPane,
)
from .snapshot import Snapshot
from .theme import NODE, SIGNATURE, VIOLET, WACTORZ_THEME

# Tab order (id == label). Keep in sync with the TabPanes composed below.
TAB_IDS = ["home", "chat", "agents", "logs", "settings"]

# Slash-words handled by the TUI itself. Anything else after "/" is sent to the
# agent; a leading "//" escapes a literal slash (so "//help" reaches the agent).
_RESERVED_COMMANDS = set(TAB_IDS) | {
    "quit",
    "exit",
    "q",
    "help",
    "?",
    "clear",
    "pause",
    "level",
    "filter",
}

_REFRESH_INTERVAL = 1.0


class WactorzTUI(App):
    """Top-level app: composes the panes and feeds them a periodic snapshot."""

    CSS_PATH = "app.tcss"
    TITLE = "WACTORZ"

    BINDINGS = [  # noqa: RUF012
        Binding("tab", "next_tab", "Next tab", priority=True, show=False),
        Binding("shift+tab", "prev_tab", "Prev tab", priority=True, show=False),
        Binding("f2", "log_level", "Log level", show=False),
        Binding("f3", "log_pause", "Pause logs", show=False),
        Binding("f4", "log_clear", "Clear logs", show=False),
        Binding("escape", "focus_command", "Command bar", show=False),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, context: TUIContext | None = None) -> None:
        super().__init__()
        self.register_theme(WACTORZ_THEME)
        self.theme = "wactorz"
        self.ctx = context or TUIContext()
        self._snap = Snapshot()
        self._views: tuple[TUIView, ...] = ()
        self.logs: LogsPane | None = None

    # ── Layout ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        """Brand bar, the tab strip, and the command bar / status footer."""
        yield Static(
            f"[b]WACTORZ[/] [{SIGNATURE}]v{VERSION}[/]  [dim]{SUBTITLE}[/]",
            id="brand",
        )
        with TabbedContent(initial="home"):
            with TabPane("home", id="home"):
                yield HomePane(id="home-body", classes="panel")
            with TabPane("chat", id="chat"):
                yield ChatPane(classes="panel")
            with TabPane("agents", id="agents"):
                yield AgentsPane(
                    id="agents-table",
                    classes="panel",
                    cursor_type="row",
                    zebra_stripes=True,
                )
            with TabPane("logs", id="logs"):
                yield LogsPane(classes="panel")
            with TabPane("settings", id="settings"):
                yield SettingsPane(classes="panel")
        with Vertical(id="footer"):
            yield Input(placeholder="chat, @agent to target, or / for a command", id="cmd")
            yield Static(self._render_hints(), id="keyhints")
            yield Static("", id="statusbar")

    def on_mount(self) -> None:
        """Cache the panes, start the refresh timer and focus the command bar."""
        self._views = (
            self.query_one(HomePane),
            self.query_one(ChatPane),
            self.query_one(AgentsPane),
            self.query_one(SettingsPane),
        )
        self.logs = self.query_one(LogsPane)
        self.set_interval(_REFRESH_INTERVAL, self._refresh)
        self.run_worker(self._refresh(), exclusive=False)
        self.query_one("#cmd", Input).focus()

    # ── Refresh loop ─────────────────────────────────────────────────────────

    async def _refresh(self) -> None:
        """One tick: snapshot the system, then push it into every pane."""
        try:
            self._snap = await self.ctx.snapshot()
        except Exception:  # pylint: disable=broad-exception-caught
            # snapshot() reaches into arbitrary agent code; a bad tick must
            # never take the UI down — the next tick will try again.
            return
        for view in self._views:
            view.refresh_view(self._snap, self.ctx)
        self.query_one("#statusbar", Static).update(self._render_status())

    def _render_status(self) -> str:
        s = self._snap
        broker = "[green]●[/]" if s.broker_connected else "[red]○[/]"
        return (
            f"[{NODE}]agents {s.agent_count}[/] │ "
            f"[{NODE}]nodes {s.node_count}[/] │ "
            f"[green]cost ${s.cost_usd:.2f}[/] │ "
            f"CPU {s.host.cpu_pct:.0f}% │ "
            f"broker {broker} {self.ctx.broker} │ "
            f"[dim]{self.ctx.llm_model}[/]"
        )

    # ── Key hints (context-aware) ────────────────────────────────────────────

    @staticmethod
    def _render_hints(active: str = "home") -> str:
        k, e = f"[b {VIOLET}]", "[/]"  # accent-coloured key caps
        if active == "logs":
            return (
                f"  {k}F2{e} level   {k}F3{e} pause   {k}F4{e} clear   "
                f"{k}/filter <text>{e}   {k}Tab{e}/{k}⇧Tab{e} tabs   {k}^C{e} exit"
            )
        return (
            f"  {k}Tab{e}/{k}⇧Tab{e} switch tabs   "
            f"{k}/<tab>{e} jump (e.g. /agents)   "
            f"{k}/{e} command   "
            f"{k}^C{e} or {k}/quit{e} exit"
        )

    def on_tabbed_content_tab_activated(self) -> None:
        """Key hints are per-tab — the logs tab has its own controls."""
        active = self.query_one(TabbedContent).active
        self.query_one("#keyhints", Static).update(self._render_hints(active))

    # ── Tab navigation ───────────────────────────────────────────────────────

    def _cycle_tab(self, delta: int) -> None:
        tabs = self.query_one(TabbedContent)
        try:
            i = TAB_IDS.index(tabs.active)
        except ValueError:
            i = 0
        tabs.active = TAB_IDS[(i + delta) % len(TAB_IDS)]

    def action_next_tab(self) -> None:
        """Tab — move one tab right, wrapping."""
        self._cycle_tab(1)

    def action_prev_tab(self) -> None:
        """Shift+Tab — move one tab left, wrapping."""
        self._cycle_tab(-1)

    def action_focus_command(self) -> None:
        """Esc — return focus to the command bar from any sub-control."""
        self.query_one("#cmd", Input).focus()

    # ── Logs actions (delegate to the logs pane) ─────────────────────────────

    def action_log_level(self) -> None:
        """F2 — cycle the logs minimum level."""
        if self.logs:
            self.logs.cycle_level()

    def action_log_pause(self) -> None:
        """F3 — pause/resume the log stream."""
        if self.logs:
            self.logs.toggle_pause()

    def action_log_clear(self) -> None:
        """F4 — clear the log view."""
        if self.logs:
            self.logs.clear_logs()

    # ── Command bar ──────────────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Route a submitted line to a TUI command or to the agent."""
        if event.input.id != "cmd":
            return  # the logs filter box handles its own input
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        # "//…" escapes a literal slash so it can be sent to an agent.
        if text.startswith("//"):
            self._send_chat(text[1:])
            return
        if text.startswith("/"):
            word = text[1:].split()[0].lower() if text[1:].strip() else ""
            if word in _RESERVED_COMMANDS:
                self._handle_command(text[1:].strip())
                return
            # Not a TUI command — fall through and send it to the agent as-is.
        self._send_chat(text)

    def _send_chat(self, text: str) -> None:
        self.run_worker(self._chat_worker(text), exclusive=False)

    async def _chat_worker(self, text: str) -> None:
        chat = self.query_one(ChatPane)
        self.query_one(TabbedContent).active = "chat"
        chat.add_user(text)
        label = text[1:].split()[0] if text.startswith("@") and text[1:].strip() else "main"
        chat.begin_reply(label)
        try:
            async for chunk in self.ctx.chat_stream(text):
                chat.stream(chunk)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Any agent-side failure is shown in the transcript, not raised.
            chat.stream(f"\n[error] {exc}")
        finally:
            chat.end_reply()

    def _handle_command(self, cmd: str) -> None:
        name = cmd.split()[0].lower() if cmd else ""
        if name in ("quit", "exit", "q"):
            self.exit()
        elif name in TAB_IDS:
            self.query_one(TabbedContent).active = name
        elif name in ("clear", "pause", "level") and self.logs:
            {
                "clear": self.logs.clear_logs,
                "pause": self.logs.toggle_pause,
                "level": self.logs.cycle_level,
            }[name]()
        elif name == "filter":
            term = cmd.split(" ", 1)[1].strip() if " " in cmd else ""
            self.query_one(TabbedContent).active = "logs"
            if self.logs:
                self.logs.set_filter(term)
        elif name in ("help", "?"):
            self.notify(
                "Tabs: Tab/⇧Tab or /<tab> · Logs: F2 level, F3 pause, F4 clear, "
                "/filter <text> · //text sends a literal / to the agent · /quit exit"
            )
        else:
            self.notify(f"unknown command: /{cmd}", severity="warning", timeout=3)


async def run_async(context: TUIContext | None = None) -> None:
    """Async entry point — run the TUI on the current event loop."""
    if context is None:
        context = await create_context()
    await WactorzTUI(context).run_async()


def run(context: TUIContext | None = None) -> None:
    """Blocking entry point.

    Delegate to run_async (to make sure a context is always available).
    """
    asyncio.run(run_async(context))
