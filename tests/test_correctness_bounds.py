"""Bounds and lifecycle guarantees that only hold while their limits are enforced."""

import contextlib
import sys
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wactorz.core.actor import SIZE_WALK_BUDGET, Actor, Message
from wactorz.monitoring.prometheus import UNMATCHED_ROUTE, PrometheusMonitor
from wactorz.web import ws as ws_module
from wactorz.web.ws import HEARTBEAT_SECONDS


class _Probe(Actor):
    """Minimal concrete actor; the base class is abstract."""

    async def handle_message(self, msg: Message) -> None:
        return None


@pytest.fixture(name="actor")
def actor_fixture(tmp_path: Any) -> _Probe:
    return _Probe(name="probe", persistence_dir=str(tmp_path))


def test_size_walk_visits_no_more_than_the_budget(
    actor: _Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large state must not buy a proportionally longer walk on the event loop."""
    actor._persistent_state = {
        "history": [{"role": "user", "content": "x" * 200, "n": i} for i in range(20_000)]
    }

    calls = 0
    real_getsizeof = sys.getsizeof

    def counting_getsizeof(obj: object, /) -> int:
        nonlocal calls
        calls += 1
        return real_getsizeof(obj)

    monkeypatch.setattr("wactorz.core.actor.sys.getsizeof", counting_getsizeof)
    actor._estimate_memory_mb()

    assert calls <= SIZE_WALK_BUDGET


def test_size_walk_still_measures_small_states(actor: _Probe) -> None:
    """The budget must not flatten states that fit inside it."""
    actor._persistent_state = {"a": "x" * 5000}
    small = actor._estimate_memory_mb()
    actor._persistent_state = {"a": "x" * 500_000}
    large = actor._estimate_memory_mb()

    assert large > small


def test_unmatched_route_label_is_constant() -> None:
    """An unrouted path is caller-supplied and must never become a label value."""

    class _Resource:
        canonical = None

    class _Route:
        resource = _Resource()

    class _MatchInfo:
        route = _Route()

    request = type(
        "_Request", (), {"match_info": _MatchInfo(), "path": "/attacker/controlled/1234"}
    )()
    label = PrometheusMonitor._route_label(request)  # pyright: ignore[reportArgumentType]

    assert label == UNMATCHED_ROUTE
    assert "attacker" not in label


def test_matched_route_label_uses_the_registered_pattern() -> None:
    """A matched route keeps its canonical pattern, not the concrete path."""

    class _Resource:
        canonical = "/api/agents/{name}"

    class _Route:
        resource = _Resource()

    class _MatchInfo:
        route = _Route()

    request = type("_Request", (), {"match_info": _MatchInfo(), "path": "/api/agents/alice"})()

    assert PrometheusMonitor._route_label(request) == "/api/agents/{name}"  # pyright: ignore[reportArgumentType]


async def test_ws_handler_pings_an_idle_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler itself must ping, so a peer that vanished is detected.

    Driven end to end rather than by inspecting a response built here: the point
    is that ``ws_handler`` passes a heartbeat, which an assertion on a socket
    this test constructed would not notice being dropped.
    """
    monkeypatch.setattr("wactorz.web.ws.HEARTBEAT_SECONDS", 0.1)

    app = web.Application()
    app.router.add_get("/ws", ws_module.ws_handler)

    saw_ping = False
    async with TestClient(TestServer(app)) as client:
        # autoping=False so the ping surfaces as a message instead of being
        # answered by the client library before the test can see it.
        async with client.ws_connect("/ws", autoping=False) as socket:
            # Suppressed so a handler that never pings fails on the assertion
            # below rather than on a bare timeout.
            with contextlib.suppress(TimeoutError):
                for _ in range(20):
                    msg = await socket.receive(timeout=1.0)
                    if msg.type is aiohttp.WSMsgType.PING:
                        saw_ping = True
                        break

    assert saw_ping, "the server never pinged an idle client"


def test_heartbeat_interval_is_positive() -> None:
    """A zero or negative interval disables pinging altogether."""
    assert HEARTBEAT_SECONDS > 0
