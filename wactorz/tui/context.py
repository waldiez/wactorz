"""Data access for the TUI.

Wraps the live actor system when the TUI is launched in-process, and degrades
gracefully to host-only stats (psutil) when run standalone for UI work. Every
accessor is defensive: a half-built or absent system never crashes the UI.
"""

from __future__ import annotations

import asyncio
import getpass
import inspect
import logging
import socket
import time
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from ..agents.llm.attachments import to_blocks
from ..agents.lookup import MAIN_ACTOR_NAME
from ..config import CONFIG
from ..core.persistence import chat_turn_recorded, get_db
from ..monitoring.log_redaction import redact
from ..web import uploads
from ..web.chat import turn_attribution
from .attachments import Staged, store_staged
from .snapshot import HostStats, Snapshot

if TYPE_CHECKING:
    from wactorz.core.actor import Actor
    from wactorz.core.registry import ActorSystem

logger = logging.getLogger(__name__)

#: How many chat_log rows the chat tab restores on start: the page the
#: dashboard's own history request asks for when it names no limit.
HISTORY_LIMIT = 200


class _NetCounters(Protocol):  # pylint: disable=too-few-public-methods
    """The two psutil ``net_io_counters()`` fields the TUI reads.

    Spelled out here so the signature does not need psutil, which is an
    optional dependency.
    """

    bytes_recv: int
    bytes_sent: int


@runtime_checkable
class TUIView(Protocol):  # pylint: disable=too-few-public-methods
    """A pane refreshed from the periodic :class:`Snapshot`.

    The parameters are positional-only, so a pane that ignores one can name it
    ``_snap`` / ``_ctx`` without breaking structural conformance.
    """

    def refresh_view(self, snap: Snapshot, ctx: TUIContext, /) -> None:
        """Re-render this pane from the latest snapshot."""


