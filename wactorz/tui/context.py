"""What the TUI knows, all of it from a running server through `wactorz.client`.

Nothing here reaches into the actors. A server on this machine and one elsewhere
are the same to the panes: the socket keeps `LiveState` current, and a refresh
tick only reads it.
"""

import getpass
import logging
import socket
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol, runtime_checkable

from ..client import (
    Attachment,
    Client,
    ClientError,
    Disconnected,
    Frame,
    LiveState,
    Part,
    Server,
)
from .attachments import Staged
from .chatlog import ASSISTANT, ChatLog, Message, message_from_row
from .snapshot import HostStats, Snapshot

logger = logging.getLogger(__name__)

#: How many chat_log rows the chat tab restores on start: the page the
#: dashboard's own history request asks for when it names no limit.
HISTORY_LIMIT = 200
#: How many log lines the logs tab opens with.
LOG_BACKLOG = 500
#: How many log lines are remembered to tell a repeat by: more than one backlog holds.
LOG_REMEMBERED = 2 * LOG_BACKLOG
#: Seconds between reads of the period's spend and limit, which the socket does not carry.
COST_EVERY_S = 15.0
MAIN = "main"

LogEntries = list[dict[str, Any]]


@runtime_checkable
class TUIView(Protocol):  # pylint: disable=too-few-public-methods
    """What a snapshot-fed pane must implement.

    The parameters are positional-only, so a pane that ignores one can name it
    ``_snap`` / ``_ctx`` without breaking structural conformance.
    """

    def refresh_view(self, snap: Snapshot, ctx: "TUIContext", /) -> None:
        """Re-render this pane from the latest snapshot."""


def host_stats(reported: dict[str, Any]) -> HostStats:
    """The server's vitals as the panes read them."""
    return HostStats(
        cpu_pct=float(reported.get("cpu") or 0.0),
        mem_used_mb=float(reported.get("mem_used_mb") or 0.0),
        mem_total_mb=float(reported.get("mem_total_mb") or 0.0),
    )


