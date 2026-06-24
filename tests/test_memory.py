"""
Runnable tests for wactorz.agents.memory.MemoryMixin.

Self-contained: a fake host supplies the Actor/LLMAgent surface the mixin needs
(recall/persist, _registry, llm.complete, token counters). Run with:
    python tests/test_memory.py
"""

import asyncio
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wactorz.agents.memory import MemoryMixin


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
    """Returns a preset raw string from complete(); records calls."""
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


RESULTS = []
def check(cond, msg):
    if not cond:
        raise AssertionError(msg)

async def run(label, coro):
    try:
        await coro
        RESULTS.append((label, True, ""))
        print(f"  PASS  {label}")
    except Exception as e:
        RESULTS.append((label, False, f"{e}\n{traceback.format_exc()}"))
        print(f"  FAIL  {label}: {e}")


# ── Tests ───────────────────────────────────────────────────────────────────

async def t_get_user_facts_empty():
    host = MemoryHost()
    check(host.get_user_facts() == {}, "no facts → empty dict")

async def t_get_user_facts_stored():
    host = MemoryHost()
    host.persist("_user_facts", {"pref_user_name": "Bob"})
    check(host.get_user_facts() == {"pref_user_name": "Bob"}, "returns stored facts")

async def t_preferred_timezone():
    host = MemoryHost()
    host.persist("_user_facts", {"pref_timezone": "Europe/Athens"})
    check(host._preferred_timezone_name() == "Europe/Athens", "reads pref_timezone")

async def t_running_agents_summary_filters():
    reg = FakeRegistry([
        FakeActor("main"), FakeActor("monitor"), FakeActor("installer"),
        FakeActor("planner-abc123"),
        FakeActor("cpu-sensor", "Reports CPU temperature"),
    ])
    host = MemoryHost(registry=reg)
    summary = host._get_running_agents_summary()
    check("cpu-sensor" in summary, "real agent listed")
    check("Reports CPU temperature" in summary, "description included")
    for hidden in ("main", "monitor", "installer", "planner-abc123"):
        check(hidden not in summary, f"{hidden} should be filtered out")

async def t_running_agents_summary_empty():
    host = MemoryHost(registry=FakeRegistry([FakeActor("main")]))
    check(host._get_running_agents_summary() == "", "only system agents → empty")

async def t_prefix_with_agents():
    reg = FakeRegistry([FakeActor("alpha"), FakeActor("beta")])
    host = MemoryHost(registry=reg)
    out = host._prefix_with_live_context("what is running?")
    check("[CURRENT SYSTEM STATE" in out and "[END SYSTEM STATE]" in out, "delimiters present")
    check("alpha, beta" in out, "sorted live names injected")
    check(out.endswith("what is running?"), "original text preserved at end")

async def t_prefix_without_agents():
    host = MemoryHost(registry=FakeRegistry([]))
    out = host._prefix_with_live_context("hi")
    check("NONE" in out, "empty state says NONE")
    check(out.endswith("hi"), "original text preserved")

async def t_rebuild_system_prompt():
    reg = FakeRegistry([FakeActor("cpu-sensor", "CPU temps")])
    host = MemoryHost(registry=reg)
    host.persist("_user_facts", {"pref_user_name": "Bob", "device_ha_url": "http://x", "policy_quiet": "23-07"})
    host._rebuild_system_prompt()
    sp = host.system_prompt
    check("MAIN-SPECIFIC OVERRIDE" in sp, "override block present")
    check("orchestrator" in sp.lower(), "ORCHESTRATOR_PROMPT included")
    check("cpu-sensor" in sp, "running agent listed in prompt")
    check("PREFERENCES & IDENTITY" in sp and "Bob" in sp, "pref bucket rendered")
    check("DEVICES & SETUP" in sp, "device bucket rendered")
    check("STANDING POLICIES" in sp, "policy bucket rendered")

async def t_extract_facts_saves_and_normalizes():
    # LLM returns some unprefixed keys → mixin should namespace them.
    llm = FakeLLM('{"user_name": "Bob", "pref_timezone": "Europe/Athens", "ha_url": "http://ha"}')
    host = MemoryHost(registry=FakeRegistry([]), llm=llm)
    await host._extract_and_save_facts("I'm Bob in Athens", "Hi Bob")
    facts = host.get_user_facts()
    check(facts.get("pref_user_name") == "Bob", f"user_name → pref_user_name, got {facts}")
    check(facts.get("pref_timezone") == "Europe/Athens", "prefixed key kept")
    check(facts.get("device_ha_url") == "http://ha", "_url → device_ bucket")
    check(host.total_input_tokens == 10 and host.total_output_tokens == 5, "token usage accumulated")
    check(host.persisted_cost == 1, "_persist_cost called")

async def t_extract_facts_no_llm():
    host = MemoryHost(llm=None)
    await host._extract_and_save_facts("hello", "hi")  # must not raise
    check(host.get_user_facts() == {}, "no llm → nothing saved")

async def t_extract_facts_empty_result():
    host = MemoryHost(registry=FakeRegistry([]), llm=FakeLLM("{}"))
    await host._extract_and_save_facts("nothing durable", "ok")
    check(host.get_user_facts() == {}, "empty json → nothing saved")

async def t_extract_facts_bad_json():
    host = MemoryHost(registry=FakeRegistry([]), llm=FakeLLM("not json at all"))
    await host._extract_and_save_facts("x", "y")  # must not raise
    check(host.get_user_facts() == {}, "bad json → nothing saved, no crash")


async def main():
    tests = [
        ("get_user_facts: empty", t_get_user_facts_empty()),
        ("get_user_facts: stored", t_get_user_facts_stored()),
        ("preferred timezone", t_preferred_timezone()),
        ("running-agents summary filters", t_running_agents_summary_filters()),
        ("running-agents summary empty", t_running_agents_summary_empty()),
        ("prefix with agents", t_prefix_with_agents()),
        ("prefix without agents", t_prefix_without_agents()),
        ("rebuild system prompt", t_rebuild_system_prompt()),
        ("extract facts: save + normalize", t_extract_facts_saves_and_normalizes()),
        ("extract facts: no llm", t_extract_facts_no_llm()),
        ("extract facts: empty result", t_extract_facts_empty_result()),
        ("extract facts: bad json", t_extract_facts_bad_json()),
    ]
    print("Running MemoryMixin tests\n")
    for label, coro in tests:
        await run(label, coro)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{passed}/{len(RESULTS)} passed.")
    if failed:
        print("\nFailures:")
        for label, _, detail in failed:
            print(f"\n### {label}\n{detail}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