class TUIContext:
    """Bridges the TUI to the running system (or nothing, when standalone)."""

    def __init__(
        self,
        *,
        main_actor: Actor | None = None,
        system: ActorSystem | None = None,
    ) -> None:
        self.main_actor = main_actor
        self.system = system
        self.registry = system.registry if system else None

        self.user = getpass.getuser()
        try:
            self.host = socket.gethostname()
        except OSError:
            self.host = "localhost"

        self._started = time.monotonic()
        self._net_last: tuple[float, int, int] | None = None  # (t, recv, sent)

    # ── Identity / config (cheap, read once per render) ──────────────────────

    @property
    def llm_model(self) -> str:
        """Configured LLM model, or an em-dash when unset."""
        return CONFIG.llm_model or "—"

    @property
    def llm_provider(self) -> str:
        """Configured LLM provider, or an em-dash when unset."""
        return CONFIG.llm_provider or "—"

    @property
    def broker(self) -> str:
        """Configured MQTT broker as ``host:port``."""
        return f"{CONFIG.mqtt_host}:{CONFIG.mqtt_port}"

    # ── Chat ─────────────────────────────────────────────────────────────────

    def attribution(self, text: str) -> str:
        """Which agent a turn belongs to: the label it gets here, and its chat_log rows.

        With a system attached this is the dashboard's own rule, so a turn typed
        here lands in the same thread as one typed in the browser. Standalone
        there is nothing to route to, and a mention is taken at its word.
        """
        mentioned = text[1:].split()[0] if text.startswith("@") and text[1:].strip() else ""
        if self.main_actor is None:
            return mentioned or MAIN_ACTOR_NAME
        try:
            return turn_attribution(text)
        except Exception:  # pylint: disable=broad-exception-caught
            # A bare "@" has no name to split off; label it as the fallback does.
            return mentioned or MAIN_ACTOR_NAME

    async def history(self, limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
        """The latest chat_log rows, oldest first, for the chat tab to restore.

        The table the dashboard rebuilds its threads from, so a conversation had
        in either shows up in both. Empty when standalone or before a database is
        open, and a failed read is an empty tab rather than a broken one.
        """
        db = get_db() if self.main_actor is not None else None
        if db is None:
            return []
        try:
            rows = await asyncio.to_thread(db.query_chat_log, limit=limit)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[tui] could not read the chat history", exc_info=True)
            return []
        return list(reversed(rows))

    async def store_attachments(
        self, staged: list[Staged]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Store dropped files as uploads: the records for the turn, and what failed."""
        records: list[dict[str, Any]] = []
        failed: list[str] = []
        for item in staged:
            try:
                records.append(await asyncio.to_thread(store_staged, item))
            except (OSError, ValueError) as exc:
                failed.append(f"{item.name} ({exc})")
        return records, failed

    async def chat_stream(
        self, text: str, attachments: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[str]:
        """Yield reply chunks for a user message, one piece at a time.

        Routes through the main orchestrator, which streams text chunks and
        emits a trailing marker dict (``system_msg`` etc.); ``@name`` targeting
        and slash-commands are resolved there. Falls back to the non-streaming
        call, and to a notice when no system is attached (standalone UI).

        Both halves of the turn are stored here, the way the dashboard's socket
        stores its own: marked as recorded, so an agent that also stores the
        turns it answers does not write this one a second time, and stored with
        the attachments, which only this side knows about.
        """
        actor = self.main_actor
        if actor is None:
            yield "(no system attached — standalone UI)"
            return

        agent = self.attribution(text)
        # Set inside the task that runs this turn, so the mark follows the call
        # into the agent and no other turn sees it.
        chat_turn_recorded.set(True)
        await _record("user", text, agent, attachments)
        said: list[str] = []
        try:
            async for chunk in _reply(actor, text, attachments or []):
                said.append(chunk)
                yield chunk
        except Exception as exc:
            said.append(f"\n[error] {exc}")
            await _record("assistant", "".join(said), agent)
            raise
        await _record("assistant", "".join(said), agent)

    # ── Snapshot ─────────────────────────────────────────────────────────────

    async def snapshot(self) -> Snapshot:
        """Sample the domain and the host into one refresh tick."""
        snap = Snapshot()
        await self._fill_agents(snap)
        self._fill_cost(snap)
        snap.broker_connected = self._broker_connected()
        snap.host = self._host_stats()
        return snap

    async def _fill_agents(self, snap: Snapshot) -> None:
        """Populate agents/nodes from the live actor, else from the registry."""
        if self.main_actor is not None:
            snap.agents = await self._actor_agents()
            snap.nodes = self._actor_nodes()
            self._enrich_protected(snap.agents)
        elif self.registry is not None:
            snap.agents = self._registry_agents()

    async def _actor_agents(self) -> list[dict]:
        """``list_agents()`` off the main actor — arbitrary agent code.

        MainActor's is a coroutine, but a custom actor may expose a plain
        method, so await only what is actually awaitable.
        """
        lister = _zero_arg_method(self.main_actor, "list_agents")
        if lister is None:
            return []
        try:
            # it seems that pylint cannot see the callable through the getattr lookup
            # in _zero_arg_method, even with the cast; basedpyright types it fine.
            result = lister()  # pylint: disable=not-callable
            if inspect.isawaitable(result):
                result = await result
            return list(result or [])
        except Exception:  # pylint: disable=broad-exception-caught
            return []

    def _actor_nodes(self) -> list[dict]:
        """``list_nodes()`` off the main actor — arbitrary agent code.

        Sync by contract (MainActor.list_nodes): there is no loop to await on
        here, so an actor that made it a coroutine is skipped rather than
        leaving an un-awaited coroutine in the snapshot.
        """
        lister = _sync_zero_arg_method(self.main_actor, "list_nodes")
        if lister is None:
            return []
        try:
            result = lister()
            return list(result or [])
        except Exception:  # pylint: disable=broad-exception-caught
            return []

    def _registry_agents(self) -> list[dict]:
        """Standalone fallback: name every actor the registry knows about."""
        registry = self.registry
        if registry is None:
            return []
        try:
            return [{"name": a.name, "state": "running"} for a in registry.all_actors()]
        except Exception:  # pylint: disable=broad-exception-caught
            return []

    def _fill_cost(self, snap: Snapshot) -> None:
        """Read the process-wide LLM spend counters, if that module loaded."""
        # Local import: llm_agent pulls in the provider stack, which the
        # standalone UI has no reason to load.
        try:
            from ..agents.llm_agent import (  # pylint: disable=import-outside-toplevel
                get_global_cost_info,
            )

            info = get_global_cost_info()
        except (ImportError, AttributeError):
            return
        snap.cost_usd = _as_float(info.get("spend"))
        snap.cost_limit_usd = _as_float(info.get("limit_usd"))

    def _enrich_protected(self, agents: list[dict]) -> None:
        """get_status() omits the protected flag — read it off the live actor."""
        registry = self.registry
        if registry is None:
            return
        for agent in agents:
            if "protected" in agent:
                continue
            try:
                actor = registry.find_by_name(agent.get("name", ""))
            except Exception:  # pylint: disable=broad-exception-caught
                actor = None
            if actor is not None:
                agent["protected"] = bool(getattr(actor, "protected", False))

    def _broker_connected(self) -> bool:
        """True when the system holds an MQTT client that isn't reporting down."""
        system = self.system
        if system is None:
            return False
        client = getattr(system, "_mqtt_client", None)
        if client is None:
            return False
        # Be permissive: presence of a client => connected unless it says otherwise.
        return bool(getattr(client, "connected", True))

    def _host_stats(self) -> HostStats:
        """Sample CPU/RAM/network/uptime. Empty stats when psutil is absent."""
        stats = HostStats()
        try:
            import psutil  # pylint: disable=import-outside-toplevel
        except ImportError:
            return stats
        try:
            stats.cpu_pct = psutil.cpu_percent(interval=None)
            stats.cpu_cores = psutil.cpu_count(logical=True) or 0
            memory = psutil.virtual_memory()
            stats.ram_used_gb = (memory.total - memory.available) / 1024**3
            stats.ram_total_gb = memory.total / 1024**3
            stats.uptime_s = time.time() - psutil.boot_time()
            self._fill_net_rates(
                stats,
                psutil.net_io_counters(),  # pyright: ignore[reportArgumentType]
            )
        except (OSError, AttributeError):
            # psutil raises OSError subclasses on restricted hosts (containers,
            # sandboxes) and returns None counters on exotic platforms.
            pass
        return stats

    def _fill_net_rates(self, stats: HostStats, io: _NetCounters) -> None:
        """Turn cumulative byte counters into per-second rates."""
        now = time.monotonic()
        if self._net_last is not None:
            then, recv, sent = self._net_last
            elapsed = max(now - then, 1e-6)
            stats.net_down_bps = max(0.0, (io.bytes_recv - recv) / elapsed)
            stats.net_up_bps = max(0.0, (io.bytes_sent - sent) / elapsed)
        self._net_last = (now, io.bytes_recv, io.bytes_sent)


async def _reply(actor: object, text: str, attachments: list[dict[str, Any]]) -> AsyncIterator[str]:
    """The agent's answer to one turn, as the chunks the transcript shows."""
    stream = getattr(actor, "process_user_input_stream", None)
    reply = getattr(actor, "process_user_input", None)
    unsent = _unsent_note(text, attachments, can_attach=stream is not None)
    if unsent:
        yield unsent

    if stream is not None:
        blocks = await _blocks(attachments) if attachments and not unsent else []
        chunks = stream(text, attachments=blocks) if blocks else stream(text)
        async for chunk in chunks:
            if isinstance(chunk, dict):
                note = chunk.get("system_msg")
                if note:
                    yield f"\n[{note}]"
                continue
            yield str(chunk)
        return

    if reply is not None:
        yield str(await reply(text))
        return

    yield "(chat unavailable on this actor)"


def _unsent_note(text: str, attachments: list[dict[str, Any]], *, can_attach: bool) -> str:
    """Say which files did not go with the turn, as the dashboard does, or ``""``.

    Main reads attachments only on the turn it answers itself. A command and an
    ``@mention`` go through its command path, which takes text, so a file sent
    with either would otherwise vanish while the reply read as though it had
    been seen.
    """
    if not attachments:
        return ""
    if text.startswith("/"):
        why = "a command does not take attachments"
    elif text.startswith("@"):
        why = "a message to one agent does not take attachments here"
    elif not can_attach:
        why = "this agent takes text only"
    else:
        return ""
    names = ", ".join(str(item.get("name") or "attachment") for item in attachments)
    return f"[note] {names} not sent — {why}.\n"


async def _blocks(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Content blocks for the model, read from the stored files off the event loop."""
    return await asyncio.to_thread(to_blocks, attachments, uploads.read_bytes)


async def _record(
    role: str, content: str, agent: str, attachments: list[dict[str, Any]] | None = None
) -> None:
    """Store one half of a turn in chat_log, redacted as the dashboard's copy is.

    Best-effort: a turn that could not be stored is still a turn the person had,
    and the reply must not fail over it.
    """
    db = get_db()
    if db is None or not (content or attachments):
        return
    try:
        await asyncio.to_thread(
            db.write_chat_log,
            ts=time.time(),
            agent_name=agent,
            role=role,
            content=redact(content),
            attachments=attachments or None,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning("[tui] chat_log write failed", exc_info=True)


def _zero_arg_method(obj: object, name: str) -> Callable[[], Any] | None:
    """A callable, no-argument attribute of ``obj``, or None if it has none.

    The actor is duck-typed here (custom actors need not implement everything),
    so the lookup goes through getattr; returning it behind this signature is
    what lets both type checkers see a plain callable at the call sites.
    """
    method = getattr(obj, name, None)
    if not callable(method):
        return None
    return cast("Callable[[], Any]", method)


def _sync_zero_arg_method(obj: object, name: str) -> Callable[[], Any] | None:
    """Like :func:`_zero_arg_method`, but None for a coroutine function.

    For sync call sites: there is no loop to await on, so an actor that made
    the method async is skipped rather than leaving an un-awaited coroutine
    in the snapshot.
    """
    method = _zero_arg_method(obj, name)
    if method is None or inspect.iscoroutinefunction(method):
        return None
    return method


def _as_float(value: object) -> float:
    """Coerce a cost counter to a float, treating junk and None as zero."""
    try:
        return float(value or 0.0)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return 0.0


async def create_context() -> TUIContext:
    """Build a context from the CLI args, wiring up a real actor system."""
    # Local imports: building the system is the heavy path, and only the
    # standalone entry point needs it.
    # pylint: disable=import-outside-toplevel
    from wactorz.app import build_system
    from wactorz.cli import get_args

    args = get_args()
    args.interface = "cli"
    system, main_actor, _ = await build_system(args)
    return TUIContext(main_actor=main_actor, system=system)
