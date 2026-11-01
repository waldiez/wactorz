"""Chat tab — threads beside a transcript, drawn from the :class:`ChatLog`.

The list has "All" first and then each agent, with what it has said since it was
last read. The transcript shows what the log says is visible, so choosing a thread
needs nothing from the server. A reply still arriving is updated in place, as
plain text; finished, it is drawn as Markdown, as the dashboard draws it. Text is
never treated as markup, so stray brackets in what an agent says cannot break the
render.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message as TextualMessage
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..attachments import describe
from ..chatlog import ALL, USER, ChatLog, Message
from ..context import Snapshot, TUIContext
from ..theme import NODE

_HINT = (
    "[dim]Type below and press Enter. It goes to the thread on the left; @name goes "
    "to that agent. Drop a file onto the terminal to attach it.[/]"
)

#: How the transcript names the person at the keyboard.
_YOU = "you  "


class Turn(Static):
    """One message in the transcript."""

    def __init__(self, message: Message) -> None:
        kind = "chat-user" if message.role == USER else "chat-reply"
        super().__init__(classes=kind)
        self.message = message
        self.redraw()

    @property
    def shown(self) -> str:
        """The message as words, whatever it is drawn with."""
        files = " ".join(str(item.get("name")) for item in self.message.files)
        picks = " ".join(self.message.choices)
        return " ".join(
            part for part in (self.message.speaker, self.message.text, files, picks) if part
        )

    def redraw(self) -> None:
        """Draw the message as it stands now."""
        message = self.message
        if message.role == USER:
            self.update(_user_line(message.text, message.files))
        elif message.streaming or not message.text:
            self.update(
                _reply_line(message.speaker, message.text or "…", body_dim=not message.text)
            )
        else:
            self.update(_finished(message))


class ThreadList(OptionList):
    """The threads to read."""

    class Chosen(TextualMessage):
        """A person picked a thread."""

        def __init__(self, thread: str) -> None:
            super().__init__()
            self.thread = thread

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._drawn: list[tuple[str, str]] = []

    def show(self, threads: list[tuple[str, str]], log: ChatLog) -> None:
        """List ``threads``, redrawing only when something on it changed."""
        wanted = [(ALL, "All")]
        for thread, label in threads:
            unread = log.unread.get(thread, 0)
            wanted.append((thread, f"{label} ({unread})" if unread else label))
        if wanted == self._drawn:
            return
        self._drawn = wanted
        self.clear_options()
        self.add_options([Option(Text(label), id=f"t{thread}") for thread, label in wanted])
        ids = [thread for thread, _ in wanted]
        if log.selected in ids:
            self.highlighted = ids.index(log.selected)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Say which thread was picked."""
        event.stop()
        self.post_message(self.Chosen(str(event.option.id)[1:]))


class ChatPane(Horizontal):
    """Threads beside the transcript of the one being read."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.log_ = ChatLog()
        self._ctx: TUIContext | None = None
        self._threads: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        """The thread list, and the transcript with its usage hint."""
        yield ThreadList(id="chat-threads")
        with VerticalScroll(id="chat-transcript"):
            yield Static(_HINT, classes="chat-hint")

    def attach(self, ctx: TUIContext) -> None:
        """Read from ``ctx``'s log."""
        self._ctx = ctx
        self.log_ = ctx.log

    def refresh_view(self, _snap: Snapshot, ctx: TUIContext, /) -> None:
        """Keep the list of threads current."""
        self._threads = ctx.threads()
        self.query_one(ThreadList).show(self._threads, self.log_)

    @property
    def _transcript(self) -> VerticalScroll:
        return self.query_one("#chat-transcript", VerticalScroll)

    def turns(self) -> list[Turn]:
        """What the transcript shows, in order."""
        return list(self._transcript.query(Turn))

    def redraw(self) -> None:
        """Draw the thread being read from the log, from the top."""
        transcript = self._transcript
        for turn in self.turns():
            turn.remove()
        transcript.mount(*[Turn(message) for message in self.log_.visible()])
        transcript.scroll_end(animate=False)
        self.query_one(ThreadList).show(self._threads, self.log_)

    def said(self, message: Message) -> None:
        """Take in a new message, drawing it if its thread is being read."""
        if message not in self.log_.messages:
            self.log_.add(message)
        if message in self.log_.visible():
            self._transcript.mount(Turn(message))
            self._transcript.scroll_end(animate=False)
        self.query_one(ThreadList).show(self._threads, self.log_)

    def changed(self, message: Message) -> None:
        """Redraw ``message`` where it is shown, as more of it arrives."""
        for turn in self.turns():
            if turn.message is message:
                turn.redraw()
                self._transcript.scroll_end(animate=False)


def _finished(message: Message) -> Group:
    """A finished reply: its speaker, its words as Markdown, and any choices."""
    parts: list[Any] = [Text(f"{message.speaker}", style="bold green"), Markdown(message.text)]
    for number, address in enumerate(message.choices, start=1):
        parts.append(Text(f"  {number}. @{address}   /pick {number}", style="dim"))
    return Group(*parts)


def _indented(body: str, label: str) -> str:
    """``body`` with each line after the first set under the first, past ``label``."""
    return body.replace("\n", "\n" + " " * len(label))


def _user_line(text: str, attachments: Iterable[object]) -> Text:
    line = Text()
    line.append(_YOU, style=f"bold {NODE}")
    line.append(_indented(text, _YOU))
    for item in attachments:
        if isinstance(item, Mapping):
            line.append(_indented(f"\n{describe(item)}", _YOU), style="dim")
    return line


def _reply_line(label: str, body: str, *, body_dim: bool = False) -> Text:
    line = Text()
    speaker = f"{label}  "
    line.append(speaker, style="bold green")
    line.append(_indented(body, speaker), style="dim" if body_dim else "")
    return line
