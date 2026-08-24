"""Focused regressions for generic one-off actuator routing."""

import tempfile
import types
from unittest.mock import AsyncMock, patch

from wactorz.agents.home_assistant_actuator_agent import ActuatorAction
from wactorz.agents.one_off_actuator_agent import OneOffActuatorAgent


class _FakeLLM:
    def __init__(self, response: str = "[]") -> None:
        self.response = response

    async def complete(self, messages, system="", **kwargs):
        return self.response, {}


def _agent(request: str, *, history: list[dict] | None = None) -> OneOffActuatorAgent:
    return OneOffActuatorAgent(
        request=request,
        conversation_context=history,
        llm_provider=_FakeLLM(),  # pyright: ignore[reportArgumentType]
        task_id="actuator-test",
        reply_to_id="main-actor",
        persistence_dir=tempfile.gettempdir(),
    )


def _two_color_lights() -> list[dict]:
    return [
        {
            "entity_id": "light.led_strip",
            "name": "Accent LED Strip",
            "state": {"attributes": {"supported_color_modes": ["rgb"]}},
        },
        {
            "entity_id": "light.main",
            "name": "Main Ceiling Light",
            "state": {"attributes": {"supported_color_modes": ["rgb"]}},
        },
    ]


def _both_on() -> list[ActuatorAction]:
    return [
        ActuatorAction(
            domain="light", service="turn_on", entity_id="light.led_strip", service_data={}
        ),
        ActuatorAction(domain="light", service="turn_on", entity_id="light.main", service_data={}),
    ]


def test_generic_color_request_collapses_to_preferred_light():
    agent = _agent("turn the light pink")

    repaired = agent._repair_color_actions(_both_on(), _two_color_lights())

    assert len(repaired) == 1
    assert repaired[0].entity_id == "light.main"
    assert "rgb_color" in repaired[0].service_data


def test_explicit_plural_request_keeps_every_light():
    agent = _agent("turn all the lights pink")

    repaired = agent._repair_color_actions(_both_on(), _two_color_lights())

    assert {action.entity_id for action in repaired} == {"light.led_strip", "light.main"}


def test_direct_color_and_brightness_request_does_not_need_llm_json():
    agent = _agent("make the light bright cyan blue")

    actions = agent._resolve_simple_light_actions(_two_color_lights())

    assert len(actions) == 1
    assert actions[0].entity_id == "light.main"
    assert actions[0].service_data == {"rgb_color": [0, 255, 255], "brightness_pct": 100}


def test_brightness_follow_up_uses_recent_controlled_light():
    agent = _agent(
        "turn down the brightness a little bit",
        history=[{"response": "Done: light.turn_on -> light.led_strip."}],
    )

    actions = agent._resolve_simple_light_actions(_two_color_lights())

    assert len(actions) == 1
    assert actions[0].entity_id == "light.led_strip"
    assert actions[0].service_data == {"brightness_step_pct": -15}


async def test_unparseable_resolver_output_becomes_safe_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        agent = OneOffActuatorAgent(
            request="turn on the mystery light",
            llm_provider=_FakeLLM("not valid JSON"),  # pyright: ignore[reportArgumentType]
            task_id="actuator-test",
            reply_to_id="main-actor",
            persistence_dir=tmpdir,
        )
        with (
            patch(
                "wactorz.agents.one_off_actuator_agent.CONFIG",
                types.SimpleNamespace(ha_url="http://ha.local:8123", ha_token="token"),
            ),
            patch(
                "wactorz.agents.one_off_actuator_agent.fetch_devices_entities_with_location",
                AsyncMock(return_value=[]),
            ),
        ):
            result = await agent._execute_request()

    assert result == "I couldn't identify a matching device for that request."
