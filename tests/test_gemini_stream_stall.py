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

from wactorz.agents.llm.providers import gemini
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
    monkeypatch.setattr(gemini, "_GEMINI_STREAM_STALL_TIMEOUT", 0.1)
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
    monkeypatch.setattr(gemini, "_GEMINI_STREAM_STALL_TIMEOUT", 5)
    models = _StreamModels([_chunk("all "), _chunk("done"), _chunk(usage=_USAGE)])

    text, final = await _drain(_provider(models))

    assert text == ["all ", "done"]
    assert "error" not in final
    assert final["input_tokens"] == 11
    assert final["output_tokens"] == 7


@pytest.mark.asyncio
async def test_a_provider_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini, "_GEMINI_STREAM_STALL_TIMEOUT", 5)

    class _Exploding(_StreamModels):
        def generate_content_stream(self, **_kwargs: Any):
            yield _chunk("partial")
            raise RuntimeError("upstream refused")

    text, final = await _drain(_provider(_Exploding([])))

    assert text == ["partial"]
    assert final.get("error"), "the stream died and the caller was told it succeeded"
    assert "upstream refused" in final["error"]


class _Busy(Exception):
    """A rate limit, shaped the way the SDKs report one."""

    status_code = 429


async def _drain_raw(provider: GeminiProvider) -> tuple[list[str], dict]:
    """Read `_stream` itself, with no retry wrapper in the way."""
    text: list[str] = []
    final: dict = {}
    async for chunk in provider._stream(messages=[{"role": "user", "content": "hi"}]):  # pylint: disable=protected-access
        if isinstance(chunk, dict):
            final = chunk
        else:
            text.append(chunk)
    return text, final


@pytest.mark.asyncio
async def test_a_failure_before_any_text_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Described in the final item, the failure was a finished stream.

    Nothing has been read yet, so the attempt can be made again from the start —
    but only if it is raised, because a stream that ends normally carrying an
    error message is not something the retry policy is ever asked about.
    """
    monkeypatch.setattr(gemini, "_GEMINI_STREAM_STALL_TIMEOUT", 5)
    busy = _Busy("rate limited")

    class _Exploding(_StreamModels):
        def generate_content_stream(self, **_kwargs: Any):
            raise busy
            yield  # pragma: no cover  # makes this a generator

    with pytest.raises(_Busy) as caught:
        await _drain_raw(_provider(_Exploding([])))

    assert caught.value is busy, "the status has to survive, not just the message"


@pytest.mark.asyncio
async def test_a_stall_before_any_text_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini, "_GEMINI_STREAM_STALL_TIMEOUT", 0.1)
    models = _StreamModels([], then_hang=True)
    try:
        with pytest.raises(TimeoutError):
            await _drain_raw(_provider(models))
    finally:
        models.released.set()


@pytest.mark.asyncio
async def test_a_stream_that_fails_before_it_starts_is_tried_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of raising: the public stream now gets a second attempt."""
    monkeypatch.setattr(gemini, "_GEMINI_STREAM_STALL_TIMEOUT", 5)
    monkeypatch.setattr("wactorz.agents.llm.retry.BACKOFF_BASE_S", 0.0)
    monkeypatch.setattr("wactorz.agents.llm.retry.BACKOFF_CAP_S", 0.0)

    class _FailsOnce(_StreamModels):
        attempts = 0

        def generate_content_stream(self, **_kwargs: Any):
            _FailsOnce.attempts += 1
            if _FailsOnce.attempts == 1:
                raise _Busy("rate limited")
            yield _chunk("second time lucky")

    text, final = await _drain(_provider(_FailsOnce([])))

    assert _FailsOnce.attempts == 2
    assert text == ["second time lucky"]
    assert "error" not in final


@pytest.mark.asyncio
async def test_usage_survives_a_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tokens already spent must still be billed, stall or not."""
    monkeypatch.setattr(gemini, "_GEMINI_STREAM_STALL_TIMEOUT", 0.1)
    models = _StreamModels([_chunk("x"), _chunk(usage=_USAGE)], then_hang=True)
    try:
        _text, final = await _drain(_provider(models))
    finally:
        models.released.set()

    assert final["input_tokens"] == 11
    assert final["output_tokens"] == 7
    assert final.get("error")
