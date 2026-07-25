"""Data access for the TUI.

Wraps the live actor system when the TUI is launched in-process, and degrades
gracefully to host-only stats (psutil) when run standalone for UI work. Every
accessor is defensive: a half-built or absent system never crashes the UI.
"""

from __future__ import annotations

import getpass
import inspect
import socket
import time
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from ..config import CONFIG
from .snapshot import HostStats, Snapshot

if TYPE_CHECKING:
    from wactorz.core.actor import Actor
    from wactorz.core.registry import ActorSystem


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

    async def chat_stream(self, text: str) -> AsyncIterator[str]:
        """Yield reply chunks for a user message, one piece at a time.

        Routes through the main orchestrator, which streams text chunks and
        emits a trailing marker dict (``system_msg`` etc.); ``@name`` targeting
        and slash-commands are resolved there. Falls back to the non-streaming
        call, and to a notice when no system is attached (standalone UI).
        """
        actor = self.main_actor
        if actor is None:
            yield "(no system attached — standalone UI)"
            return

        stream = getattr(actor, "process_user_input_stream", None)
        if stream is not None:
            async for chunk in stream(text):
                if isinstance(chunk, dict):
                    note = chunk.get("system_msg")
                    if note:
                        yield f"\n[{note}]"
                    continue
                yield str(chunk)
            return

        reply = getattr(actor, "process_user_input", None)
        if reply is not None:
            yield str(await reply(text))
            return

        yield "(chat unavailable on this actor)"

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
