"""Reaching the spend cap stops the next paid call, whichever route it takes.

The limit is enforced on `LLMProvider`'s public methods rather than at the
places that call them. Almost every caller holds a provider and calls it
directly — `self.llm.complete(...)` — so a check that lives in the agent
guards only the handful of paths that go through the agent, and each new call
site is another chance to miss it.

Placing it on the base makes the guarded surface the same object every caller
already has, and a provider added later inherits it.
"""

from typing import Any
from unittest.mock import patch

import pytest

import wactorz.agents.llm_agent as llm
from wactorz.agents.llm_agent import LLMProvider


class _KV:
    """A key-value store standing in for the persistence layer."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], Any] = {}

    def kv_get(self, ns: str, key: str) -> Any:
        return self._data.get((ns, key))

    def kv_set(self, ns: str, key: str, value: Any) -> None:
        self._data[(ns, key)] = value


class _Provider(LLMProvider):
    """A provider that records whether the model was actually reached."""

    def __init__(self) -> None:
        self.calls = 0

    async def _complete(
        self, messages: list[dict], system: str = "", **kwargs: Any
    ) -> tuple[str, dict]:
        self.calls += 1
        return "answer", {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}

    async def _complete_with_tools(
        self, messages: list[dict], tools: list[dict], system: str = "", **kwargs: Any
    ) -> Any:
        self.calls += 1
        return "tool answer"

    async def _stream(self, messages: list[dict], system: str = "", **kwargs: Any) -> Any:
        self.calls += 1
        yield "chunk"


class _CompleteOnly(LLMProvider):
    """Implements only the non-streaming path."""

    async def _complete(
        self, messages: list[dict], system: str = "", **kwargs: Any
    ) -> tuple[str, dict]:
        return "answer", {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}


@pytest.fixture(name="provider")
def provider_fixture() -> Any:
    """A provider wired to an isolated cost store."""
    store = _KV()
    with patch.object(llm, "get_db", lambda: store):
        yield _Provider()


def _spend(amount: float, limit: float) -> None:
    llm.set_cost_limit(limit, "monthly")
    llm._accumulate_global_cost(amount)


class TestTheCapBlocks:
    async def test_complete_is_refused_once_the_cap_is_reached(self, provider: _Provider) -> None:
        _spend(10.0, limit=5.0)

        with pytest.raises(RuntimeError, match="cost limit"):
            await provider.complete([{"role": "user", "content": "hi"}])

        # Refused before the request, not after — the point is not spending.
        assert provider.calls == 0

    async def test_tool_calls_are_refused_too(self, provider: _Provider) -> None:
        _spend(10.0, limit=5.0)

        with pytest.raises(RuntimeError, match="cost limit"):
            await provider.complete_with_tools([{"role": "user", "content": "hi"}], tools=[])

        assert provider.calls == 0

    async def test_streaming_is_refused_too(self, provider: _Provider) -> None:
        _spend(10.0, limit=5.0)

        with pytest.raises(RuntimeError, match="cost limit"):
            async for _ in provider.stream([{"role": "user", "content": "hi"}]):
                pass

        assert provider.calls == 0


class TestTheCapDoesNotGetInTheWay:
    async def test_a_call_under_the_cap_goes_through(self, provider: _Provider) -> None:
        _spend(1.0, limit=5.0)

        text, _usage = await provider.complete([{"role": "user", "content": "hi"}])

        assert text == "answer"
        assert provider.calls == 1

    async def test_no_configured_cap_means_no_limit(self, provider: _Provider) -> None:
        # The default is 0.0 = disabled, so an install that never set a limit
        # must be unaffected by any of this.
        llm._accumulate_global_cost(1_000.0)

        text, _usage = await provider.complete([{"role": "user", "content": "hi"}])

        assert text == "answer"


class TestStreamingSupport:
    def test_a_provider_implementing_stream_reports_support(self) -> None:
        assert _Provider.supports_streaming() is True

    def test_a_provider_without_it_does_not(self) -> None:
        # `hasattr(provider, "stream")` is True for both, since the base defines
        # one — so the capability has to be read from whether `_stream` was
        # implemented.
        assert hasattr(_CompleteOnly(), "stream")
        assert _CompleteOnly.supports_streaming() is False

    def test_the_shipped_providers_all_stream(self) -> None:
        for cls in (llm.AnthropicProvider, llm.OpenAIProvider, llm.OllamaProvider):
            assert cls.supports_streaming() is True, cls.__name__
