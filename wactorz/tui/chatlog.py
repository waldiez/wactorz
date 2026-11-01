"""The conversation the terminal holds, apart from how it is drawn.

One log, read as threads: every message belongs to the agent whose thread it is
in, by id, since two agents may share a name, and the "All" view is every thread
in the order things were said. The pane draws what `visible` gives it, so a
switch of thread needs nothing from the server.
"""

from dataclasses import dataclass, field
from typing import Any

#: The view of every thread at once.
ALL = ""
USER = "user"
ASSISTANT = "assistant"


@dataclass
class Message:
    """One thing said, by a person or an agent."""

    thread: str
    role: str
    speaker: str
    text: str
    ts: float = 0.0
    files: list[dict[str, Any]] = field(default_factory=list)
    #: The addresses to pick from when a name fitted more than one agent.
    choices: tuple[str, ...] = ()
    #: Whether more of it is still arriving.
    streaming: bool = False


def message_from_row(row: dict[str, Any]) -> Message:
    """A stored chat_log row as a message."""
    files = row.get("attachments")
    return Message(
        thread=str(row.get("agent_id") or ""),
        role=USER if row.get("role") == USER else ASSISTANT,
        speaker=str(row.get("agent_name") or "main"),
        text=str(row.get("content") or ""),
        ts=float(row.get("ts") or 0.0),
        files=files if isinstance(files, list) else [],
    )


class ChatLog:
    """Everything said so far, and which thread is being read."""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.selected = ALL
        #: What each thread has said since it was last read.
        self.unread: dict[str, int] = {}

    def visible(self) -> list[Message]:
        """The messages of the thread being read."""
        if self.selected == ALL:
            return list(self.messages)
        return [message for message in self.messages if message.thread == self.selected]

    def add(self, message: Message) -> Message:
        """Keep ``message``, counting it as unread if nobody is looking at its thread."""
        self.messages.append(message)
        if message.role == ASSISTANT and self.selected not in (ALL, message.thread):
            self.unread[message.thread] = self.unread.get(message.thread, 0) + 1
        return message

    def restore(self, messages: list[Message]) -> None:
        """Put stored messages in, in time order, leaving out what is already here."""
        known = {(m.thread, m.role, m.text) for m in self.messages}
        fresh = [m for m in messages if (m.thread, m.role, m.text) not in known]
        self.messages = sorted([*self.messages, *fresh], key=lambda message: message.ts)

    def select(self, thread: str) -> None:
        """Read ``thread`` from now on."""
        self.selected = thread
        self.unread.pop(thread, None)

    def move(self, message: Message, thread: str) -> None:
        """File ``message`` in ``thread``: where a turn turned out to be addressed."""
        message.thread = thread
