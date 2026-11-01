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

    def __init__(self, client: Client) -> None:
        self.client = client
        self.state = LiveState()
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
        return MAIN

    async def history(self, limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
        """The latest chat_log rows, oldest first, for the chat tab to restore."""
        try:
            rows = await self.client.chat.history(limit=limit)
        except ClientError:
            logger.warning("[tui] could not read the chat history", exc_info=True)
            return []
        return list(reversed(rows))

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

    def chat_stream(self, text: str, attachments: list[Attachment]) -> AsyncIterator[Part]:
        """Say ``text`` and yield what comes back until the turn ends."""
        return self.client.chat.send(text, attachments=attachments)

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

    async def follow(self, on_logs: Callable[[LogEntries], None]) -> None:
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
            if isinstance(event, Frame) and event.type == "app_log":
                on_logs(self._unseen(list(event.raw.get("entries") or ())))

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
