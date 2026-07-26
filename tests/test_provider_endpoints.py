"""Providers must talk to the endpoint they were configured with.

A dropped `base_url` does not fail loudly — it silently redirects traffic to the
vendor's public API, sending prompts and the configured key somewhere the
operator did not choose. Anyone running a self-hosted or proxied
OpenAI-compatible endpoint (vLLM, LiteLLM, an internal gateway) depends on this.

The SDK class is replaced with a recorder, so these run without the optional
extras installed.
"""

# pylint: disable=missing-function-docstring,too-few-public-methods

import sys
import types

import pytest

from tests.optional_deps import ensure_importable

ensure_importable("openai")

from wactorz.agents.llm_agent import (
    LLMAgent,
    LLMProvider,
    NIMProvider,
    OllamaProvider,
    OpenAIProvider,
)

_CUSTOM = "https://llm.internal.example/v1"


@pytest.fixture(name="openai_calls")
def openai_calls_fixture(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record the kwargs the provider hands to openai.AsyncOpenAI."""
    calls: list[dict] = []

    def _fake_async_openai(**kwargs) -> types.SimpleNamespace:
        calls.append(kwargs)
        return types.SimpleNamespace()

    # raising=False so this also works against a bare stub module.
    monkeypatch.setattr(sys.modules["openai"], "AsyncOpenAI", _fake_async_openai, raising=False)
    return calls


# ── OpenAI ──────────────────────────────────────────────────────────────────


def test_a_custom_base_url_reaches_the_openai_client(openai_calls: list[dict]) -> None:
    OpenAIProvider(model="gpt-4o", api_key="k", base_url=_CUSTOM)
    assert openai_calls[0].get("base_url") == _CUSTOM, (
        "the configured endpoint was dropped — traffic would go to api.openai.com"
    )


def test_no_base_url_leaves_the_client_on_its_default(openai_calls: list[dict]) -> None:
    OpenAIProvider(model="gpt-4o", api_key="k")
    assert openai_calls[0].get("base_url") is None


def test_the_api_key_is_always_passed(openai_calls: list[dict]) -> None:
    OpenAIProvider(model="gpt-4o", api_key="k", base_url=_CUSTOM)
    assert openai_calls[0]["api_key"] == "k"


def test_the_provider_records_the_endpoint_it_uses(openai_calls: list[dict]) -> None:
    # `base_url` is read back for reporting; it must agree with the client.
    provider = OpenAIProvider(model="gpt-4o", api_key="k", base_url=_CUSTOM)
    assert provider.base_url == _CUSTOM
    assert openai_calls[0].get("base_url") == provider.base_url


def test_an_empty_base_url_is_normalised_to_none(openai_calls: list[dict]) -> None:
    provider = OpenAIProvider(model="gpt-4o", api_key="k", base_url="")
    assert provider.base_url is None
    assert openai_calls[0].get("base_url") is None


# ── the sibling providers, same class of bug ────────────────────────────────


def test_nim_passes_its_base_url_through(openai_calls: list[dict]) -> None:
    NIMProvider(model="m", api_key="k", base_url=_CUSTOM)
    assert openai_calls[0].get("base_url") == _CUSTOM


def test_ollama_keeps_the_base_url_it_was_given() -> None:
    assert OllamaProvider(model="llama3", base_url="http://box:11434").base_url == (
        "http://box:11434"
    )


# ── streaming capability detection ──────────────────────────────────────────


class _CompleteOnly(LLMProvider):  # pylint: disable=abstract-method
    """A custom provider that implements only the non-streaming path."""

    async def complete(self, messages, system="", **kwargs):
        return "whole answer", {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}


class _Streaming(LLMProvider):  # pylint: disable=abstract-method
    """A custom provider that really streams."""

    async def stream(self, messages, system="", **kwargs):
        yield "a"
        yield "b"


async def test_a_complete_only_provider_falls_back_instead_of_raising() -> None:
    """A provider implementing only complete() must degrade, not raise.

    Guarded by hasattr(llm, "stream") — this fails the day someone declares
    stream() on LLMProvider without replacing that check.
    """
    agent = LLMAgent(llm_provider=_CompleteOnly(), name="fallback-agent")
    chunks = [c async for c in agent.chat_stream("hello")]
    assert any(isinstance(c, str) and "whole answer" in c for c in chunks), chunks


async def test_a_streaming_provider_is_not_forced_down_the_fallback() -> None:
    """The other side of the guard: a real stream() must actually be used."""
    agent = LLMAgent(llm_provider=_Streaming(), name="streaming-agent")
    chunks = [c async for c in agent.chat_stream("hello")]
    text = "".join(c for c in chunks if isinstance(c, str))
    assert "ab" in text, chunks
