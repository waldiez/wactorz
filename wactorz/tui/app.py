"""Wactorz TUI — a Textual app: tabbed monitor + chat over the actor system."""

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, Static, TabbedContent, TabPane

from ..client import Attachment, Client, ClientError, Part, TimedOutError, UnavailableError
from .attachments import Staged, describe, examine
from .chatlog import ALL, ASSISTANT, USER, Message
from .command import CommandInput
from .completion import Offers
from .context import TUIContext, TUIView
from .meta import SUBTITLE, VERSION
from .panes import (
    AgentsPane,
    ChatPane,
    HomePane,
    LogsPane,
    SettingsPane,
)
from .panes.chat import ThreadList
from .snapshot import Snapshot
from .theme import NODE, SIGNATURE, VIOLET, WACTORZ_THEME

# Tab order (id == label). Keep in sync with the TabPanes composed below.
TAB_IDS = ["home", "chat", "agents", "logs", "settings"]

# Slash-words this terminal answers itself. Anything else after "/" goes to the
# server, which has its own /agents, /help and the rest; a leading "//" sends a word
# of these to the server after all.
_RESERVED_COMMANDS = {
    "quit",
    "exit",
    "q",
    "?",
    "tab",
    "thread",
    "stop",
    "pick",
    "detach",
    "clearlogs",
    "pause",
    "level",
    "filter",
}

#: The port a server on this machine answers on unless ``MONITOR_PORT`` says.
DEFAULT_PORT = 8888
#: Seconds to wait for a server on this machine to say it is there.
PROBE_TIMEOUT_S = 2.0

_REFRESH_INTERVAL = 1.0


