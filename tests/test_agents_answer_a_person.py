"""A reply a person can read, from the agents that do not converse.

`web/chat.py` turns an agent's reply into chat text by looking for `reply`,
`message`, `text`, `content` or `result`, and printing `str(payload)` when it
finds none. Three Home Assistant agents and a dynamic agent's failure paths sent
status and error dicts carrying none of those, so asking one a question produced
a Python repr of its internal state -- the same repr whatever was asked, because
the question was never read.

These agents genuinely cannot answer questions. Saying so is the fix; the
structured fields stay for the callers that delegate to them and read them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.home_assistant_actuator_agent import (
    ActuatorConfig,
    HomeAssistantActuatorAgent,
)
from wactorz.agents.home_assistant_map_agent import HomeAssistantMapAgent
from wactorz.agents.home_assistant_state_bridge_agent import HomeAssistantStateBridgeAgent
from wactorz.core.actor import Message, MessageType
from wactorz.web.chat import reply_text

#: The real renderer, not a copy of it. A test that reimplemented the lookup
#: would keep passing after the product stopped agreeing with it, which is the
#: failure this file exists to catch in the first place.
spoken = reply_text


def ask(agent: Any, question: str = "How warm is the nursery right now?") -> dict[str, Any]:
    """Send one TASK and return the reply the agent sent back."""
    captured: dict[str, Any] = {}

    async def _capture(target: str, msg_type: MessageType, payload: Any = None, **_: Any) -> bool:
        captured["payload"] = payload
        return True

    agent.send = _capture  # type: ignore[method-assign]
    asyncio.run(
        agent.handle_message(
            Message(type=MessageType.TASK, sender_id="someone", payload={"text": question})
        )
    )
    assert "payload" in captured, "the agent answered nothing at all"
    payload = captured["payload"]
    assert isinstance(payload, dict), f"expected a dict reply, got {type(payload).__name__}"
    return payload


@pytest.fixture(name="actuator")
def actuator_fixture() -> HomeAssistantActuatorAgent:
    return HomeAssistantActuatorAgent(
        name="nursery-fan-actuator",
        config=ActuatorConfig(
            automation_id="nursery-fan-actuator",
            description="Turns the fan on when the nursery goes above 26 degrees.",
            mqtt_topics=["custom/triggers/nursery-fan"],
            actions=[],
        ),
    )


class TestTheReplyReachesAPerson:
    def test_the_actuator_answers_in_words(self, actuator: HomeAssistantActuatorAgent) -> None:
        payload = ask(actuator)
        said = spoken(payload)

        assert not said.startswith("{"), (
            f"the reply is being shown as a dict rather than read out: {said[:200]!r}"
        )
        assert "cannot answer questions" in said, (
            f"an agent that cannot answer should say so rather than return its state "
            f"and leave the asker to infer it: {said[:200]!r}"
        )

    def test_the_actuator_keeps_its_structured_fields(
        self, actuator: HomeAssistantActuatorAgent
    ) -> None:
        # Other agents delegate here and read these; the sentence is added
        # alongside them rather than in place of them.
        payload = ask(actuator)
        for field_name in ("automation_id", "mqtt_topics", "actuations_count", "ha_connected"):
            assert field_name in payload, f"{field_name!r} disappeared from the reply"

    def test_the_state_bridge_answers_in_words(self) -> None:
        payload = ask(HomeAssistantStateBridgeAgent(name="home-assistant-state-bridge"))
        said = spoken(payload)

        assert not said.startswith("{"), f"shown as a dict: {said[:200]!r}"
        assert "cannot answer questions" in said, said[:200]

    def test_the_map_agent_answers_in_words(self) -> None:
        payload = ask(HomeAssistantMapAgent(name="home-assistant-map-agent"))
        said = spoken(payload)

        assert not said.startswith("{"), f"shown as a dict: {said[:200]!r}"
        assert "cannot answer questions" in said, said[:200]

    def test_a_status_request_is_answered_as_a_sentence(self) -> None:
        # The one command these two do take. It used to return the status dict
        # raw, which reads no better than the refusal did.
        payload = ask(HomeAssistantStateBridgeAgent(name="bridge"), question="status")
        said = spoken(payload)

        assert not said.startswith("{"), f"status shown as a dict: {said[:200]!r}"
        assert payload.get("events_seen") is not None, "the structured status was dropped"


class TestADynamicAgentThatFails:
    """The three ways `handle_task` does not produce an answer.

    All three used to reply with `error`/`info` dicts and nothing the chat could
    read, so a task that failed reached the person who asked as a repr. They are
    failure text, which is the part of a change most likely to be reworded next
    and the part least likely to be re-checked by hand.
    """

    @staticmethod
    def _agent(tmp_path: Path, handler: Any) -> DynamicAgent:
        agent = DynamicAgent(name="porch-watch", code="", persistence_dir=str(tmp_path))
        agent._fn_handle_task = handler
        return agent

    @staticmethod
    def _invoke(agent: DynamicAgent, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Drive one task through and return what was sent back."""
        captured: dict[str, Any] = {}

        async def _capture(target: str, msg_type: MessageType, body: Any = None, **_: Any) -> bool:
            captured["payload"] = body
            return True

        async def _swallow_publish(**_: Any) -> None:
            """The error topic is a separate concern; this is about the reply."""

        agent.send = _capture  # type: ignore[method-assign]
        agent._publish_error = _swallow_publish  # type: ignore[method-assign]
        msg = Message(type=MessageType.TASK, sender_id="someone", payload=payload or {})
        asyncio.run(agent._invoke_handle_task(msg, None, None, lambda body: body))
        assert "payload" in captured, "the task failed and the asker was told nothing"
        return captured["payload"]

    def test_an_agent_without_a_handler_says_so(self, tmp_path: Path) -> None:
        payload = self._invoke(self._agent(tmp_path, None))
        said = spoken(payload)

        assert not said.startswith("{"), f"shown as a dict: {said[:200]!r}"
        assert "cannot answer questions" in said, said[:200]
        assert payload.get("info"), "the machine-readable reason was dropped"

    def test_a_handler_that_raises_reports_what_went_wrong(self, tmp_path: Path) -> None:
        async def _boom(_api: Any, _payload: Any) -> None:
            raise RuntimeError("the sensor was not there")

        payload = self._invoke(self._agent(tmp_path, _boom))
        said = spoken(payload)

        assert not said.startswith("{"), f"shown as a dict: {said[:200]!r}"
        assert "the sensor was not there" in said, (
            f"the reason is what makes this worth reading, and it is missing: {said[:200]!r}"
        )
        assert payload.get("error_phase") == "handle_task", "the structured error was dropped"

    def test_a_handler_that_never_returns_says_it_gave_up(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path, None)

        async def _hang(_api: Any, _payload: Any) -> None:
            await asyncio.sleep(3600)

        agent._fn_handle_task = _hang
        agent._HANDLE_TASK_TIMEOUT = 0.05  # type: ignore[attr-defined]

        payload = self._invoke(agent)
        said = spoken(payload)

        assert not said.startswith("{"), f"shown as a dict: {said[:200]!r}"
        assert "gave up" in said, said[:200]
        assert "0.05" in said, (
            f"how long it waited is the one fact that makes this actionable: {said[:200]!r}"
        )


class TestBothDeliveryPathsAgree:
    """One reply, one rendering, wherever the agent happens to run.

    The two paths had drifted: an in-process agent was read `reply` first and
    one answering from a node `result` first. Nothing carried two of those
    fields, so nothing looked wrong -- but moving an agent onto a node would
    have silently changed what it appeared to say.
    """

    def test_result_wins_because_that_is_what_agents_are_told_to_fill(self) -> None:
        # `main_actor_prompts` tells a generated agent: for plain text, use
        # {"result": ...}. Every other reader in the tree agrees.
        assert reply_text({"result": "the porch is 4.3", "reply": "hello"}) == "the porch is 4.3"

    @pytest.mark.parametrize("field", ["result", "reply", "text", "message", "content"])
    def test_every_field_an_agent_might_use_is_read(self, field: str) -> None:
        assert reply_text({field: "the answer"}) == "the answer"

    def test_a_reply_with_no_words_is_left_ugly(self) -> None:
        # Deliberate: a repr gets reported, where a tidy rendering would hide an
        # agent that never learned to answer.
        assert reply_text({"actuations_count": 1}).startswith("{")

    def test_a_reply_that_is_not_a_dict_still_renders(self) -> None:
        assert reply_text("just words") == "just words"
