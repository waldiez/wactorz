"""How Home Assistant's WebSocket API answers, as `/metrics` says it.

Requests are counted by command and outcome and timed; connecting is timed, and
a connection that fails is counted. A call its caller cancelled is not counted:
that is the caller giving up, not Home Assistant.
"""

import asyncio
import json
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from wactorz.core.integrations.home_assistant import ha_web_socket_client as client_module
from wactorz.core.integrations.home_assistant.ha_web_socket_client import HAWebSocketClient
from wactorz.monitoring import ha_metrics


class _FakeWS:
    """Replays queued frames, then stays silent without closing."""

    def __init__(self, frames: list[dict[str, Any]] | None = None) -> None:
        self._frames = [json.dumps(f) for f in (frames or [])]

    async def send(self, raw: str) -> None:
        return None

    async def recv(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        return None


def _client(ws: _FakeWS) -> HAWebSocketClient:
    client = HAWebSocketClient("ws://ha.local/api/websocket", "token")
    client._ws = ws  # pyright: ignore[reportAttributeAccessIssue]
    return client


def _sample(name: str, **labels: str) -> float:
    registry = CollectorRegistry()
    for collector in ha_metrics.COLLECTORS:
        registry.register(collector)
    return registry.get_sample_value(name, labels) or 0.0


def _requests(command: str, outcome: str) -> float:
    return _sample("wactorz_ha_requests_total", command=command, outcome=outcome)


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_RESPONSE_TIMEOUT", 0.1)


class TestRequests:
    async def test_an_answer_is_counted_and_timed(self) -> None:
        before = _requests("get_states", ha_metrics.OK)
        timed = _sample("wactorz_ha_request_duration_seconds_count", command="get_states")

        await _client(_FakeWS([{"id": 1, "success": True, "result": []}])).call("get_states")

        assert _requests("get_states", ha_metrics.OK) == before + 1
        assert (
            _sample("wactorz_ha_request_duration_seconds_count", command="get_states") == timed + 1
        )

    async def test_a_failure_is_an_error(self) -> None:
        before = _requests("call_service", ha_metrics.ERROR)

        with pytest.raises(RuntimeError):
            await _client(_FakeWS([{"id": 1, "success": False}])).call("call_service")

        assert _requests("call_service", ha_metrics.ERROR) == before + 1

    async def test_no_answer_in_time_is_a_timeout(self) -> None:
        # The frame wait times out inside `asyncio.wait_for`, which raises
        # asyncio's own TimeoutError on Python 3.10 and the builtin from 3.11:
        # both are a timeout.
        before = _requests("get_states", ha_metrics.TIMEOUT)

        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await _client(_FakeWS()).call("get_states")

        assert _requests("get_states", ha_metrics.TIMEOUT) == before + 1

    async def test_a_call_its_caller_cancelled_is_not_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "_RESPONSE_TIMEOUT", 3600)
        outcomes = [ha_metrics.OK, ha_metrics.ERROR, ha_metrics.TIMEOUT]
        before = [_requests("get_states", o) for o in outcomes]

        call = asyncio.create_task(_client(_FakeWS()).call("get_states"))
        await asyncio.sleep(0)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

        assert [_requests("get_states", o) for o in outcomes] == before


class TestSubscribing:
    async def test_a_subscription_is_counted_and_timed_as_a_request(self) -> None:
        before = _requests("subscribe_events", ha_metrics.OK)

        await _client(_FakeWS([{"id": 1, "success": True}])).subscribe_events("state_changed")

        assert _requests("subscribe_events", ha_metrics.OK) == before + 1

    async def test_one_never_confirmed_is_a_timeout(self) -> None:
        before = _requests("subscribe_events", ha_metrics.TIMEOUT)

        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await _client(_FakeWS()).subscribe_events("state_changed")

        assert _requests("subscribe_events", ha_metrics.TIMEOUT) == before + 1


class TestConnecting:
    async def test_a_connection_is_timed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _FakeWS([{"type": "auth_required"}, {"type": "auth_ok"}])

        async def _connect(*_args: Any, **_kwargs: Any) -> _FakeWS:
            return ws

        monkeypatch.setattr(client_module.websockets, "connect", _connect)
        before = _sample("wactorz_ha_connect_duration_seconds_count")

        async with HAWebSocketClient("ws://ha.local/api/websocket", "token"):
            pass

        assert _sample("wactorz_ha_connect_duration_seconds_count") == before + 1

    async def test_a_failed_connection_is_counted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _refused(*_args: Any, **_kwargs: Any) -> _FakeWS:
            raise OSError("connection refused")

        monkeypatch.setattr(client_module.websockets, "connect", _refused)
        before = _sample("wactorz_ha_connect_failures_total")

        with pytest.raises(OSError):
            async with HAWebSocketClient("ws://ha.local/api/websocket", "token"):
                pass

        assert _sample("wactorz_ha_connect_failures_total") == before + 1

    async def test_a_refused_token_is_counted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _FakeWS([{"type": "auth_required"}, {"type": "auth_invalid"}])

        async def _connect(*_args: Any, **_kwargs: Any) -> _FakeWS:
            return ws

        monkeypatch.setattr(client_module.websockets, "connect", _connect)
        before = _sample("wactorz_ha_connect_failures_total")

        with pytest.raises(RuntimeError):
            async with HAWebSocketClient("ws://ha.local/api/websocket", "token"):
                pass

        assert _sample("wactorz_ha_connect_failures_total") == before + 1