_HELP = (
    "Tabs: Tab/⇧Tab or /tab <name> · Threads: ^N/^B or /thread <name> · F8 or /stop "
    "cancels every turn · /pick <n> answers a which-one · Logs: F2 level, F3 pause, F4 "
    "clear, /filter <text> · Drop a file to attach it, /detach removes it · //word sends "
    "a word of these to the server · /quit exits. Anything else after / goes to the server."
)


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
        Binding("ctrl+n", "next_thread", "Next thread", show=False),
        Binding("ctrl+b", "prev_thread", "Previous thread", show=False),
        Binding("f8", "stop_turns", "Stop", show=False),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, context: TUIContext) -> None:
        super().__init__()
        self.register_theme(WACTORZ_THEME)
        self.theme = "wactorz"
        self.ctx = context
        self._snap = Snapshot()
        self._views: tuple[TUIView, ...] = ()
        self.logs: LogsPane | None = None
        # Files dropped onto the terminal, waiting for the next message.
        self._staged: list[Staged] = []
        self._offers = Offers()
        # What was last sent, for a pick among the agents a name fitted.
        self._last_sent = ""

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
            yield Static("", id="attachments")
            yield CommandInput(
                placeholder="chat, @agent to target, or / for a command",
                id="cmd",
                # Mirrors the dashboard, which offers no drop zone when uploads
                # are off: a dropped path is then just text.
                accept_files=self.ctx.server.uploads,
                suggester=self._offers,
                offers=self._offers,
            )
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
        self.query_one(ChatPane).attach(self.ctx)
        self.set_interval(_REFRESH_INTERVAL, self._refresh)
        self.run_worker(self._refresh(), exclusive=False)
        self.run_worker(self._restore_chat(), exclusive=False)
        self.run_worker(self._follow(), exclusive=False)
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
        self._offers.agents = [label for _, label in self.ctx.threads()]
        for view in self._views:
            view.refresh_view(self._snap, self.ctx)
        self.query_one("#statusbar", Static).update(self._render_status())

    def _render_status(self) -> str:
        s = self._snap
        link = "[green]●[/]" if s.connected else "[red]○[/]"
        broker = "[green]●[/]" if s.broker_connected else "[red]○[/]"
        return (
            f"[{NODE}]agents {s.agent_count}[/] │ "
            f"[{NODE}]nodes {s.node_count}[/] │ "
            f"[green]cost ${s.cost_usd:.2f}[/] │ "
            f"CPU {s.host.cpu_pct:.0f}% │ "
            f"{link} {self.ctx.address} │ broker {broker} │ "
            f"[dim]{self.ctx.llm_model}[/]"
        )

    async def _follow(self) -> None:
        """Stay with the server, for the state and the logs."""
        try:
            await self.ctx.follow(self.query_one(LogsPane).add, self.query_one(ChatPane).said)
        except ClientError as exc:
            self.notify(f"Connection ended: {exc}", severity="error", timeout=10)

    # ── Chat history ─────────────────────────────────────────────────────────

    async def _restore_chat(self) -> None:
        """Open the chat tab on the conversation so far, and learn the server's commands."""
        self._offers.commands = await self.ctx.commands()
        self.ctx.log.restore(await self.ctx.history())
        self.query_one(ChatPane).redraw()

    def _select_thread(self, thread: str) -> None:
        """Read ``thread``, drawing it now and fetching what is stored of it."""
        self.ctx.log.select(thread)
        self.query_one(ChatPane).redraw()
        self.query_one(TabbedContent).active = "chat"
        if thread:
            self.run_worker(self._restore_thread(thread), exclusive=False)

    async def _restore_thread(self, thread: str) -> None:
        self.ctx.log.restore(await self.ctx.history(agent_id=thread))
        self.query_one(ChatPane).redraw()

    def on_thread_list_chosen(self, event: ThreadList.Chosen) -> None:
        """A thread was picked from the list."""
        self._select_thread(event.thread)

    def _step_thread(self, delta: int) -> None:
        ids = [ALL, *[thread for thread, _ in self.ctx.threads()]]
        selected = self.ctx.log.selected
        here = ids.index(selected) if selected in ids else 0
        self._select_thread(ids[(here + delta) % len(ids)])

    def action_next_thread(self) -> None:
        """Ctrl+N — read the next thread."""
        self._step_thread(1)

    def action_prev_thread(self) -> None:
        """Ctrl+B — read the previous thread."""
        self._step_thread(-1)

    def action_stop_turns(self) -> None:
        """F8 — cancel the turns in progress on the server."""
        self.run_worker(self._stop_turns(), exclusive=False)

    async def _stop_turns(self) -> None:
        try:
            stopped = await self.ctx.stop_all()
        except ClientError as exc:
            self.notify(f"Could not stop: {exc}", severity="error", timeout=5)
            return
        self.notify(f"Stopped {stopped} turn(s), on every client of this server", timeout=3)

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
            f"{k}^N{e}/{k}^B{e} threads   {k}F8{e} stop   "
            f"{k}/{e} command   {k}/?{e} help   "
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

    # ── Attachments (a file dropped onto the terminal) ───────────────────────

    def on_command_input_files_dropped(self, event: CommandInput.FilesDropped) -> None:
        """Check a drop off the event loop, then stage what the dashboard would take."""
        self.run_worker(self._stage(event.paths, event.text), exclusive=False)

    async def _stage(self, paths: list[Path], text: str) -> None:
        checked = await asyncio.to_thread(examine, paths, self.ctx.server.upload_max_bytes)
        if checked is None:
            # Not files after all — a path someone copied. Type it, as a paste would.
            lines = text.splitlines()
            self.query_one("#cmd", Input).insert_text_at_cursor(lines[0] if lines else "")
            return
        accepted, skipped = checked
        known = {item.path for item in self._staged}
        for item in accepted:
            if item.path in known:
                continue
            most = self.ctx.server.uploads_per_message
            if most and len(self._staged) >= most:
                skipped.append(f"{item.name} (over {most} files)")
                continue
            self._staged.append(item)
            known.add(item.path)
        if skipped:
            self.notify("Some files skipped: " + ", ".join(skipped), severity="warning", timeout=5)
        self._update_tray()

    def _update_tray(self) -> None:
        """Show what goes out with the next message, or hide the tray when nothing does."""
        tray = self.query_one("#attachments", Static)
        tray.display = bool(self._staged)
        line = Text()
        for item in self._staged:
            line.append(describe({"name": item.name, "size": item.size}) + "   ")
        line.append("/detach to remove", style="dim")
        tray.update(line)

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
        staged, self._staged = self._staged, []
        self._update_tray()
        self._last_sent = text
        self._offers.typed.append(text)
        self.run_worker(self._chat_worker(text, staged), exclusive=False)

    def _addressee(self, text: str) -> str | None:
        """Who ``text`` goes to: the thread being read, unless it says for itself."""
        if text.startswith(("/", "@")) or self.ctx.log.selected == ALL:
            return None
        return self.ctx.label_of(self.ctx.log.selected) or None

    def _turn_reached(self, sent: Message, agent_id: str) -> None:
        """File what was said where it arrived: a mention can move it to another thread."""
        log = self.ctx.log
        if not agent_id or sent.thread == agent_id:
            return
        log.move(sent, agent_id)
        if log.selected != ALL:
            log.select(agent_id)
        self.query_one(ChatPane).redraw()

    async def _chat_worker(self, text: str, staged: list[Staged]) -> None:
        pane = self.query_one(ChatPane)
        self.query_one(TabbedContent).active = "chat"
        stored: list[Attachment] = []
        if staged:
            stored, failed = await self.ctx.store_attachments(staged)
            if failed:
                self.notify("Could not attach: " + ", ".join(failed), severity="warning", timeout=5)
        files = [{"name": file.name, "size": file.size} for file in stored]
        sent = Message(self.ctx.log.selected, USER, "you", text, time.time(), files)
        pane.said(sent)
        label = self.ctx.attribution(text)
        reply = Message(sent.thread, ASSISTANT, label, "", time.time(), streaming=True)
        pane.said(reply)
        try:
            async for part in self.ctx.chat_stream(text, stored, to=self._addressee(text)):
                self._turn_reached(sent, part.agent_id)
                reply = self._heard_part(pane, reply, part)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Any agent-side failure is shown in the transcript, not raised.
            reply.text += f"\n[error] {exc}"
        finally:
            reply.text = reply.text or "(no response)"
            reply.streaming = False
            pane.changed(reply)

    @staticmethod
    def _heard_part(pane: ChatPane, reply: Message, part: Part) -> Message:
        """Add ``part`` to the transcript, and return the message more would go in."""
        reply.speaker = part.agent or reply.speaker
        reply.thread = part.agent_id or reply.thread
        if part.streaming or not reply.text:
            reply.text += part.text
            reply.choices = part.choices
            pane.changed(reply)
            return reply
        # A second whole reply in one turn is a message of its own.
        reply.streaming = False
        pane.changed(reply)
        following = Message(
            reply.thread, ASSISTANT, reply.speaker, part.text, time.time(), choices=part.choices
        )
        following.streaming = True
        pane.said(following)
        return following

    def _pick(self, argument: str) -> None:
        """Send the last message again, to one of the agents its name fitted."""
        offered = next((m.choices for m in reversed(self.ctx.log.messages) if m.choices), ())
        if not argument.isdigit() or not 0 < int(argument) <= len(offered):
            self.notify("Nothing to pick: /pick <number> after a list", severity="warning")
            return
        words = self._last_sent.split(maxsplit=1)
        rest = words[1] if len(words) > 1 and self._last_sent.startswith("@") else self._last_sent
        self._send_chat(f"@{offered[int(argument) - 1]} {rest}")

    def _handle_command(self, cmd: str) -> None:
        name, _, argument = cmd.partition(" ")
        name, argument = name.lower(), argument.strip()
        if name in ("quit", "exit", "q"):
            self.exit()
        elif name == "tab":
            self._go_to_tab(argument)
        elif name == "thread":
            self._go_to_thread(argument)
        elif name == "stop":
            self.action_stop_turns()
        elif name == "pick":
            self._pick(argument)
        elif name in ("clearlogs", "pause", "level") and self.logs:
            {
                "clearlogs": self.logs.clear_logs,
                "pause": self.logs.toggle_pause,
                "level": self.logs.cycle_level,
            }[name]()
        elif name == "filter":
            self.query_one(TabbedContent).active = "logs"
            if self.logs:
                self.logs.set_filter(argument)
        elif name == "detach":
            self._staged = []
            self._update_tray()
        else:
            self.notify(_HELP)

    def _go_to_tab(self, name: str) -> None:
        if name in TAB_IDS:
            self.query_one(TabbedContent).active = name
        else:
            self.notify(f"Tabs: {', '.join(TAB_IDS)}", severity="warning", timeout=3)

    def _go_to_thread(self, label: str) -> None:
        if label.lower() in ("", "all"):
            self._select_thread(ALL)
            return
        thread = self.ctx.thread_named(label.removeprefix("@"))
        if thread is None:
            self.notify(f"No thread called {label}", severity="warning", timeout=3)
        else:
            self._select_thread(thread)


