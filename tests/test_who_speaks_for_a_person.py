"""Which agents may supply interface conversation context to main.

Main admits this context only from reviewed local catalogue programs. Admission
allows restricted conversation, never operator commands: neither the proxy's
trust nor caller-controlled flags authenticate the person who originated it.
"""

import asyncio
import types
from typing import Any
from unittest.mock import AsyncMock

import pytest

from wactorz.agents.dynamic import DynamicAgent
from wactorz.agents.main.actor import MainActor
from wactorz.core.actor import Message, MessageType

#: How long a test waits for main's reply. Generous: it only bounds a failure.
REPLIED_WITHIN_S = 5.0


class _Main:
    """A MainActor whose turns and replies are recorded rather than run."""

    def __init__(self, sender: Any) -> None:
        self.actor = MainActor(llm_provider=None)
        self.turns: list[str] = []
        self.replies: list[dict[str, Any]] = []
        self.replied = asyncio.Event()

        async def _turn(text: str) -> str:
            self.turns.append(text)
            return "done"

        async def _send(_target: str, _kind: MessageType, payload: dict[str, Any]) -> None:
            self.replies.append(payload)
            self.replied.set()

        self.actor.process_user_input_restricted = AsyncMock(side_effect=_turn)
        setattr(self.actor, "send", _send)
        setattr(
            self.actor,
            "_registry",
            types.SimpleNamespace(
                get=lambda actor_id: sender if actor_id == "sender-id" else None,
                all_actors=lambda: [sender],
            ),
        )

    async def told(self, text: str, sender_id: str = "sender-id") -> dict[str, Any]:
        msg = Message(
            type=MessageType.TASK,
            sender_id=sender_id,
            payload={
                "text": text,
                "_via_interface": True,
                "_interface_source": "someone",
                "_task_id": "t1",
                "_reply_to": sender_id,
            },
        )
        await self.actor._handle_task(msg)
        await asyncio.wait_for(self.replied.wait(), timeout=REPLIED_WITHIN_S)
        return self.replies[-1]


def _dynamic(*, trusted: bool) -> DynamicAgent:
    return DynamicAgent(code="", name="speaker", trusted=trusted)


class TestAnInterface:
    async def test_a_catalogue_agent_speaks_for_its_person(self) -> None:
        main = _Main(_dynamic(trusted=True))

        reply = await main.told("what is the weather?")

        assert main.turns == ["what is the weather?"]
        assert reply["result"] == "done"


class TestAnyoneElse:
    @pytest.mark.parametrize(
        "command", ["/nodes run rpi sudo reboot", "/packages allow requests-evil", "/deploy rpi"]
    )
    async def test_an_agent_the_model_wrote_types_no_command(self, command: str) -> None:
        main = _Main(_dynamic(trusted=False))

        reply = await main.told(command)

        assert main.turns == []
        assert "only an interface" in reply["error"]
        assert reply["_task_id"] == "t1"

    async def test_nor_a_catalogue_agent_running_a_repair(self) -> None:
        # A repair is the model's code, and drops the catalogue's trust.
        speaker = _dynamic(trusted=True)
        speaker._trusted = False
        main = _Main(speaker)

        await main.told("/nodes run rpi uptime")

        assert main.turns == []

    async def test_nor_an_agent_configured_by_the_model(self) -> None:
        # A rule's task action sends the body its config spells out.
        main = _Main(types.SimpleNamespace(name="rule"))

        await main.told("/packages allow requests-evil")

        assert main.turns == []

    async def test_nor_a_sender_main_does_not_know(self) -> None:
        main = _Main(_dynamic(trusted=True))

        await main.told("/nodes run rpi uptime", sender_id="nobody")

        assert main.turns == []
