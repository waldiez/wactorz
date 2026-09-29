"""OpenAI and NVIDIA NIM: the request each sends, and how each recovers.

Both speak the Chat Completions format, but differ in what they send about
reasoning. OpenAI passes `reasoning_effort` through and, against a compatible
server, turns the chat template's thinking off when none was asked for. NIM
always states its thinking preference, because its reasoning models leave
thinking on by default and spend a small budget entirely on it. A server that
refuses those knobs costs one retry without them rather than the turn.

`tests/test_llm_provider_tools.py` covers tool calling; this covers plain
completion and streaming.
"""

import types
from typing import Any

import pytest

from tests.optional_deps import ensure_importable  # pyright: ignore[reportMissingImports]

ensure_importable("openai")

from wactorz.agents.llm.providers.nim import NIMProvider
from wactorz.agents.llm.providers.openai import OpenAIProvider


class _Completions:
    """`client.chat.completions`, answering each call from a queue."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **params: Any) -> Any:
        self.calls.append(dict(params))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Stream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def __aiter__(self) -> "_Stream":
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _usage(prompt: int, completion: int) -> Any:
    return types.SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


def _response(content: str | None, reasoning: str | None = None, usage: Any = None) -> Any:
    message = types.SimpleNamespace(content=content, reasoning_content=reasoning)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=usage)


def _chunk(content: str | None = None, reasoning: str | None = None, usage: Any = None) -> Any:
    delta = types.SimpleNamespace(content=content, reasoning_content=reasoning)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)], usage=usage)


def _usage_chunk(prompt: int, completion: int) -> Any:
    return types.SimpleNamespace(choices=[], usage=_usage(prompt, completion))


def _openai(model: str, base_url: str | None, completions: _Completions) -> OpenAIProvider:
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.model = model
    provider.base_url = base_url
    provider.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))  # pyright: ignore[reportAttributeAccessIssue]
    return provider


def _nim(model: str, completions: _Completions) -> NIMProvider:
    provider = NIMProvider.__new__(NIMProvider)
    provider.model = model
    provider.base_url = NIMProvider.NIM_BASE_URL
    provider.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))  # pyright: ignore[reportAttributeAccessIssue]
    return provider


async def _collect(stream: Any) -> list[Any]:
    return [item async for item in stream]


class TestOpenAIComplete:
    async def test_the_system_prompt_leads_and_usage_is_counted(self) -> None:
        completions = _Completions(_response("hi", usage=_usage(10, 2)))
        provider = _openai("gpt-4o", None, completions)

        text, usage = await provider._complete(
            [{"role": "user", "content": "hello"}], system="be brief"
        )

        assert text == "hi"
        assert (usage["input_tokens"], usage["output_tokens"]) == (10, 2)
        call = completions.calls[0]
        assert call["messages"][0] == {"role": "system", "content": "be brief"}
        assert "extra_body" not in call and "reasoning_effort" not in call

    async def test_a_compatible_server_is_told_not_to_think(self) -> None:
        completions = _Completions(_response(None, usage=_usage(1, 1)))
        provider = _openai("qwen3", "http://vllm:8000/v1", completions)

        text, _ = await provider._complete([{"role": "user", "content": "x"}])

        assert text == ""
        assert completions.calls[0]["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": False}
        }

    async def test_a_refused_effort_is_retried_without_it(self) -> None:
        completions = _Completions(
            RuntimeError("unknown parameter reasoning_effort"), _response("ok", usage=_usage(1, 1))
        )
        provider = _openai("gpt-4o", None, completions)

        text, _ = await provider._complete(
            [{"role": "user", "content": "x"}], reasoning_effort="high"
        )

        assert text == "ok"
        assert completions.calls[0]["reasoning_effort"] == "high"
        assert "reasoning_effort" not in completions.calls[1]

    async def test_any_other_error_is_raised(self) -> None:
        provider = _openai("gpt-4o", None, _Completions(RuntimeError("quota exceeded")))

        with pytest.raises(RuntimeError, match="quota exceeded"):
            await provider._complete([{"role": "user", "content": "x"}])


class TestOpenAIStream:
    async def test_chunks_then_usage(self) -> None:
        stream = _Stream([_chunk("Hel"), _chunk(None), _chunk("lo"), _usage_chunk(4, 2)])
        completions = _Completions(stream)
        provider = _openai("mistral-large", None, completions)

        items = await _collect(
            provider._stream([{"role": "user", "content": "x"}], reasoning_effort="low")
        )

        assert items[:2] == ["Hel", "lo"]
        assert (items[2]["input_tokens"], items[2]["output_tokens"]) == (4, 2)
        assert "reasoning_effort" not in completions.calls[0], "mistral takes no effort knob"

    async def test_a_refused_effort_is_retried_and_other_errors_raise(self) -> None:
        completions = _Completions(
            RuntimeError("reasoning_effort unsupported"), _Stream([_chunk("x")])
        )
        provider = _openai("gpt-4o", None, completions)

        items = await _collect(
            provider._stream([{"role": "user", "content": "x"}], reasoning_effort="high")
        )

        assert items[0] == "x"
        with pytest.raises(RuntimeError, match="boom"):
            await _collect(_openai("gpt-4o", None, _Completions(RuntimeError("boom")))._stream([]))


class TestNimComplete:
    async def test_thinking_is_switched_off_and_reasoning_is_the_fallback_text(self) -> None:
        completions = _Completions(_response("", reasoning="thought about it", usage=None))
        provider = _nim("deepseek-ai/deepseek-r1", completions)

        text, usage = await provider._complete([{"role": "user", "content": "x"}], system="s")

        assert text == "thought about it"
        assert usage["input_tokens"] == 0
        assert (
            completions.calls[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
        )

    async def test_a_model_refusing_the_thinking_knobs_is_retried_without_them(self) -> None:
        completions = _Completions(
            RuntimeError("chat_template kwargs not allowed"), _response("ok", usage=_usage(3, 1))
        )
        provider = _nim("meta/llama-3.3-70b-instruct", completions)

        text, usage = await provider._complete(
            [{"role": "user", "content": "x"}], reasoning_effort="high"
        )

        assert text == "ok"
        assert usage["output_tokens"] == 1
        assert completions.calls[0]["reasoning_effort"] == "high"
        assert "reasoning_effort" not in completions.calls[1]
        assert "extra_body" not in completions.calls[1]

    @pytest.mark.parametrize(
        ("model", "error"),
        [
            ("meta/llama-3.3-70b-instruct", RuntimeError("rate limited")),
            ("mistralai/mistral-7b-instruct-v0.3", RuntimeError("reasoning not supported")),
        ],
    )
    async def test_errors_that_retrying_cannot_fix_are_raised(
        self, model: str, error: Exception
    ) -> None:
        provider = _nim(model, _Completions(error))

        with pytest.raises(RuntimeError):
            await provider._complete([{"role": "user", "content": "x"}])


class TestNimStream:
    async def test_content_reasoning_and_usage_are_yielded(self) -> None:
        stream = _Stream(
            [
                _chunk(None, reasoning="thinking..."),
                _chunk("answer", usage=_usage(5, 3)),
                types.SimpleNamespace(choices=[], usage=None),
                _usage_chunk(6, 4),
            ]
        )
        provider = _nim("deepseek-ai/deepseek-r1", _Completions(stream))

        items = await _collect(provider._stream([{"role": "user", "content": "x"}], system="s"))

        assert items[:2] == ["thinking...", "answer"]
        assert (items[2]["input_tokens"], items[2]["output_tokens"]) == (6, 4)

    async def test_a_chunk_with_nothing_to_say_is_skipped(self) -> None:
        provider = _nim("meta/llama", _Completions(_Stream([_chunk(None, reasoning=None)])))

        items = await _collect(provider._stream([]))

        assert len(items) == 1 and isinstance(items[0], dict)

    def test_the_provider_accepts_block_content(self) -> None:
        assert NIMProvider.supports_blocks() is True
        assert OpenAIProvider.supports_blocks() is True
