"""The actuator's domain gate — the layer under ``process_user_input_restricted``.

The restricted entry point refuses spawn/delete/code, but it *allows* device
control, and device control is executed by OneOffActuatorAgent as whatever
``domain``/``service`` the LLM resolved. Home Assistant's ``shell_command`` and
``python_script`` domains run arbitrary code on the HA host, and ``hassio`` can
stop the stack — so without a gate here, "turn on my lamp" and "run my backup
shell script" are the same code path.

These tests drive ``_execute_actions`` directly with a jailbroken resolver's
output, so they cover the gate itself rather than the intent routing above it.
"""

import asyncio
from typing import ClassVar

import pytest

from wactorz.agents.home_assistant_actuator_agent import ActuatorAction
from wactorz.agents.one_off_actuator_agent import SOCIAL_ACTUATE_DOMAINS, OneOffActuatorAgent


def run(coro):
    return asyncio.run(coro)


class _RecordingHA:
    """Stands in for HAWebSocketClient — records what would have been called."""

    calls: ClassVar[list] = []

    def __init__(self, *_a, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def call_service(self, domain, service, entity_id, **service_data):
        _RecordingHA.calls.append((domain, service, entity_id, service_data))


@pytest.fixture(autouse=True)
def _patch_ha(monkeypatch):
    _RecordingHA.calls = []
    monkeypatch.setattr(
        "wactorz.agents.one_off_actuator_agent.HAWebSocketClient",
        _RecordingHA,
    )
    monkeypatch.setattr(
        "wactorz.agents.one_off_actuator_agent.normalize_ha_ws_url",
        lambda _url: "ws://ha.test/api/websocket",
    )


def make_actuator(allowed_domains):
    a = OneOffActuatorAgent.__new__(OneOffActuatorAgent)
    a.name = "one-off-actuator-test"
    a.allowed_domains = frozenset(allowed_domains) if allowed_domains is not None else None
    return a


def action(domain, service, entity_id=None, **data):
    return ActuatorAction(
        domain=domain,
        service=service,
        entity_id=entity_id or f"{domain}.thing",
        service_data=data or {},
    )


# ── The gate ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "domain,service",
    [
        ("shell_command", "run_backup"),  # arbitrary shell on the HA host
        ("python_script", "exec"),  # arbitrary Python on the HA host
        ("hassio", "addon_restart"),  # restart/stop the add-on stack
        ("homeassistant", "stop"),  # stop Home Assistant itself
        ("script", "anything_the_user_wired_up"),
        ("automation", "trigger"),
        ("notify", "persistent_notification"),
    ],
)
def test_dangerous_domains_never_reach_call_service(domain, service):
    a = make_actuator(SOCIAL_ACTUATE_DOMAINS)
    out = run(a._execute_actions([action(domain, service)]))

    assert _RecordingHA.calls == []
    assert "can't control" in out
    assert domain in out


def test_everyday_domains_still_execute():
    a = make_actuator(SOCIAL_ACTUATE_DOMAINS)
    out = run(a._execute_actions([action("light", "turn_on", "light.lamp", brightness_pct=40)]))

    assert _RecordingHA.calls == [("light", "turn_on", "light.lamp", {"brightness_pct": 40})]
    assert "Done:" in out


def test_mixed_batch_executes_allowed_and_reports_blocked():
    """A resolver that smuggles one bad call alongside a good one must not get
    the bad one through, and the reply must not imply it ran."""
    a = make_actuator(SOCIAL_ACTUATE_DOMAINS)
    out = run(
        a._execute_actions(
            [
                action("light", "turn_on", "light.lamp"),
                action("shell_command", "exfiltrate"),
            ]
        )
    )

    assert _RecordingHA.calls == [("light", "turn_on", "light.lamp", {})]
    assert "Done:" in out
    assert "Skipped" in out and "shell_command.exfiltrate" in out


def test_domain_match_is_case_insensitive():
    a = make_actuator(SOCIAL_ACTUATE_DOMAINS)
    run(a._execute_actions([action("LIGHT", "turn_on", "light.lamp")]))
    assert _RecordingHA.calls == [("LIGHT", "turn_on", "light.lamp", {})]


def test_uppercase_dangerous_domain_is_still_blocked():
    a = make_actuator(SOCIAL_ACTUATE_DOMAINS)
    run(a._execute_actions([action("Shell_Command", "run")]))
    assert _RecordingHA.calls == []


def test_trusted_caller_is_unrestricted():
    """The dashboard and CLI pass None and keep full Home Assistant access."""
    a = make_actuator(None)
    run(a._execute_actions([action("shell_command", "run_backup")]))
    assert _RecordingHA.calls == [("shell_command", "run_backup", "shell_command.thing", {})]


def test_allow_list_excludes_the_code_execution_domains():
    for domain in ("shell_command", "python_script", "hassio", "homeassistant", "script"):
        assert domain not in SOCIAL_ACTUATE_DOMAINS
