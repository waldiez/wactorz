"""Tests for ``wactorz.agents.main.memory.MemoryMixin``.

Exercises the real mixin against a fake host that supplies the Actor/LLMAgent
surface it needs (recall/persist, registry, ``llm.complete``, token counters).
No broker or persistence backend required.

Run with ``pytest`` (or ``make test-py``). Async methods are driven through
``asyncio.run`` so no ``pytest-asyncio`` plugin is needed.
"""

import asyncio

from wactorz.agents.main.memory import MemoryMixin


def run(coro):
    return asyncio.run(coro)


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeActor:
    def __init__(self, name, description=""):
        self.name = name
        self.DESCRIPTION = description


class FakeRegistry:
    def __init__(self, actors):
        self._actors = actors

    def all_actors(self):
        return list(self._actors)


class FakeLLM:
    """Returns a preset raw string from ``complete``; records calls."""

    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    async def complete(self, messages, system=None, max_tokens=None):
        self.calls.append({"messages": messages, "system": system})
        return self.raw, {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01}


class MemoryHost(MemoryMixin):
    def __init__(self, registry=None, llm=None):
        self.name = "main"
        self._store = {}
        self._registry = registry
        self.llm = llm
        self.system_prompt = ""
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.persisted_cost = 0

    # Actor base surface
    def recall(self, key, default=None):
        return self._store.get(key, default)

    def persist(self, key, value):
        self._store[key] = value

    # LLMAgent surface
    def _persist_cost(self):
        self.persisted_cost += 1


def host(registry_actors=None, llm=None):
    reg = FakeRegistry(registry_actors) if registry_actors is not None else None
    return MemoryHost(registry=reg, llm=llm)


# ── User facts ───────────────────────────────────────────────────────────────


def test_get_user_facts_empty():
    assert host().get_user_facts() == {}


def test_get_user_facts_stored():
    h = host()
    h.persist("_user_facts", {"pref_user_name": "Bob"})
    assert h.get_user_facts() == {"pref_user_name": "Bob"}


def test_preferred_timezone():
    h = host()
    h.persist("_user_facts", {"pref_timezone": "Europe/Athens"})
    assert h._preferred_timezone_name() == "Europe/Athens"


# ── Running-agents summary ───────────────────────────────────────────────────


def test_running_agents_summary_filters():
    h = host(
        [
            FakeActor("main"),
            FakeActor("monitor"),
            FakeActor("installer"),
            FakeActor("planner-abc123"),
            FakeActor("cpu-sensor", "Reports CPU temperature"),
        ]
    )
    summary = h._get_running_agents_summary()
    assert "cpu-sensor" in summary
    assert "Reports CPU temperature" in summary
    for hidden in ("planner-abc123",):
        assert hidden not in summary
    # system agents are filtered (checked as whole lines, not substrings)
    lines = [ln.strip() for ln in summary.splitlines()]
    assert not any(ln.startswith("main ") or ln == "main" for ln in lines)
    assert not any(ln.startswith("installer") for ln in lines)


def test_running_agents_summary_empty():
    assert host([FakeActor("main")])._get_running_agents_summary() == ""


# ── Live-context prefix ──────────────────────────────────────────────────────


def test_prefix_with_agents():
    out = host([FakeActor("alpha"), FakeActor("beta")])._prefix_with_live_context(
        "what is running?"
    )
    assert "[CURRENT SYSTEM STATE" in out and "[END SYSTEM STATE]" in out
    assert "alpha, beta" in out
    assert out.endswith("what is running?")


def test_prefix_without_agents():
    out = host([])._prefix_with_live_context("hi")
    assert "NONE" in out
    assert out.endswith("hi")


# ── System-prompt assembly ───────────────────────────────────────────────────


def test_rebuild_system_prompt():
    h = host([FakeActor("cpu-sensor", "CPU temps")])
    h.persist(
        "_user_facts",
        {
            "pref_user_name": "Bob",
            "device_ha_url": "http://x",
            "policy_quiet": "23-07",
        },
    )
    h._rebuild_system_prompt()
    sp = h.system_prompt
    assert "MAIN-SPECIFIC OVERRIDE" in sp
    assert "orchestrator" in sp.lower()
    assert "cpu-sensor" in sp
    assert "PREFERENCES & IDENTITY" in sp and "Bob" in sp
    assert "DEVICES & SETUP" in sp
    assert "STANDING POLICIES" in sp


# ── Fact extraction ──────────────────────────────────────────────────────────


def test_extract_facts_saves_and_normalizes():
    llm = FakeLLM('{"user_name": "Bob", "pref_timezone": "Europe/Athens", "ha_url": "http://ha"}')
    h = host([], llm=llm)
    run(h._extract_and_save_facts("I'm Bob in Athens", "Hi Bob"))
    facts = h.get_user_facts()
    assert facts.get("pref_user_name") == "Bob"
    assert facts.get("pref_timezone") == "Europe/Athens"
    assert facts.get("device_ha_url") == "http://ha"
    assert h.total_input_tokens == 10 and h.total_output_tokens == 5
    assert h.persisted_cost == 1


def test_extract_facts_skips_voice_transcripts():
    llm = FakeLLM('{"pref_user_name": "Adé"}')
    h = host([], llm=llm)
    h._current_interface_is_voice = lambda: True

    run(h._extract_and_save_facts("Adé, Amishu", "Hello"))

    assert h.get_user_facts() == {}
    assert llm.calls == []


def test_explicit_voice_memory_request_is_allowed():
    llm = FakeLLM('{"pref_user_name": "Amalia"}')
    h = host([], llm=llm)
    h._current_interface_is_voice = lambda: True

    run(h._extract_and_save_facts("Remember that my name is Amalia", "Okay"))

    assert h.get_user_facts()["pref_user_name"] == "Amalia"
    assert len(llm.calls) == 1


def test_extract_facts_no_llm():
    h = host(llm=None)
    run(h._extract_and_save_facts("hello", "hi"))  # must not raise
    assert h.get_user_facts() == {}


def test_extract_facts_empty_result():
    h = host([], llm=FakeLLM("{}"))
    run(h._extract_and_save_facts("nothing durable", "ok"))
    assert h.get_user_facts() == {}


def test_extract_facts_bad_json():
    h = host([], llm=FakeLLM("not json at all"))
    run(h._extract_and_save_facts("x", "y"))  # must not raise
    assert h.get_user_facts() == {}
