"""Tests for POST /chat/stop (monitor_server.rest_chat_stop_handler).

The handler must work in both runtime modes:
  • direct_ws — cancel the in-process generation task(s) tracked locally.
  • mqtt (legacy) — publish {"action": "stop"} to io/chat/control qos 1.

The handler returns a real aiohttp ``web.json_response``; its payload is read
back with ``_payload`` (json.loads on the response body). No aiohttp stubbing —
faking the module cannot reach monitor_server's module-level ``web`` reference
anyway, and real aiohttp keeps the test fully order-independent.
"""

import json

import pytest

import wactorz.monitor_server as m
from wactorz.monitor import chat, runtime


def _payload(resp):
    """Decode the JSON body of a real aiohttp ``web.json_response``."""
    return json.loads(resp.body)


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


@pytest.fixture(name="patched")
def patched_fixture(monkeypatch):
    """Reset the module globals the handler reads (in-flight tasks, mqtt ref)."""
    monkeypatch.setattr(chat, "inflight_chat_tasks", set())
    monkeypatch.setattr(runtime, "mqtt_client_ref", None)


async def test_stop_cancels_inflight_and_publishes(patched: pytest.MonkeyPatch):
    running = _FakeTask(done=False)
    finished = _FakeTask(done=True)
    mqtt = _FakeMqtt()
    chat.inflight_chat_tasks = {running, finished}
    runtime.mqtt_client_ref = mqtt

    resp = await m.rest_chat_stop_handler(None)
    payload = _payload(resp)

    # Only the not-done task is cancelled.
    assert running.cancelled is True
    assert finished.cancelled is False
    assert payload["cancelled"] == 1

    # Legacy MQTT path: io/chat/control {"action": "stop"} qos 1.
    assert len(mqtt.published) == 1
    topic, published, qos = mqtt.published[0]
    assert topic == "io/chat/control"
    assert json.loads(published) == {"action": "stop"}
    assert qos == 1
    assert payload["published"] is True
    assert payload["status"] == "stopped"


async def test_stop_when_idle_and_no_broker_is_harmless(patched: pytest.MonkeyPatch):
    resp = await m.rest_chat_stop_handler(None)
    payload = _payload(resp)

    assert payload["cancelled"] == 0
    assert payload["published"] is False
    assert payload["status"] == "stopped"
