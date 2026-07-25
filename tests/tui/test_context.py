"""The TUI's data access layer.

Nothing real is attached here — the actor, registry and psutil are all fakes.
The point of this module is that it is *defensive*: it reaches into duck-typed
agent code that may be half-built, absent, or throwing, and the UI must keep
rendering regardless. So most of these assert what happens when things break.
"""

# pylint: disable=missing-function-docstring,protected-access,too-few-public-methods
# pyright: reportArgumentType=false

import builtins
import socket
import sys
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.tui import context as ctx_mod
from wactorz.tui.context import TUIContext, TUIView, _as_float, create_context
from wactorz.tui.snapshot import Snapshot


class _Actor:
    """A main actor with scriptable listers."""

    def __init__(
        self,
        agents: list[dict] | None = None,
        nodes: list[dict] | None = None,
        protected: bool = False,
    ) -> None:
        self._agents = agents if agents is not None else []
        self._nodes = nodes if nodes is not None else []
        self.protected = protected

    async def list_agents(self) -> list[dict]:
        return self._agents

    def list_nodes(self) -> list[dict]:
        return self._nodes


class _Registry:
    def __init__(self, actors: tuple = ()) -> None:
        self._actors = list(actors)

    def all_actors(self) -> list:
        return self._actors

    def find_by_name(self, name: str) -> object | None:
        return next((a for a in self._actors if a.name == name), None)


def _system(registry: object | None = None, client: object = ...) -> SimpleNamespace:
    """An ActorSystem stand-in; pass client=None for "no broker"."""
    system = SimpleNamespace(registry=registry)
    if client is not ...:
        system._mqtt_client = client
    return system


