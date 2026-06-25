"""Tests for ``wactorz.agents.mixins.routing.RoutingMixin``.

Exercises intent classification and the one-off actuation path against a fake
LLMAgent host. No broker or real agents required.
"""

import asyncio
import tempfile
import types
from pathlib import Path

import pytest

from wactorz.agents.mixins.routing import RoutingMixin


def run(coro):
    return asyncio.run(coro)


class FakeLLM:
    def __init__(self, decision=None, raises=None):
        self.decision = decision
        self.raises = raises

    async def complete(self, messages=None, system=None, max_tokens=None,
                       reasoning_effort=None, **_kw):
        if self.raises is not None:
            raise self.raises
        return self.decision, {"input_tokens": 7, "output_tokens": 2, "cost_usd": 0.02}


class FakeRegistry:
    def __init__(self, by_name=None):
        self._by_name = by_name or {}

    def find_by_name(self, name):
        return self._by_name.get(name)


class RoutingHost(RoutingMixin):
    def __init__(self, llm=None, registry=None):
        self.name = "main"
        self.actor_id = "id-main"
        self.llm = llm
        self._registry = registry
        self._result_futures = {}
        self._persistence_dir = Path(tempfile.mkdtemp()) / "main"
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.persisted = 0
        self.spawn_calls = []

    def _persist_cost(self):
        self.persisted += 1

    async def send(self, *_a, **_k):
        pass

    async def spawn(self, actor_class, **kwargs):
        self.spawn_calls.append((actor_class, kwargs))
        # Resolve the actuation future as the real OneOffActuatorAgent would.
        tid = kwargs.get("task_id")
        fut = self._result_futures.get(tid)
        if fut is not None and not fut.done():
            fut.set_result({"result": "turned on"})
        return object()


# ── _classify_intent ─────────────────────────────────────────────────────────

def test_classify_empty_is_other():
    assert run(RoutingHost(llm=FakeLLM("HA"))._classify_intent("")) == "OTHER"


def test_classify_slash_is_other():
    assert run(RoutingHost(llm=FakeLLM("HA"))._classify_intent("/help")) == "OTHER"


def test_classify_no_llm_is_other():
    assert run(RoutingHost(llm=None)._classify_intent("turn on the light")) == "OTHER"


@pytest.mark.parametrize("decision,expected", [
    ("HA", "HA"),
    ("PIPELINE", "PIPELINE"),
    ("ACTUATE", "ACTUATE"),
    ("OTHER", "OTHER"),
    ("pipeline then extra words", "PIPELINE"),  # first token, upper-cased
    ("nonsense", "OTHER"),                       # unknown label → OTHER
])
def test_classify_maps_llm_output(decision, expected):
    assert run(RoutingHost(llm=FakeLLM(decision))._classify_intent("do a thing")) == expected


def test_classify_accumulates_cost():
    h = RoutingHost(llm=FakeLLM("HA"))
    run(h._classify_intent("x"))
    assert h.total_input_tokens == 7 and h.total_output_tokens == 2
    assert h.persisted == 1


def test_classify_exception_is_other():
    assert run(RoutingHost(llm=FakeLLM(raises=ValueError("boom")))._classify_intent("x")) == "OTHER"


def test_classify_timeout_is_other():
    assert run(RoutingHost(llm=FakeLLM(raises=asyncio.TimeoutError()))._classify_intent("x")) == "OTHER"


# ── _is_home_automation_request ──────────────────────────────────────────────

def test_is_ha_true():
    assert run(RoutingHost(llm=FakeLLM("HA"))._is_home_automation_request("x")) is True


def test_is_ha_false():
    assert run(RoutingHost(llm=FakeLLM("PIPELINE"))._is_home_automation_request("x")) is False


# ── _handle_actuate_intent ───────────────────────────────────────────────────

def test_actuate_unconfigured_returns_message(monkeypatch):
    # Force HA unconfigured so the test doesn't depend on the local .env.
    monkeypatch.setattr(
        "wactorz.agents.mixins.routing.CONFIG",
        types.SimpleNamespace(ha_url="", ha_token=""),
    )
    msg = run(RoutingHost(llm=FakeLLM("ACTUATE"))._handle_actuate_intent("turn on light"))
    assert "not configured" in msg.lower()


def test_actuate_happy_path(monkeypatch):
    monkeypatch.setattr(
        "wactorz.agents.mixins.routing.CONFIG",
        types.SimpleNamespace(ha_url="http://ha", ha_token="tok"),
    )
    h = RoutingHost(llm=FakeLLM("ACTUATE"), registry=FakeRegistry())  # no HA agent → skip enrichment
    result = run(h._handle_actuate_intent("turn on the lamp"))
    assert result == "turned on"
    assert h.spawn_calls and h.spawn_calls[-1][0].__name__ == "OneOffActuatorAgent"