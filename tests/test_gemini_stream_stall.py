"""A Gemini stream that stops early must say so.

The provider bridges the synchronous SDK generator to async through a queue. If
nothing arrives within the stall window the bridge stops waiting — but the
caller still received a normal final usage dict, so a truncated answer was
indistinguishable from a complete one. Everything downstream (the chat log, the
feed, the caller's own retry logic) treated a half-finished reply as finished.

The accumulated text is still delivered — tokens that arrived are real work and
throwing them away would be worse — but the final dict now carries `error`, so a
consumer can tell the difference.

No google-genai needed: the provider is built with ``__new__`` and handed only
the attributes its methods touch.
"""

# pylint: disable=missing-function-docstring,too-few-public-methods

import threading
import types
from typing import Any

import pytest

from wactorz.agents import llm_agent
from wactorz.agents.llm_agent import GeminiProvider


class _Types:
    @staticmethod
    def GenerateContentConfig(**kwargs: Any) -> dict[str, Any]:  # pylint: disable=invalid-name
        return kwargs


def _chunk(text: str = "", usage: Any = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(text=text, usage_metadata=usage)


_USAGE = types.SimpleNamespace(prompt_token_count=11, candidates_token_count=7)


class _StreamModels:
    """Yields `chunks`, then optionally blocks forever to simulate a stall."""

    def __init__(self, chunks: list[Any], then_hang: bool = False):
        self._chunks = chunks
        self._then_hang = then_hang
        self.released = threading.Event()

    def generate_content_stream(self, **_kwargs: Any):
        yield from self._chunks
        if self._then_hang:
            # Held until the test releases it, so the worker thread cannot
            # outlive the test run.
            self.released.wait(timeout=10)


def _provider(models: _StreamModels) -> GeminiProvider:
    provider = GeminiProvider.__new__(GeminiProvider)
    provider.model_name = "gemini-2.5-flash"
    provider._types = _Types  # pyright: ignore[reportAttributeAccessIssue]
    provider.client = types.SimpleNamespace(models=models)  # pyright: ignore[reportAttributeAccessIssue]
    return provider


async def _drain(provider: GeminiProvider) -> tuple[list[str], dict]:
    text: list[str] = []
    final: dict = {}
    async for chunk in provider.stream(messages=[{"role": "user", "content": "hi"}]):
        if isinstance(chunk, dict):
            final = chunk
        else:
            text.append(chunk)
    return text, final


@pytest.mark.asyncio
async def test_a_stalled_stream_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_agent, "_GEMINI_STREAM_STALL_TIMEOUT", 0.1)
    models = _StreamModels([_chunk("half an ")], then_hang=True)
    try:
        text, final = await _drain(_provider(models))
    finally:
        models.released.set()

    assert text == ["half an "], "text that did arrive must still be delivered"
    assert final.get("error"), "a truncated reply looked exactly like a finished one"
    assert "stall" in final["error"].lower()


@pytest.mark.asyncio
async def test_a_healthy_stream_reports_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_agent, "_GEMINI_STREAM_STALL_TIMEOUT", 5)
    models = _StreamModels([_chunk("all "), _chunk("done"), _chunk(usage=_USAGE)])

    text, final = await _drain(_provider(models))

    assert text == ["all ", "done"]
    assert "error" not in final
    assert final["input_tokens"] == 11
    assert final["output_tokens"] == 7


@pytest.mark.asyncio
async def test_a_provider_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_agent, "_GEMINI_STREAM_STALL_TIMEOUT", 5)

    class _Exploding(_StreamModels):
        def generate_content_stream(self, **_kwargs: Any):
            yield _chunk("partial")
            raise RuntimeError("upstream refused")

    text, final = await _drain(_provider(_Exploding([])))

    assert text == ["partial"]
    assert final.get("error"), "the stream died and the caller was told it succeeded"
    assert "upstream refused" in final["error"]


@pytest.mark.asyncio
async def test_usage_survives_a_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tokens already spent must still be billed, stall or not."""
    monkeypatch.setattr(llm_agent, "_GEMINI_STREAM_STALL_TIMEOUT", 0.1)
    models = _StreamModels([_chunk("x"), _chunk(usage=_USAGE)], then_hang=True)
    try:
        _text, final = await _drain(_provider(models))
    finally:
        models.released.set()

    assert final["input_tokens"] == 11
    assert final["output_tokens"] == 7
    assert final.get("error")