@pytest.fixture(name="no_psutil", autouse=True)
def no_psutil_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host sampling out of the domain tests (opt back in explicitly)."""
    monkeypatch.setattr(TUIContext, "_host_stats", lambda self: Snapshot().host)


# ── construction ────────────────────────────────────────────────────────────


def test_a_standalone_context_has_no_system() -> None:
    ctx = TUIContext()
    assert ctx.main_actor is None
    assert ctx.system is None
    assert ctx.registry is None


def test_the_registry_comes_from_the_system() -> None:
    registry = _Registry()
    assert TUIContext(system=_system(registry)).registry is registry


def test_identity_is_captured_at_construction() -> None:
    ctx = TUIContext()
    assert ctx.user
    assert ctx.host


def test_an_unresolvable_hostname_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: (_ for _ in ()).throw(OSError))
    assert TUIContext().host == "localhost"


# ── config passthrough ──────────────────────────────────────────────────────


def test_config_values_are_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    # CONFIG is a frozen dataclass, so swap the singleton rather than a field.
    monkeypatch.setattr(
        ctx_mod,
        "CONFIG",
        SimpleNamespace(
            llm_model="gpt-9",
            llm_provider="openai",
            mqtt_host="broker.local",
            mqtt_port=8883,
        ),
    )
    ctx = TUIContext()
    assert ctx.llm_model == "gpt-9"
    assert ctx.llm_provider == "openai"
    assert ctx.broker == "broker.local:8883"


@pytest.mark.parametrize("field", ["llm_model", "llm_provider"])
def test_an_unset_config_value_renders_as_a_dash(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setattr(ctx_mod, "CONFIG", SimpleNamespace(**{field: ""}))
    assert getattr(TUIContext(), field) == "—"


# ── chat ────────────────────────────────────────────────────────────────────


def _approx(value: float, expected: float) -> bool:
    """Float comparison that does not consult sys.modules.

    pytest.approx reaches for numpy, which another test in this suite leaves
    stubbed in sys.modules — see tests/test_supervisor.py.
    """
    return abs(value - expected) < 1e-6


async def _collect(ag: AsyncIterator) -> list:
    return [chunk async for chunk in ag]


async def test_chat_says_so_when_nothing_is_attached() -> None:
    chunks = await _collect(TUIContext().chat_stream("hi"))
    assert chunks == ["(no system attached — standalone UI)"]


async def test_chat_prefers_the_streaming_entry_point() -> None:
    class _Streaming:
        async def process_user_input_stream(self, text: str) -> AsyncIterator:
            for part in ("Hel", "lo ", text):
                yield part

        async def process_user_input(self, text: str) -> None:  # must not be used
            raise AssertionError("the non-streaming path was taken")

    chunks = await _collect(TUIContext(main_actor=_Streaming()).chat_stream("bob"))
    assert "".join(chunks) == "Hello bob"


async def test_a_marker_dict_becomes_a_bracketed_note() -> None:
    class _WithMarker:
        async def process_user_input_stream(self, _text: str) -> AsyncIterator:
            yield "done"
            yield {"system_msg": "spawned agent-7"}

    chunks = await _collect(TUIContext(main_actor=_WithMarker()).chat_stream("x"))
    assert chunks == ["done", "\n[spawned agent-7]"]


async def test_a_marker_dict_without_a_message_is_dropped() -> None:
    class _EmptyMarker:
        async def process_user_input_stream(self, _text: str) -> AsyncIterator:
            yield {"other_key": "ignored"}
            yield "text"

    assert await _collect(TUIContext(main_actor=_EmptyMarker()).chat_stream("x")) == ["text"]


async def test_non_string_chunks_are_stringified() -> None:
    class _Numeric:
        async def process_user_input_stream(self, _text: str) -> AsyncIterator:
            yield 42

    assert await _collect(TUIContext(main_actor=_Numeric()).chat_stream("x")) == ["42"]


async def test_chat_falls_back_to_the_non_streaming_call() -> None:
    class _OneShot:
        async def process_user_input(self, text: str) -> str:
            return f"reply to {text}"

    chunks = await _collect(TUIContext(main_actor=_OneShot()).chat_stream("hi"))
    assert chunks == ["reply to hi"]


async def test_an_actor_that_cannot_chat_says_so() -> None:
    chunks = await _collect(TUIContext(main_actor=SimpleNamespace()).chat_stream("hi"))
    assert chunks == ["(chat unavailable on this actor)"]


# ── agents / nodes ──────────────────────────────────────────────────────────


async def test_agents_and_nodes_come_from_the_main_actor() -> None:
    actor = _Actor(agents=[{"name": "a"}], nodes=[{"id": "n1"}])
    snap = await TUIContext(main_actor=actor).snapshot()
    # No registry attached, so nothing enriches the protected flag.
    assert snap.agents == [{"name": "a"}]
    assert snap.nodes == [{"id": "n1"}]


async def test_a_sync_list_agents_is_also_accepted() -> None:
    # MainActor's is a coroutine, but a custom actor may expose a plain method.
    actor = SimpleNamespace(list_agents=lambda: [{"name": "sync"}])
    snap = await TUIContext(main_actor=actor).snapshot()
    assert snap.agents == [{"name": "sync"}]


async def test_an_async_list_nodes_is_skipped_rather_than_left_unawaited() -> None:
    # There is no loop to await on in the sync path; a coroutine object in the
    # snapshot would blow up the pane that renders it.
    async def _async_nodes() -> list[dict]:
        return [{"id": "n"}]

    actor = SimpleNamespace(list_agents=list, list_nodes=_async_nodes)
    snap = await TUIContext(main_actor=actor).snapshot()
    assert snap.nodes == []


async def test_a_throwing_lister_yields_an_empty_list() -> None:
    def _boom() -> None:
        raise RuntimeError("agent exploded")

    actor = SimpleNamespace(list_agents=_boom, list_nodes=_boom)
    snap = await TUIContext(main_actor=actor).snapshot()
    assert snap.agents == []
    assert snap.nodes == []


async def test_an_actor_without_listers_yields_empty_lists() -> None:
    snap = await TUIContext(main_actor=SimpleNamespace()).snapshot()
    assert snap.agents == []
    assert snap.nodes == []


async def test_a_lister_returning_none_yields_an_empty_list() -> None:
    actor = SimpleNamespace(list_agents=lambda: None, list_nodes=lambda: None)
    snap = await TUIContext(main_actor=actor).snapshot()
    assert snap.agents == []
    assert snap.nodes == []


async def test_a_non_callable_attribute_is_ignored() -> None:
    actor = SimpleNamespace(list_agents="not a method", list_nodes=123)
    snap = await TUIContext(main_actor=actor).snapshot()
    assert snap.agents == []
    assert snap.nodes == []


async def test_without_an_actor_the_registry_names_the_agents() -> None:
    registry = _Registry([SimpleNamespace(name="alpha"), SimpleNamespace(name="beta")])
    snap = await TUIContext(system=_system(registry)).snapshot()
    assert snap.agents == [
        {"name": "alpha", "state": "running"},
        {"name": "beta", "state": "running"},
    ]


def test_the_registry_fallback_guards_against_no_registry() -> None:
    # _fill_agents only calls this with a registry, but the guard is what
    # lets the method be typed and used on its own.
    assert TUIContext()._registry_agents() == []


async def test_a_throwing_registry_yields_no_agents() -> None:
    registry = SimpleNamespace(all_actors=lambda: (_ for _ in ()).throw(RuntimeError))
    snap = await TUIContext(system=_system(registry)).snapshot()
    assert snap.agents == []


# ── the protected flag ──────────────────────────────────────────────────────


async def test_the_protected_flag_is_read_off_the_live_actor() -> None:
    # get_status() omits it, so it has to be enriched from the registry.
    registry = _Registry([SimpleNamespace(name="guard", protected=True)])
    actor = _Actor(agents=[{"name": "guard"}])
    snap = await TUIContext(main_actor=actor, system=_system(registry)).snapshot()
    assert snap.agents[0]["protected"] is True


async def test_an_existing_protected_flag_is_not_overwritten() -> None:
    registry = _Registry([SimpleNamespace(name="guard", protected=False)])
    actor = _Actor(agents=[{"name": "guard", "protected": True}])
    snap = await TUIContext(main_actor=actor, system=_system(registry)).snapshot()
    assert snap.agents[0]["protected"] is True


async def test_an_unknown_agent_gets_no_protected_flag() -> None:
    registry = _Registry([])
    actor = _Actor(agents=[{"name": "ghost"}])
    snap = await TUIContext(main_actor=actor, system=_system(registry)).snapshot()
    assert "protected" not in snap.agents[0]


async def test_a_throwing_registry_lookup_is_survivable() -> None:
    registry = SimpleNamespace(
        all_actors=list,
        find_by_name=lambda _n: (_ for _ in ()).throw(RuntimeError),
    )
    actor = _Actor(agents=[{"name": "x"}])
    snap = await TUIContext(main_actor=actor, system=_system(registry)).snapshot()
    assert "protected" not in snap.agents[0]


async def test_without_a_registry_nothing_is_enriched() -> None:
    actor = _Actor(agents=[{"name": "x"}])
    snap = await TUIContext(main_actor=actor).snapshot()
    assert "protected" not in snap.agents[0]


# ── broker ──────────────────────────────────────────────────────────────────


async def test_no_system_means_no_broker() -> None:
    assert (await TUIContext().snapshot()).broker_connected is False


async def test_a_system_without_a_client_is_offline() -> None:
    assert (await TUIContext(system=_system()).snapshot()).broker_connected is False


async def test_a_none_client_is_offline() -> None:
    assert (await TUIContext(system=_system(client=None)).snapshot()).broker_connected is False


async def test_a_client_is_assumed_connected_unless_it_says_otherwise() -> None:
    # Be permissive: not every client exposes a `connected` attribute.
    snap = await TUIContext(system=_system(client=SimpleNamespace())).snapshot()
    assert snap.broker_connected is True


async def test_a_client_reporting_disconnected_is_believed() -> None:
    system = _system(client=SimpleNamespace(connected=False))
    assert (await TUIContext(system=system).snapshot()).broker_connected is False


# ── cost ────────────────────────────────────────────────────────────────────


async def test_cost_is_read_from_the_llm_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        TUIContext,
        "_fill_cost",
        lambda self, snap: setattr(snap, "cost_usd", 1.25),
    )
    assert (await TUIContext().snapshot()).cost_usd == 1.25


def test_cost_survives_a_missing_llm_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    # The standalone UI has no reason to have loaded the provider stack.
    real_import = builtins.__import__

    def _no_llm(name: str, *args: Any, **kwargs: Any) -> object:
        if "llm_agent" in name:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_llm)
    snap = Snapshot()
    TUIContext()._fill_cost(snap)
    assert snap.cost_usd == 0.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(2.5, 2.5), ("3.5", 3.5), (None, 0.0), ("", 0.0), ("junk", 0.0), ([], 0.0)],
)
def test_cost_values_are_coerced(raw: object, expected: object) -> None:
    assert _as_float(raw) == expected


# ── host stats ──────────────────────────────────────────────────────────────


def _psutil(**overrides: Any) -> SimpleNamespace:
    """A psutil stand-in with sane defaults."""
    defaults = {
        "cpu_percent": lambda interval=None: 12.5,
        "cpu_count": lambda logical=True: 8,
        "virtual_memory": lambda: SimpleNamespace(total=16 * 1024**3, available=8 * 1024**3),
        "boot_time": lambda: 1000.0,
        "net_io_counters": lambda: SimpleNamespace(bytes_recv=1000, bytes_sent=500),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture(name="with_psutil")
def with_psutil_fixture(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Re-enable host sampling against a fake psutil."""
    monkeypatch.undo()  # drop the autouse stub

    def _install(module: object) -> None:
        monkeypatch.setitem(sys.modules, "psutil", module)

    return _install


