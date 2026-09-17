"""The context routing gives the classifier and the actuator.

A follow-up like "and the other one" means nothing on its own, so the classifier
is shown the last exchanges from the same interface, marked as context and not
as something to act on. The actuator is given the Home Assistant entity list, so
"the lamp" can be matched to a real entity id; an HA agent that is slow or
answers nothing costs the enrichment, not the request.
"""

import asyncio
import types
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.main import routing
from wactorz.agents.main.routing import RoutingMixin
from wactorz.core.actor import MessageType


class _Llm:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def complete(self, messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
        self.messages = messages
        return "ACTUATE", {}


class _Actor:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"id-{name}"


class _Registry:
    def __init__(self, *names: str) -> None:
        self._actors = {n: _Actor(n) for n in names}

    def find_by_name(self, name: str) -> _Actor | None:
        return self._actors.get(name)


class _Host(RoutingMixin):
    def __init__(self, registry: _Registry | None = None) -> None:
        self.name = "main"
        self.actor_id = "main-id"
        self.fake_llm = _Llm()
        self.llm = self.fake_llm  # pyright: ignore[reportAttributeAccessIssue]
        self._registry = registry  # pyright: ignore[reportAttributeAccessIssue]
        self._result_futures: dict[str, asyncio.Future[Any]] = {}
        # Only its parent is read, as the directory the actuator persists under.
        self._persistence_dir = Path("state") / "main"
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.history: list[dict[str, Any]] = []
        self.ha_reply: Any = None
        self.actuator_reply: Any = {"result": "Turned on the lamp."}
        self.sent: list[tuple[str, MessageType, dict[str, Any]]] = []
        self.spawned: list[dict[str, Any]] = []

    def _persist_cost(self) -> None:
        return None

    def _current_interface_history(self) -> list[dict[str, Any]]:
        return self.history

    async def send(self, target: str, msg_type: MessageType, payload: dict[str, Any]) -> bool:
        self.sent.append((target, msg_type, payload))
        if isinstance(self.ha_reply, Exception):
            raise self.ha_reply
        if self.ha_reply is not None:
            self._result_futures[payload["_task_id"]].set_result(self.ha_reply)
        return True

    async def spawn(self, actor_class: type, **kwargs: Any) -> Any:
        self.spawned.append(kwargs)
        if self.actuator_reply is not None:
            self._result_futures[kwargs["task_id"]].set_result(self.actuator_reply)
        return object()


def _host(registry: _Registry | None = None) -> _Host:
    # Partial on purpose: the members routing reaches, not the whole MainActor.
    return _Host(registry)  # pyright: ignore[reportAbstractUsage]


@pytest.fixture(autouse=True)
def _ha_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routing, "CONFIG", types.SimpleNamespace(ha_url="http://ha", ha_token="t"))


class TestClassifierContext:
    async def test_the_last_two_exchanges_are_shown_as_context_only(self) -> None:
        host = _host()
        host.history = [
            {"transcript": "oldest", "response": "ignored"},
            {"transcript": "turn on the kitchen light", "response": "Done."},
            {"transcript": "", "response": "Anything else?"},
        ]

        assert await host._classify_intent("and the other one") == "ACTUATE"

        text = host.fake_llm.messages[0]["content"]
        assert text.startswith("Recent context (classification only; do not execute):")
        assert "Previous user: turn on the kitchen light" in text
        assert "Previous assistant: Anything else?" in text
        assert "oldest" not in text
        assert text.endswith("Current request (classify this only): and the other one")


class TestActuatorEnrichment:
    async def test_matching_entities_are_added_to_the_request(self) -> None:
        host = _host(_Registry("home-assistant-agent"))
        host.ha_reply = {
            "entities": [
                {"entity_id": "light.lamp", "name": "Desk lamp"},
                {"entity_id": "switch.fan", "friendly_name": "switch.fan"},
                {"name": "no id"},
            ]
        }
        host.history = [{"transcript": "hi"}]

        reply = await host._handle_actuate_intent("turn on the lamp")

        assert reply == "Turned on the lamp."
        ((target, msg_type, payload),) = host.sent
        assert (target, msg_type, payload["text"]) == (
            "id-home-assistant-agent",
            MessageType.TASK,
            "list_entities",
        )
        request = host.spawned[0]["request"]
        assert request.startswith("turn on the lamp\n\n[AVAILABLE HA ENTITIES")
        assert "  light.lamp (Desk lamp)" in request
        assert "  switch.fan\n" in request
        assert host.spawned[0]["conversation_context"] == [{"transcript": "hi"}]
        assert host._result_futures == {}

    @pytest.mark.parametrize("ha_reply", [{"entities": []}, {"result": "no list"}, "not a dict"])
    async def test_an_answer_without_entities_leaves_the_request_alone(self, ha_reply: Any) -> None:
        host = _host(_Registry("home-assistant-agent"))
        host.ha_reply = ha_reply

        await host._handle_actuate_intent("turn on the lamp")

        assert host.spawned[0]["request"] == "turn on the lamp"

    async def test_a_failing_ha_agent_costs_only_the_enrichment(self) -> None:
        host = _host(_Registry("home-assistant-agent"))
        host.ha_reply = RuntimeError("mailbox full")

        assert await host._handle_actuate_intent("turn on the lamp") == "Turned on the lamp."
        assert host.spawned[0]["request"] == "turn on the lamp"

    async def test_a_silent_ha_agent_times_out_and_the_request_goes_ahead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = _host(_Registry("home-assistant-agent"))
        real_wait_for = asyncio.wait_for

        async def _short(awaitable: Any, timeout: float | None) -> Any:
            return await real_wait_for(awaitable, timeout=0.01 if timeout == 10.0 else timeout)

        monkeypatch.setattr(routing.asyncio, "wait_for", _short)

        assert await host._handle_actuate_intent("turn on the lamp") == "Turned on the lamp."
        assert host._result_futures == {}

    async def test_an_actuator_that_never_answers_times_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host = _host()
        host.actuator_reply = None
        real_wait_for = asyncio.wait_for

        async def _short(awaitable: Any, timeout: float | None) -> Any:
            return await real_wait_for(awaitable, timeout=0.01)

        monkeypatch.setattr(routing.asyncio, "wait_for", _short)

        assert await host._handle_actuate_intent("turn on the lamp") == (
            "Actuation timed out, please retry."
        )
        assert host._result_futures == {}

    async def test_an_answer_without_a_result_is_done(self) -> None:
        host = _host()
        host.actuator_reply = {"ok": True}

        assert await host._handle_actuate_intent("turn on the lamp") == "Done."
