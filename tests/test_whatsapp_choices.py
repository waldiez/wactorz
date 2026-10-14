"""WhatsApp carries an agent's question out, numbered, and the number back.

Twilio offers no buttons, so the options go as a numbered list under the
text, and a reply that is one of the numbers, or an option's words, is the
answer: it is handed to the outbox for the agent and never reaches the model.
The next message from that person is a message again.

The webhook only takes what Twilio signed, so each inbound reply here is
signed as Twilio would; the outbound send is captured rather than dialled.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast

import pytest
from aiohttp.test_utils import TestClient, TestServer

RequestValidator = pytest.importorskip(
    "twilio.request_validator", reason="the whatsapp extra is not installed"
).RequestValidator

from wactorz.core.outbound import Answer
from wactorz.interfaces.chat.outbound import Outbox
from wactorz.interfaces.chat.whatsapp import WhatsAppInterface

AUTH_TOKEN = "twilio-auth-token"
ALLOWED = "+306912345678"


class _Orchestrator:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def handle_turn(self, text: str, *, channel: str, user: str | None = None) -> str:
        self.seen.append(text)
        return "reply"

    async def handle_turn_stream(
        self,
        text: str,
        *,
        channel: str,
        user: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        yield await self.handle_turn(text, channel=channel, user=user)

    def commands(self) -> frozenset[str]:
        return frozenset()


class _Outbox:
    def __init__(self) -> None:
        self.answers: list[Answer] = []

    def register(self, name: str, sender: Any) -> None:
        return None

    def unregister(self, name: str) -> None:
        return None

    async def answered(self, answer: Answer) -> bool:
        self.answers.append(answer)
        return True


@pytest.fixture(name="interface")
def interface_fixture(monkeypatch: pytest.MonkeyPatch) -> WhatsAppInterface:
    """An interface whose outbound sends are captured rather than dialled."""
    iface = WhatsAppInterface(
        _Orchestrator(),
        account_sid="AC" + "0" * 32,
        auth_token=AUTH_TOKEN,
        from_number="+15550000000",
        port=0,
        allowed_numbers=frozenset({ALLOWED}),
        outbox=cast(Outbox, _Outbox()),
    )
    sent: list[tuple[str, str]] = []

    async def _capture(self: WhatsAppInterface, twilio: Any, body: str, to: str) -> None:
        sent.append((to, body))

    monkeypatch.setattr(WhatsAppInterface, "_send_message", _capture)
    iface.sent = sent  # pyright: ignore[reportAttributeAccessIssue]
    return iface


@pytest.fixture(name="client")
async def client_fixture(interface: WhatsAppInterface) -> AsyncGenerator[TestClient, None]:
    client = TestClient(TestServer(interface.build_app()))
    await client.start_server()
    yield client
    await client.close()


def _signed_post(client: TestClient, body: str) -> Any:
    form = {"Body": body, "From": f"whatsapp:{ALLOWED}"}
    url = str(client.make_url("/webhook/whatsapp"))
    signature = RequestValidator(AUTH_TOKEN).compute_signature(url, form)
    return client.post("/webhook/whatsapp", data=form, headers={"X-Twilio-Signature": signature})


async def _replies(interface: WhatsAppInterface) -> None:
    await asyncio.gather(*list(interface._replies))


async def test_the_question_goes_out_numbered_to_the_whatsapp_number(
    client: TestClient, interface: WhatsAppInterface
) -> None:
    message_id = await interface.send(
        f"whatsapp:{ALLOWED}",
        "Changed?",
        ["maintenance", "real problem"],
        message_id="m1",
        agent_id="agent-1",
    )

    assert message_id == "m1"
    assert interface.sent == [  # pyright: ignore[reportAttributeAccessIssue]
        (
            f"whatsapp:{ALLOWED}",
            "Changed?\n\n1. maintenance\n2. real problem\n\nReply with the number.",
        )
    ]


async def test_the_number_comes_back_as_the_answer_and_not_as_a_turn(
    client: TestClient, interface: WhatsAppInterface
) -> None:
    await interface.send(
        ALLOWED, "Changed?", ["maintenance", "real problem"], message_id="m1", agent_id="agent-1"
    )

    resp = await _signed_post(client, "2")
    assert resp.status == 200
    # The next message is a message again.
    await _signed_post(client, "2")
    await _replies(interface)

    outbox: Any = interface.outbox
    (answer,) = outbox.answers
    assert (
        answer.id,
        answer.agent_id,
        answer.option,
        answer.index,
        answer.channel,
        answer.user,
    ) == (
        "m1",
        "agent-1",
        "real problem",
        1,
        "whatsapp",
        ALLOWED,
    )
    orchestrator: Any = interface.orchestrator
    assert orchestrator.seen == ["2"]


async def test_a_reply_that_picks_nothing_is_a_message(
    client: TestClient, interface: WhatsAppInterface
) -> None:
    await interface.send(ALLOWED, "Changed?", ["yes", "no"], message_id="m1", agent_id="agent-1")

    await _signed_post(client, "what do you mean?")
    await _replies(interface)

    outbox: Any = interface.outbox
    assert outbox.answers == []
    orchestrator: Any = interface.orchestrator
    assert orchestrator.seen == ["what do you mean?"]


async def test_before_the_webhook_runs_there_is_nothing_to_send_with() -> None:
    iface = WhatsAppInterface(
        _Orchestrator(),
        account_sid="AC" + "0" * 32,
        auth_token=AUTH_TOKEN,
        from_number="+15550000000",
        port=0,
        allowed_numbers=frozenset({ALLOWED}),
    )
    with pytest.raises(RuntimeError, match="not running"):
        await iface.send(ALLOWED, "hi")


async def test_a_user_with_no_number_is_refused(
    client: TestClient, interface: WhatsAppInterface
) -> None:
    with pytest.raises(ValueError, match="phone number"):
        await interface.send("whatsapp:", "hi")
