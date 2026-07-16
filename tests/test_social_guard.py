"""Tests for ``wactorz.interfaces.social_guard.SocialCommandGuard``.

The guard is the safety boundary for Discord/Telegram: it must run a small set
of whitelisted commands and refuse everything else — crucially, it must never
reach ``process_user_input`` (spawn/delete/code). These tests drive it against a
lightweight fake MainActor; async handlers run through ``asyncio.run``.
"""

import asyncio

from wactorz.interfaces.social_guard import SocialCommandGuard


def run(coro):
    return asyncio.run(coro)


# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeActor:
    def __init__(self, name, state="running"):
        self.name = name
        self.state = type("S", (), {"value": state})()


class _FakeRegistry:
    def __init__(self, actors):
        self._actors = actors

    def all_actors(self):
        return list(self._actors)


class FakeMain:
    def __init__(self, rules=None, actors=None, summary=""):
        self._rules = rules or {}
        self._registry = _FakeRegistry(actors or [])
        self._summary = summary
        self.process_user_input_called = False
        self.run_pipeline_calls = []

    # Safe surface the guard is allowed to touch
    def get_pipeline_rules(self):
        return self._rules

    def recall(self, key, default=None):
        if key == "history_summary":
            return self._summary
        return default

    async def run_pipeline(self, goal, agents, **kw):
        self.run_pipeline_calls.append({"goal": goal, "agents": agents})
        return {"result": f"ran '{goal}' with {agents}"}

    # The forbidden path — the guard must never call this.
    async def process_user_input(self, text):  # pragma: no cover - must not run
        self.process_user_input_called = True
        return "ORCHESTRATOR REACHED"


def guard(**kw):
    return SocialCommandGuard(FakeMain(**kw))


# ── Whitelisted commands ─────────────────────────────────────────────────────


def test_help_lists_commands_and_is_default_for_empty():
    g = guard()
    for text in ("", "help", "/help"):
        out = run(g.handle(text))
        assert "status" in out and "workflows" in out
        assert "disabled here" in out


def test_status_lists_running_agents():
    g = SocialCommandGuard(FakeMain(actors=[_FakeActor("main"), _FakeActor("weather-agent")]))
    out = run(g.handle("status"))
    assert "2 agent(s)" in out
    assert "weather-agent: running" in out


def test_summarize_returns_rolling_summary():
    g = SocialCommandGuard(FakeMain(summary="did some things"))
    assert "did some things" in run(g.handle("summarize"))
    assert "No recent activity" in run(SocialCommandGuard(FakeMain()).handle("summarize"))


def test_workflows_lists_saved_rules():
    rules = {"rule-1": {"task": "nightly report", "agents": ["reporter"]}}
    out = run(SocialCommandGuard(FakeMain(rules=rules)).handle("workflows"))
    assert "rule-1" in out and "nightly report" in out


def test_run_executes_existing_workflow_only():
    rules = {"rule-1": {"task": "nightly report", "agents": ["reporter"]}}
    main = FakeMain(rules=rules)
    out = run(SocialCommandGuard(main).handle("run rule-1"))
    assert main.run_pipeline_calls == [{"goal": "nightly report", "agents": ["reporter"]}]
    assert "ran 'nightly report'" in out


def test_run_unknown_workflow_is_refused_not_executed():
    main = FakeMain(rules={"rule-1": {"task": "x", "agents": []}})
    out = run(SocialCommandGuard(main).handle("run rm -rf /"))
    assert main.run_pipeline_calls == []
    assert "No saved workflow" in out


# ── Deny-by-default: the important part ──────────────────────────────────────


def test_spawn_delete_and_code_are_refused():
    main = FakeMain()
    g = SocialCommandGuard(main)
    for text in (
        "spawn a cpu monitor agent",
        "delete weather-agent",
        "run python; import os; os.system('x')",
        "please execute this code",
        "what's the weather in Athens tomorrow?",  # free-form NL
    ):
        out = run(g.handle(text))
        assert "isn't allowed" in out or "No saved workflow" in out
    # The orchestrator is never reached for any of them.
    assert main.process_user_input_called is False
