"""Security tests for ``MainActor.process_user_input_restricted`` — the entry
point social channels (Discord/Telegram) use.

Policy B: full conversation + device control (ACTUATE) + HA queries are allowed,
but spawning, deleting, code, pipelines, admin commands, and delegation to the
create/install/spawn agents must be unreachable. These tests drive the method
against a stubbed MainActor and assert every dangerous path is blocked — the
spawn/delete executors are wired to explode if ever called.
"""

import asyncio
from types import SimpleNamespace

import pytest

from wactorz.agents.main_actor import MainActor


def run(coro):
    return asyncio.run(coro)


class _Registry:
    def __init__(self, agents=()):
        self._by = {n: SimpleNamespace(name=n) for n in agents}

    def find_by_name(self, name):
        return self._by.get(name)


def make_main(*, intent="OTHER", chat_response="hello", agents=()):
    """A MainActor with only the surface process_user_input_restricted touches,
    stubbed. Spawn/delete executors raise if reached."""
    m = MainActor.__new__(MainActor)
    m.name = "main"
    m._conversation_history = []
    m._registry = _Registry(agents)
    m.calls = {"actuate": 0, "chat": 0, "delegated": [], "classified": 0}

    m._drain_notifications = lambda: ""
    m._rebuild_system_prompt = lambda: None
    m._prefix_with_live_context = lambda t: t
    m.persist = lambda k, v: None

    async def _record(_t, _r):
        return None

    m._record_external_exchange = _record

    async def _classify(_t):
        m.calls["classified"] += 1
        return intent

    m._classify_intent = _classify

    async def _actuate(_t, allowed_domains=None):
        m.calls["actuate"] += 1
        m.calls["actuate_domains"] = allowed_domains
        return "actuated the device"

    m._handle_actuate_intent = _actuate

    async def _delegate_task(_name, _text, timeout=0):
        return {"result": "home-assistant says ok"}

    m.delegate_task = _delegate_task

    async def _chat(_t):
        m.calls["chat"] += 1
        return chat_response

    m.chat = _chat

    async def _run_delegation(name, _payload):
        m.calls["delegated"].append(name)
        return f"[{name} handled it]"

    m._run_delegation = _run_delegation

    async def _boom_spawn(_resp):
        raise AssertionError("spawn executor reached on a restricted channel!")

    async def _boom_delete(_resp):
        raise AssertionError("delete executor reached on a restricted channel!")

    m._process_spawn_commands = _boom_spawn
    m._process_delete_commands = _boom_delete
    return m


# ── Pure helper ──────────────────────────────────────────────────────────────


def test_neutralize_strips_spawn_and_delete():
    text = 'talk <spawn>{"name":"x","code":"..."}</spawn> more <delete>{"name":"y"}</delete> end'
    clean, had = MainActor._neutralize_action_blocks(text)
    assert had is True
    assert "<spawn>" not in clean and "<delete>" not in clean
    assert "talk" in clean and "end" in clean


def test_neutralize_leaves_plain_text():
    clean, had = MainActor._neutralize_action_blocks("just a normal answer")
    assert had is False
    assert clean == "just a normal answer"


# ── Admin commands & pipeline intent are refused ─────────────────────────────


@pytest.mark.parametrize("cmd", ["/delete weather-agent", "/deploy node1", "pipeline! do x"])
def test_admin_commands_refused_without_reaching_llm(cmd):
    m = make_main()
    out = run(m.process_user_input_restricted(cmd))
    assert "aren't available" in out
    assert m.calls["chat"] == 0 and m.calls["classified"] == 0  # never reached routing


def test_pipeline_intent_refused():
    m = make_main(intent="PIPELINE")
    out = run(m.process_user_input_restricted("every morning email me the news"))
    assert "can't create automations" in out
    assert m.calls["chat"] == 0  # planner/spawn path never entered


# ── Spawn/delete emitted in a chat reply are neutralized, not executed ───────


def test_spawn_block_in_reply_is_not_executed():
    # The LLM's prose falsely confirms a spawn; the whole reply must be discarded
    # in favor of an honest refusal, and nothing spawned.
    m = make_main(
        chat_response='I\'ve spawned poet-agent! <spawn>{"name":"poet","code":"x"}</spawn>'
    )
    out = run(m.process_user_input_restricted("make me a poem agent"))
    # _process_spawn_commands would raise if reached — reaching here proves it didn't.
    assert "<spawn>" not in out
    assert "spawned poet-agent" not in out  # the lie is gone
    assert "dashboard-only" in out


def test_delete_block_in_reply_is_not_executed():
    m = make_main(chat_response='Done, deleted it. <delete>{"name":"weather-agent"}</delete>')
    out = run(m.process_user_input_restricted("delete the weather agent"))
    assert "<delete>" not in out
    assert "deleted it" not in out
    assert "dashboard-only" in out


# ── Delegation laundering is blocked; safe delegation still works ────────────


@pytest.mark.parametrize("bad", ["catalog", "installer", "planner"])
def test_delegation_to_create_agents_denied(bad):
    m = make_main(chat_response=f'<delegate>{{"agent":"{bad}","task":"spawn a bot"}}</delegate>')
    out = run(m.process_user_input_restricted("spawn a bot via the catalog"))
    assert f"{bad} isn't available" in out
    assert m.calls["delegated"] == []  # never dispatched


def test_delegation_to_safe_running_agent_works():
    m = make_main(
        chat_response='<delegate>{"agent":"weather-agent","task":"weather in Athens"}</delegate>',
        agents=("weather-agent",),
    )
    out = run(m.process_user_input_restricted("what's the weather in Athens?"))
    assert m.calls["delegated"] == ["weather-agent"]
    assert "handled it" in out


# ── Allowed paths: actuation, HA, plain chat ─────────────────────────────────


def test_actuate_intent_allowed():
    from wactorz.agents.one_off_actuator_agent import SOCIAL_ACTUATE_DOMAINS

    m = make_main(intent="ACTUATE")
    out = run(m.process_user_input_restricted("turn off the living room lamp"))
    assert m.calls["actuate"] == 1
    assert "actuated" in out
    # Device control is allowed, but only over everyday domains.
    assert m.calls["actuate_domains"] == SOCIAL_ACTUATE_DOMAINS


def test_ha_intent_allowed():
    m = make_main(intent="HA")
    out = run(m.process_user_input_restricted("is the garage door closed?"))
    assert "home-assistant says ok" in out


def test_plain_conversation_allowed():
    m = make_main(chat_response="I'm doing great, thanks for asking!")
    out = run(m.process_user_input_restricted("hey how are you"))
    assert out == "I'm doing great, thanks for asking!"
    assert m.calls["chat"] == 1
