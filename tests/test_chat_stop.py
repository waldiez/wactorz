"""Tests for POST /chat/stop (monitor_server.rest_chat_stop_handler).

The handler must work in both runtime modes:
  • direct_ws — cancel the in-process generation task(s) tracked locally.
  • mqtt (legacy) — publish {"action": "stop"} to io/chat/control qos 1.

The handler does `from aiohttp import web` at call time, so we patch a minimal
`web.json_response` for the duration (and restore it). This also keeps the test
order-independent: another test in the suite leaves an aiohttp stub without
json_response in sys.modules, which would otherwise break us.
"""
import json
import sys
import types

import pytest

import wactorz.monitor_server as m


class _FakeTask:
    """Minimal stand-in for an asyncio.Task tracked in _inflight_chat_tasks."""

    def __init__(self, done=False):
        self._done = done
        self.cancelled = False

    def done(self):
        return self._done

    def cancel(self):
        self.cancelled = True


class _FakeMqtt:
    def __init__(self):
        self.published = []

    async def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))


class _Resp:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status


@pytest.fixture
def patched(monkeypatch):
    """Provide a minimal aiohttp.web.json_response and reset module globals."""
    web = types.SimpleNamespace(json_response=lambda data, status=200: _Resp(data, status))
    monkeypatch.setitem(sys.modules, "aiohttp", types.SimpleNamespace(web=web))
    monkeypatch.setattr(m, "_inflight_chat_tasks", set(), raising=False)
    monkeypatch.setattr(m, "mqtt_client_ref", None, raising=False)


async def test_stop_cancels_inflight_and_publishes(patched):
    running = _FakeTask(done=False)
    finished = _FakeTask(done=True)
    mqtt = _FakeMqtt()
    m._inflight_chat_tasks = {running, finished}
    m.mqtt_client_ref = mqtt

    resp = await m.rest_chat_stop_handler(request=None)

    # Only the not-done task is cancelled.
    assert running.cancelled is True
    assert finished.cancelled is False
    assert resp.data["cancelled"] == 1

    # Legacy MQTT path: io/chat/control {"action": "stop"} qos 1.
    assert len(mqtt.published) == 1
    topic, payload, qos = mqtt.published[0]
    assert topic == "io/chat/control"
    assert json.loads(payload) == {"action": "stop"}
    assert qos == 1
    assert resp.data["published"] is True
    assert resp.data["status"] == "stopped"


async def test_stop_when_idle_and_no_broker_is_harmless(patched):
    resp = await m.rest_chat_stop_handler(request=None)

    assert resp.data["cancelled"] == 0
    assert resp.data["published"] is False
    assert resp.data["status"] == "stopped"
