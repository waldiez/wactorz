"""Blocking network calls must not run on the shared event loop.

Every actor in the process runs on one loop. A synchronous call made from inside
``async def`` freezes all of them for its full duration — which for a Twilio
round-trip is a network hop, and for a failing mDNS lookup is a DNS timeout.
While it runs, MQTT keepalive stops too, and the heartbeat watchdog can restart
healthy actors as "presumed crashed".

Each test runs an unrelated ticker alongside the call and counts how often it
gets scheduled: a blocked loop starves it, an offloaded call does not.
"""

# pylint: disable=missing-function-docstring,too-few-public-methods

import asyncio
import socket
import types
from collections.abc import Awaitable

import pytest

from wactorz.interfaces.chat_interfaces import WhatsAppInterface

# Long enough that a blocked loop is unambiguous, short enough to stay fast.
_BLOCK = 0.2


async def _loop_ticks_during(coro: Awaitable) -> int:
    """Run `coro`, counting how many times an unrelated task got scheduled."""
    ticks = 0
    stop = False

    async def _ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.001)

    task = asyncio.ensure_future(_ticker())
    await asyncio.sleep(0)
    try:
        await coro
    finally:
        stop = True
        await task
    return ticks


class _BlockingTwilio:
    """Stands in for the sync Twilio SDK: really blocks the calling thread."""

    def __init__(self) -> None:
        self.messages = types.SimpleNamespace(create=self._create)
        self.calls: list[dict] = []

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        import time

        time.sleep(_BLOCK)


def _whatsapp() -> WhatsAppInterface:
    iface = WhatsAppInterface.__new__(WhatsAppInterface)
    iface.from_number = "+10000000000"  # pyright: ignore[reportAttributeAccessIssue]
    return iface


class TestTwilioSend:
    @pytest.mark.asyncio
    async def test_sending_does_not_block_the_loop(self):
        iface, twilio = _whatsapp(), _BlockingTwilio()

        ticks = await _loop_ticks_during(
            iface._send_message(twilio, "hello", "whatsapp:+15550000000")
        )

        assert ticks > 5, f"loop starved for the whole send ({ticks} ticks)"

    @pytest.mark.asyncio
    async def test_sending_passes_the_expected_fields(self):
        iface, twilio = _whatsapp(), _BlockingTwilio()

        await iface._send_message(twilio, "hello", "whatsapp:+15550000000")

        assert twilio.calls == [
            {
                "body": "hello",
                "from_": "whatsapp:+10000000000",
                "to": "whatsapp:+15550000000",
            }
        ]


class TestHostLookup:
    @pytest.mark.asyncio
    async def test_a_slow_dns_lookup_does_not_block_the_loop(self, monkeypatch):
        from wactorz.interfaces import chat_interfaces

        def _slow(_host: str) -> str:
            import time

            time.sleep(_BLOCK)
            return "10.0.0.5"

        monkeypatch.setattr(socket, "gethostbyname", _slow)

        ticks = await _loop_ticks_during(chat_interfaces._resolve_host("nowhere.local"))

        assert ticks > 5, f"loop starved during the lookup ({ticks} ticks)"

    @pytest.mark.asyncio
    async def test_resolution_failure_is_reported_as_none(self, monkeypatch):
        def _fail(_host: str) -> str:
            raise socket.gaierror("no such host")

        monkeypatch.setattr(socket, "gethostbyname", _fail)

        from wactorz.interfaces import chat_interfaces

        assert await chat_interfaces._resolve_host("nowhere.local") is None

    @pytest.mark.asyncio
    async def test_a_successful_lookup_returns_the_address(self, monkeypatch):
        monkeypatch.setattr(socket, "gethostbyname", lambda _h: "192.168.1.7")

        from wactorz.interfaces import chat_interfaces

        assert await chat_interfaces._resolve_host("pi.local") == "192.168.1.7"