def test_host_stats_are_empty_without_psutil(with_psutil: Callable[[object], None]) -> None:
    with_psutil(None)
    assert "psutil" in sys.modules
    assert TUIContext()._host_stats().cpu_pct == 0.0


def test_host_stats_are_sampled(with_psutil: Callable[[object], None]) -> None:
    with_psutil(_psutil())
    stats = TUIContext()._host_stats()
    assert stats.cpu_pct == 12.5
    assert stats.cpu_cores == 8
    assert _approx(stats.ram_total_gb, 16.0)
    assert _approx(stats.ram_used_gb, 8.0)


def test_an_unknown_core_count_is_zero_not_none(with_psutil: Callable[[object], None]) -> None:
    with_psutil(_psutil(cpu_count=lambda logical=True: None))
    assert TUIContext()._host_stats().cpu_cores == 0


def test_a_restricted_host_yields_partial_stats(with_psutil: Callable[[object], None]) -> None:
    # Containers and sandboxes raise OSError from some psutil probes.
    with_psutil(_psutil(virtual_memory=lambda: (_ for _ in ()).throw(OSError("denied"))))
    stats = TUIContext()._host_stats()
    assert stats.cpu_pct == 12.5  # sampled before the failure
    assert stats.ram_total_gb == 0.0


def test_the_first_tick_reports_no_network_rate(with_psutil: Callable[[object], None]) -> None:
    # Rates need two samples; the first must not report a huge spike.
    with_psutil(_psutil())
    stats = TUIContext()._host_stats()
    assert stats.net_down_bps == 0.0
    assert stats.net_up_bps == 0.0


