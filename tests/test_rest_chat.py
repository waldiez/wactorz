"""The REST chat endpoints — POST /api/chat and POST /api/actors/{id}/message.

Nothing here had test coverage: the HTTP smoke test walks GET routes only, which
is how two defects shipped together in one handler. The 404 from the wrong
default agent name masked the second one, so a request that got *past* the name
check hit a `TypeError` that the task-discard callback swallowed — tokens billed,
no reply, nothing logged above debug.

The reply-callback tests assert the *contract* rather than a delivered message:
`route_chat` does `await reply_fn(text)` at ~30 sites, so whatever the handlers
pass must be awaitable and take one argument. A lambda satisfies neither.
"""

# pylint: disable=missing-function-docstring,too-few-public-methods

import inspect
from collections.abc import AsyncGenerator, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.web import api_actors, chat, runtime
from wactorz.web.app import build_app


class _Actor:
    """Minimal actor stand-in — the handlers only read `.name`."""

    def __init__(self, name: str) -> None:
        self.name = name


class _Registry:
    """Registry stand-in resolving by name (and id, for the actor endpoint)."""

    def __init__(self, *names: str) -> None:
        self._actors = {n: _Actor(n) for n in names}

    def find_by_name(self, name: str) -> _Actor | None:
        return self._actors.get(name)

    def get(self, actor_id: str) -> _Actor | None:
        return self._actors.get(actor_id)


@pytest.fixture(name="captured")
def captured_fixture(monkeypatch: pytest.MonkeyPatch) -> list[SimpleNamespace]:
    """Capture what the handlers hand to route_chat, without running it."""
    calls: list = []

    async def _fake_route_chat(
        content: str, reply_fn: Callable[..., str], *args: Any, **kwargs: Any
    ) -> None:
        calls.append(SimpleNamespace(content=content, reply_fn=reply_fn))

    monkeypatch.setattr(chat, "route_chat", _fake_route_chat)
    monkeypatch.setattr(api_actors.chat, "route_chat", _fake_route_chat, raising=False)
    return calls


@pytest.fixture(name="client")
async def client_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[TestClient, None]:
    """The real app with a registry holding the orchestrator, named `main`."""
    monkeypatch.setattr(runtime, "registry", _Registry("main", "helper"))
    async with TestClient(TestServer(build_app())) as c:
        yield c


# ── the default agent name ──────────────────────────────────────────────────


async def test_a_bare_message_reaches_the_orchestrator(client: TestClient) -> None:
    # The documented minimal request. The orchestrator is registered as "main".
    resp = await client.post("/api/chat", json={"message": "hello"})
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["agent"] == "main"


async def test_a_bare_message_is_addressed_to_the_orchestrator(
    client: TestClient, captured: list[SimpleNamespace]
) -> None:
    await client.post("/api/chat", json={"message": "hello"})
    assert captured[0].content == "@main hello"


async def test_an_explicit_agent_is_still_honoured(
    client: TestClient, captured: list[SimpleNamespace]
) -> None:
    resp = await client.post("/api/chat", json={"message": "hi", "agent_name": "helper"})
    assert resp.status == 200
    assert captured[0].content == "@helper hi"


async def test_an_unknown_agent_still_404s(
    client: TestClient, captured: list[SimpleNamespace]
) -> None:
    resp = await client.post("/api/chat", json={"message": "hi", "agent_name": "ghost"})
    assert resp.status == 404
    assert not captured


# ── the reply callback must be awaitable ────────────────────────────────────


async def test_the_reply_callback_is_a_coroutine_function(
    client: TestClient, captured: list[SimpleNamespace]
) -> None:
    # route_chat does `await reply_fn(...)`; a plain lambda raises TypeError on
    # the first chunk and abandons the stream after the tokens are paid for.
    await client.post("/api/chat", json={"message": "hello"})
    assert inspect.iscoroutinefunction(captured[0].reply_fn)


async def test_the_reply_callback_accepts_one_argument(
    client: TestClient, captured: list[SimpleNamespace]
) -> None:
    await client.post("/api/chat", json={"message": "hello"})
    await captured[0].reply_fn("a chunk of reply text")  # must not raise


async def test_the_actor_endpoint_reply_callback_is_awaitable_too(
    client: TestClient, captured: list[SimpleNamespace]
) -> None:
    # Same defect, second site.
    resp = await client.post("/api/actors/helper/message", json={"content": "hi"})
    assert resp.status == 200, await resp.text()
    assert inspect.iscoroutinefunction(captured[0].reply_fn)
    await captured[0].reply_fn("a chunk")


# ── unchanged behaviour ─────────────────────────────────────────────────────


async def test_an_empty_message_is_rejected(
    client: TestClient, captured: list[SimpleNamespace]
) -> None:
    resp = await client.post("/api/chat", json={"message": "   "})
    assert resp.status == 400
    assert not captured


async def test_malformed_json_is_rejected(client: TestClient) -> None:
    resp = await client.post("/api/chat", data=b"not json")
    assert resp.status == 400


async def test_an_addressed_message_is_not_double_prefixed(
    client: TestClient, captured: list[SimpleNamespace]
) -> None:
    await client.post("/api/chat", json={"message": "@helper hi"})
    assert captured[0].content == "@helper hi"


async def test_a_slash_command_is_passed_through(
    client: TestClient, captured: list[SimpleNamespace]
) -> None:
    await client.post("/api/chat", json={"message": "/status"})
    assert captured[0].content == "/status"