async def run_async(context: TUIContext) -> None:
    """Connect ``context`` and run the TUI on the current event loop."""
    await context.start()
    await WactorzTUI(context).run_async()


async def _attach(client: Client) -> None:
    async with client:
        await run_async(TUIContext(client))


def _parsed_url(argv: list[str] | None) -> str | None:
    parser = argparse.ArgumentParser(
        prog="wactorz-tui",
        description=(
            "The Wactorz terminal UI. With no server named it uses the one on this machine, "
            "or starts one. The key comes from WACTORZ_API_KEY."
        ),
    )
    parser.add_argument("--url", help="the server, as http://host:8888 (default: WACTORZ_URL)")
    return parser.parse_args(argv).url


async def _answers(url: str) -> bool:
    """Whether anything is serving at ``url``."""
    async with Client(url, timeout=PROBE_TIMEOUT_S) as client:
        try:
            await client.server()
        except (UnavailableError, TimedOutError):
            return False
        except ClientError:
            return True  # it refused us, which takes a server
    return True


def _attach_or_start() -> None:
    """With no server named: use the one on this machine, or start one and run over it.

    Started by the command, since only that may know about the server's side, and
    in a process of its own for that reason: this one stays a client.
    """
    url = f"http://127.0.0.1:{os.environ.get('MONITOR_PORT') or DEFAULT_PORT}"
    if asyncio.run(_answers(url)):
        environ = {**os.environ, "WACTORZ_URL": url}
        asyncio.run(_attach(Client.from_env(environ)))
        return
    sys.exit(subprocess.call([sys.executable, "-m", "wactorz", "--interface", "tui"]))


def run(argv: list[str] | None = None) -> None:
    """``wactorz-tui``: attach to the server named, or if none, as `_attach_or_start` does."""
    url = _parsed_url(argv)
    try:
        if url or os.environ.get("WACTORZ_URL"):
            environ = {**os.environ, "WACTORZ_URL": url} if url else None
            asyncio.run(_attach(Client.from_env(environ)))
        else:
            _attach_or_start()
    except ClientError as exc:
        sys.exit(f"wactorz-tui: {exc}")