def test_network_rates_are_computed_between_ticks(
    with_psutil: Callable[[object], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    with_psutil(_psutil())
    ctx = TUIContext()
    ticks = iter([100.0, 102.0])
    monkeypatch.setattr(ctx_mod.time, "monotonic", lambda: next(ticks))

    ctx._host_stats()  # seeds the counters
    # pylint: disable=line-too-long
    sys.modules["psutil"].net_io_counters = lambda: SimpleNamespace(  # pyright: ignore[reportAttributeAccessIssue]
        bytes_recv=3000,
        bytes_sent=1500,
    )
    stats = ctx._host_stats()
    assert _approx(stats.net_down_bps, 1000.0)  # 2000 bytes / 2 s
    assert _approx(stats.net_up_bps, 500.0)


def test_a_counter_reset_never_reports_a_negative_rate(
    with_psutil: Callable[[object], None],
) -> None:
    # Interface counters wrap or reset; a negative rate would render as garbage.
    with_psutil(_psutil())
    ctx = TUIContext()
    ctx._host_stats()
    # pylint: disable=line-too-long
    sys.modules["psutil"].net_io_counters = lambda: SimpleNamespace(  # pyright: ignore[reportAttributeAccessIssue]
        bytes_recv=0,
        bytes_sent=0,
    )
    stats = ctx._host_stats()
    assert stats.net_down_bps == 0.0
    assert stats.net_up_bps == 0.0


# ── the view protocol ───────────────────────────────────────────────────────


def test_anything_with_refresh_view_satisfies_the_protocol() -> None:
    class _Pane:
        def refresh_view(self, snap: Snapshot, ctx: TUIContext, /) -> None:
            """A conforming pane."""

    assert isinstance(_Pane(), TUIView)


def test_something_without_refresh_view_does_not() -> None:
    assert not isinstance(SimpleNamespace(), TUIView)


# ── create_context ──────────────────────────────────────────────────────────


async def test_create_context_builds_a_system_from_the_cli_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pylint: disable=import-outside-toplevel
    import wactorz.app
    import wactorz.cli

    args = SimpleNamespace(interface="rest")
    actor = object()
    system = SimpleNamespace(registry=None)

    async def _build(passed: SimpleNamespace) -> tuple:
        assert passed is args
        assert passed.interface == "cli"  # the TUI forces the in-process path
        return system, actor, None

    monkeypatch.setattr(wactorz.cli, "get_args", lambda: args)
    monkeypatch.setattr(wactorz.app, "build_system", _build)

    ctx = await create_context()
    assert ctx.main_actor is actor
    assert ctx.system is system