class TUIContext:
    """One server, as the panes see it. `start` before use."""

    def __init__(self, client: Client, *, hosted: bool = False) -> None:
        self.client = client
        #: Whether this process started the server, so closing the TUI stops it.
        self.hosted = hosted
        self.state = LiveState()
        self.log = ChatLog()
        self.server = Server(version="", uploads=False)
        self.user = getpass.getuser()
        try:
            self.host = socket.gethostname()
        except OSError:
            self.host = "localhost"
        self._period_spend = 0.0
        self._cost_limit = 0.0
        self._cost_period = ""
        self._cost_read_at = 0.0
        #: The log lines already handed on, so a reread of the backlog repeats none.
        self._logged: OrderedDict[tuple[object, object, object], None] = OrderedDict()

    async def start(self) -> None:
        """Read what does not change while connected. A refusal here ends the start."""
        self.server = await self.client.server()

    @property
    def llm_model(self) -> str:
        """The server's model, or an em-dash when unset."""
        return self.server.llm_model or "—"

    @property
    def llm_provider(self) -> str:
        """The server's model provider, or an em-dash when unset."""
        return self.server.llm_provider or "—"

    @property
    def address(self) -> str:
        """The server this terminal is attached to."""
        return self.client.url

    # ── Chat ─────────────────────────────────────────────────────────────────

    def attribution(self, text: str) -> str:
        """Who a turn looks addressed to, until the server says who answered it."""
        if text.startswith("@") and text[1:].strip():
            return text[1:].split()[0]
        return self.label_of(self.log.selected) or MAIN

    def threads(self) -> list[tuple[str, str]]:
        """Each agent to talk to, as ``(id, label)``: a shared name is told apart by node."""
        agents = self.state.agents
        names = [str(agent.get("name") or "") for agent in agents]
        found: dict[str, str] = {}
        for agent, name in zip(agents, names, strict=True):
            node = agent.get("node")
            shared = names.count(name) > 1 and node
            found[str(agent.get("agent_id") or name)] = f"{name}@{node}" if shared else name
        for message in self.log.messages:
            if message.thread and message.thread not in found:
                found[message.thread] = message.speaker
        return sorted(found.items(), key=lambda thread: thread[1])

    def label_of(self, thread: str) -> str:
        """The name a thread goes by, or ``""`` when nothing is known of it."""
        return dict(self.threads()).get(thread, "")

    def thread_named(self, label: str) -> str | None:
        """The thread going by ``label``, or None."""
        return next((id_ for id_, name in self.threads() if name == label), None)

    async def history(
        self, *, agent_id: str | None = None, limit: int = HISTORY_LIMIT
    ) -> list[Message]:
        """Stored messages, one thread's or all of them."""
        try:
            rows = await self.client.chat.history(agent_id=agent_id, limit=limit)
        except ClientError:
            logger.warning("[tui] could not read the chat history", exc_info=True)
            return []
        return [message_from_row(row) for row in rows]

    async def commands(self) -> list[str]:
        """What the server takes after a slash, as it goes in the box."""
        try:
            forms = await self.client.chat.commands()
        except ClientError:
            logger.warning("[tui] could not read the commands", exc_info=True)
            return []
        return [str(form.get("typed") or "") for form in forms if form.get("typed")]

    async def stop_all(self) -> int:
        """Cancel the turns in progress on the server, anyone's."""
        return await self.client.chat.stop_all()

    async def store_attachments(self, staged: list[Staged]) -> tuple[list[Attachment], list[str]]:
        """Store dropped files on the server: what is stored, and what failed."""
        stored: list[Attachment] = []
        failed: list[str] = []
        for item in staged:
            try:
                stored.append(await self.client.files.upload(item.path))
            except (OSError, ClientError) as exc:
                failed.append(f"{item.name} ({exc})")
        return stored, failed

    def chat_stream(
        self, text: str, attachments: list[Attachment], *, to: str | None = None
    ) -> AsyncIterator[Part]:
        """Say ``text`` to ``to`` and yield what comes back until the turn ends."""
        return self.client.chat.send(text, to=to, attachments=attachments)

    # ── What the panes render ────────────────────────────────────────────────

    async def _backlog(self) -> LogEntries:
        """The log lines the server still holds."""
        try:
            return await self.client.system.logs(limit=LOG_BACKLOG)
        except ClientError:
            logger.warning("[tui] could not read the logs", exc_info=True)
            return []

    def _unseen(self, entries: LogEntries) -> LogEntries:
        """``entries`` less those already handed on."""
        fresh: LogEntries = []
        for entry in entries:
            key = (entry.get("ts"), entry.get("origin"), entry.get("text"))
            if key not in self._logged:
                self._logged[key] = None
                fresh.append(entry)
        while len(self._logged) > LOG_REMEMBERED:
            self._logged.popitem(last=False)
        return fresh

    def _heard(self, frame: Frame) -> Message | None:
        """An agent's own message, said outside any turn of this terminal's."""
        topic = str(frame.raw.get("topic") or "")
        payload = frame.raw.get("payload")
        if frame.type != "server_event" or not isinstance(payload, dict):
            return None
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "agents" or parts[2] != "chat":
            return None
        return Message(
            thread=str(frame.raw.get("thread_id") or parts[1]),
            role=ASSISTANT,
            speaker=str(payload.get("from") or parts[1][:8]),
            text=str(payload.get("content") or ""),
            ts=float(payload.get("timestamp") or 0.0),
        )

    async def follow(
        self,
        on_logs: Callable[[LogEntries], None],
        on_message: Callable[[Message], None] = lambda _message: None,
    ) -> None:
        """Keep the state and the logs current for as long as the connection lasts.

        The backlog is read once the socket is up, and again each time it comes
        back: it is the only record of what was logged while nothing was listening.
        What arrives on the socket meanwhile waits its turn, so order holds, and
        what is in both is shown once.
        """
        behind = True
        async for event in self.client.events():
            self.state.apply(event)
            if isinstance(event, Disconnected):
                behind = True
            if behind and self.state.connected:
                behind = False
                on_logs(self._unseen(await self._backlog()))
            if not isinstance(event, Frame):
                continue
            if event.type == "app_log":
                on_logs(self._unseen(list(event.raw.get("entries") or ())))
            heard = self._heard(event)
            if heard is not None and heard.text:
                on_message(heard)

    async def snapshot(self) -> Snapshot:
        """The state as it stands, for one refresh tick."""
        await self._read_cost()
        state = self.state
        return Snapshot(
            agents=state.agents,
            nodes=state.nodes,
            cost_usd=state.total_cost_usd,
            period_spend_usd=self._period_spend,
            cost_limit_usd=self._cost_limit,
            cost_period=self._cost_period,
            connected=state.connected,
            broker_connected=state.broker_connected,
            host=host_stats(state.host),
        )

    async def _read_cost(self) -> None:
        """Reread the period's spend and limit when due. A failure waits for the next time."""
        now = time.monotonic()
        if self._cost_read_at and now - self._cost_read_at < COST_EVERY_S:
            return
        self._cost_read_at = now
        try:
            cost = await self.client.system.cost()
        except ClientError:
            return
        self._period_spend = cost.spend_usd
        self._cost_limit = cost.limit_usd or 0.0
        self._cost_period = cost.period
