"""The WhatsApp webhook only accepts requests Twilio signed.

`From` is matched against the allow-list, but a forged POST can name any
sender, so the allow-list alone authorises a caller it never authenticated.
The signature is what establishes that Twilio sent the request.
"""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from twilio.request_validator import RequestValidator

from wactorz.interfaces.chat.whatsapp import WhatsAppInterface

AUTH_TOKEN = "twilio-auth-token"
ALLOWED = "+306912345678"
FORM = {"Body": "hello", "From": f"whatsapp:{ALLOWED}"}


class _Main:
    """The one MainActor method the webhook reaches for."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def process_user_input_restricted(self, message: str) -> str:
        self.seen.append(message)
        return "reply"


@pytest.fixture(name="interface")
def interface_fixture(monkeypatch: pytest.MonkeyPatch) -> WhatsAppInterface:
    """An interface whose outbound sends are captured rather than dialled."""
    iface = WhatsAppInterface(
        _Main(),  # pyright: ignore[reportArgumentType]
        account_sid="AC" + "0" * 32,
        auth_token=AUTH_TOKEN,
        from_number="+15550000000",
        port=0,
        allowed_numbers=frozenset({ALLOWED}),
    )
    sent: list[str] = []

    async def _capture(self: WhatsAppInterface, twilio: Any, body: str, to: str) -> None:
        sent.append(body)

    monkeypatch.setattr(WhatsAppInterface, "_send_message", _capture)
    iface.sent = sent  # pyright: ignore[reportAttributeAccessIssue]
    return iface


@pytest.fixture(name="client")
async def client_fixture(interface: WhatsAppInterface) -> AsyncGenerator[TestClient, None]:
    client = TestClient(TestServer(interface.build_app()))
    await client.start_server()
    yield client
    await client.close()


def _signature(url: str, params: dict[str, str]) -> str:
    return RequestValidator(AUTH_TOKEN).compute_signature(url, params)


class TestUnsignedRequestsAreRefused:
    async def test_no_signature_header(self, client: TestClient) -> None:
        resp = await client.post("/webhook/whatsapp", data=FORM)
        assert resp.status == 403

    async def test_a_wrong_signature(self, client: TestClient) -> None:
        resp = await client.post(
            "/webhook/whatsapp", data=FORM, headers={"X-Twilio-Signature": "nope"}
        )
        assert resp.status == 403

    async def test_an_allow_listed_sender_is_not_enough(
        self, client: TestClient, interface: WhatsAppInterface
    ) -> None:
        # The forged POST names a permitted number; without a signature it is
        # still refused, and the agent is never reached.
        await client.post("/webhook/whatsapp", data=FORM)
        assert interface.agent.seen == []  # pyright: ignore[reportAttributeAccessIssue]

    async def test_a_signature_for_a_different_body_is_refused(self, client: TestClient) -> None:
        url = str(client.make_url("/webhook/whatsapp"))
        stale = _signature(url, {"Body": "something else", "From": FORM["From"]})
        resp = await client.post(
            "/webhook/whatsapp", data=FORM, headers={"X-Twilio-Signature": stale}
        )
        assert resp.status == 403


class TestASignedRequestIsAccepted:
    async def test_it_reaches_the_agent(
        self, client: TestClient, interface: WhatsAppInterface
    ) -> None:
        url = str(client.make_url("/webhook/whatsapp"))
        resp = await client.post(
            "/webhook/whatsapp", data=FORM, headers={"X-Twilio-Signature": _signature(url, FORM)}
        )
        assert resp.status == 200
        assert interface.agent.seen == ["hello"]  # pyright: ignore[reportAttributeAccessIssue]


class TestTheSignedUrl:
    def test_forwarded_headers_win_when_present(self) -> None:
        # Twilio signs the public URL from its console; behind a proxy the
        # request carries an internal host, so validation needs the forwarded one.
        request = _FakeRequest(
            url="http://127.0.0.1:8080/webhook/whatsapp",
            rel_url="/webhook/whatsapp",
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "bot.example.com"},
        )
        assert (
            WhatsAppInterface._signed_url(request)  # pyright: ignore[reportArgumentType]
            == "https://bot.example.com/webhook/whatsapp"
        )

    def test_the_request_url_is_used_without_them(self) -> None:
        request = _FakeRequest(
            url="https://bot.example.com/webhook/whatsapp",
            rel_url="/webhook/whatsapp",
            headers={},
        )
        assert (
            WhatsAppInterface._signed_url(request)  # pyright: ignore[reportArgumentType]
            == "https://bot.example.com/webhook/whatsapp"
        )


class _FakeRequest:
    """Just the attributes `_signed_url` reads."""

    def __init__(self, url: str, rel_url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.rel_url = rel_url
        self.headers = headers
