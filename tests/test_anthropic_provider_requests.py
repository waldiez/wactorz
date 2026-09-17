"""What the Anthropic provider sends, and how it survives a model that refuses it.

Reasoning is the part that changed under this provider. Older Claude models
think only when asked; Claude Opus 5 and later think unless told not to, so a
caller that wants no reasoning has to say so — and Fable and Mythos cannot be
told at all. Depth travels separately as `output_config.effort`, clamped to the
levels each model family accepts, and only alongside thinking.

A parameter a model refuses costs one 400: it is dropped, the model is
remembered for the rest of the process, and the request is sent again. Only one
parameter is dropped per attempt, and nothing is retried once a stream has
started yielding. `tests/test_llm_temperature.py` covers the temperature half.
"""

import types
from typing import Any

import pytest

from tests.optional_deps import ensure_importable  # pyright: ignore[reportMissingImports]

ensure_importable("anthropic")

from wactorz.agents.llm.providers import anthropic as mod
from wactorz.agents.llm.providers.anthropic import AnthropicProvider


class _BadRequest(Exception):
    status_code = 400


class _Messages:
    """`client.messages`, answering from a queue; `**kwargs` keeps temperature legal."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **params: Any) -> Any:
        self.calls.append(dict(params))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def stream(self, **params: Any) -> Any:
        self.calls.append(dict(params))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Stream:
    def __init__(self, chunks: list[str], fail_after: Exception | None = None) -> None:
        self._chunks = chunks
        self._fail_after = fail_after

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    @property
    def text_stream(self) -> Any:
        return self._text()

    async def _text(self) -> Any:
        for chunk in self._chunks:
            yield chunk
        if self._fail_after:
            raise self._fail_after

    async def get_final_message(self) -> Any:
        return types.SimpleNamespace(usage=types.SimpleNamespace(input_tokens=7, output_tokens=3))


def _block(kind: str, **fields: Any) -> Any:
    return types.SimpleNamespace(type=kind, **fields)


def _response(*blocks: Any, usage: Any = None) -> Any:
    return types.SimpleNamespace(
        content=list(blocks),
        usage=usage or types.SimpleNamespace(input_tokens=5, output_tokens=2),
        stop_reason="end_turn",
    )


def _provider(model: str, messages: _Messages) -> AnthropicProvider:
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.model = model
    provider.client = types.SimpleNamespace(messages=messages)  # pyright: ignore[reportAttributeAccessIssue]
    return provider


@pytest.fixture(autouse=True)
def _forget_learned_models(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_learned_no_sampling", "_learned_no_thinking_param", "_learned_no_effort"):
        monkeypatch.setattr(mod, name, set())


class TestThinkingAndEffort:
    @pytest.mark.parametrize(
        ("model", "effort", "thinking", "output_config"),
        [
            ("claude-sonnet-4-6", None, None, None),
            ("claude-sonnet-4-6", "high", {"type": "adaptive"}, {"effort": "high"}),
            ("claude-opus-4-5", "max", {"type": "adaptive"}, {"effort": "high"}),
            ("claude-sonnet-4-6", "xhigh", {"type": "adaptive"}, {"effort": "high"}),
            ("claude-haiku-4-5", "high", {"type": "adaptive"}, None),
            ("claude-opus-5", "none", {"type": "disabled"}, None),
            ("claude-opus-5", "minimal", {"type": "adaptive"}, {"effort": "low"}),
            ("claude-opus-5", "extreme", {"type": "adaptive"}, None),
            ("claude-fable-5", None, None, None),
            ("claude-fable-5", "max", {"type": "adaptive"}, {"effort": "max"}),
        ],
    )
    def test_the_request_carries_what_each_model_accepts(
        self,
        model: str,
        effort: str | None,
        thinking: dict[str, str] | None,
        output_config: dict[str, str] | None,
    ) -> None:
        params = _provider(model, _Messages())._request_params(
            [], "sys", {"reasoning_effort": effort}
        )

        assert params.get("thinking") == thinking
        assert params.get("output_config") == output_config

    def test_a_model_that_cannot_stop_thinking_is_warned_about(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        _provider("claude-mythos-5", _Messages())._request_params([], "", {})

        assert "cannot disable reasoning" in caplog.text

    def test_an_uninspectable_client_is_assumed_to_carry_temperature(self) -> None:
        assert mod._carries_temperature(object()) is True
        assert mod._carries_temperature(len) is False


class TestRejections:
    @pytest.mark.parametrize(
        ("exc", "thinking", "effort", "temperature"),
        [
            (_BadRequest("thinking.type disabled is not supported"), True, False, False),
            (_BadRequest("output_config.effort: invalid"), False, True, False),
            (_BadRequest("temperature is deprecated for this model"), False, False, True),
            (TypeError("unexpected keyword argument 'temperature'"), False, False, True),
            (_BadRequest("temperature must be between 0 and 1"), False, False, False),
            (_BadRequest("max_tokens too large"), False, False, False),
            (RuntimeError("thinking not supported"), False, False, False),
        ],
    )
    def test_only_a_refusal_of_the_parameter_itself_counts(
        self, exc: Exception, thinking: bool, effort: bool, temperature: bool
    ) -> None:
        assert mod._is_thinking_rejection(exc) is thinking
        assert mod._is_effort_rejection(exc) is effort
        assert mod._is_temperature_rejection(exc) is temperature

    async def test_a_refused_thinking_block_is_dropped_and_remembered(self) -> None:
        messages = _Messages(
            _BadRequest("thinking: unexpected field"), _response(_block("text", text="ok"))
        )
        provider = _provider("claude-opus-5", messages)

        text, _ = await provider._complete([{"role": "user", "content": "x"}])

        assert text == "ok"
        assert "thinking" in messages.calls[0] and "thinking" not in messages.calls[1]
        assert "claude-opus-5" in mod._learned_no_thinking_param
        assert provider._request_params([], "", {}).get("thinking") is None

    async def test_a_refused_effort_is_dropped_and_remembered(self) -> None:
        messages = _Messages(
            _BadRequest("output_config not permitted"), _response(_block("text", text="ok"))
        )
        provider = _provider("claude-opus-4-7", messages)

        await provider._complete([{"role": "user", "content": "x"}], reasoning_effort="high")

        assert "output_config" not in messages.calls[1]
        assert mod._effort_for("claude-opus-4-7", "high") is None

    async def test_an_error_that_is_not_a_refusal_is_raised(self) -> None:
        provider = _provider("claude-opus-5", _Messages(_BadRequest("prompt is too long")))

        with pytest.raises(_BadRequest):
            await provider._complete([{"role": "user", "content": "x"}])


class TestText:
    def test_text_blocks_are_joined_and_reasoning_is_skipped(self) -> None:
        response = _response(
            _block("thinking", thinking="hmm"),
            _block("text", text="Hello "),
            _block("text", text="there"),
        )

        assert _provider("claude-opus-5", _Messages())._extract_text(response) == "Hello there"

    def test_a_response_of_only_reasoning_is_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        response = _response(_block("redacted_thinking", data="x"))

        assert _provider("claude-opus-5", _Messages())._extract_text(response) == ""
        assert "returned only reasoning and no text" in caplog.text


class TestTools:
    async def test_tool_calls_and_reasoning_are_replayable(self) -> None:
        class _Dumpable:
            type = "thinking"

            def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
                return {"type": "thinking", "thinking": "plan", "signature": "sig"}

        messages = _Messages(
            _response(
                _Dumpable(),
                _block("redacted_thinking", data="opaque"),
                _block("thinking", thinking=None, signature=None),
                _block("text", text="Checking."),
                _block("tool_use", id="t1", name="lookup", input="not a dict"),
                usage=types.SimpleNamespace(input_tokens=9, output_tokens=4),
            )
        )
        provider = _provider("claude-sonnet-4-6", messages)

        result = await provider._complete_with_tools(
            [
                {"role": "system", "content": "dropped"},
                {"role": "user", "content": "find it"},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                {"role": "tool", "id": "t0", "content": 42, "is_error": True},
            ],
            [{"name": "lookup", "description": "Finds things"}, {"name": "bare"}],
            system="sys",
        )

        assert result.content == "Checking."
        assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [("t1", "lookup", {})]
        assert result.assistant_message is not None
        assert result.assistant_message["content"] == [
            {"type": "thinking", "thinking": "plan", "signature": "sig"},
            {"type": "redacted_thinking", "data": "opaque"},
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "t1", "name": "lookup", "input": {}},
        ]
        sent = messages.calls[0]
        assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"]
        assert sent["messages"][2]["content"][0] == {
            "type": "tool_result",
            "tool_use_id": "t0",
            "content": "42",
            "is_error": True,
        }
        assert sent["tools"][1]["input_schema"] == {"type": "object", "properties": {}}

    def test_a_block_that_cannot_be_reproduced_is_left_out(self) -> None:
        class _BrokenDump:
            type = "thinking"
            thinking = "t"
            signature = "s"

            def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
                raise ValueError("not pydantic")

        assert mod._replayable_block(_BrokenDump()) == {
            "type": "thinking",
            "thinking": "t",
            "signature": "s",
        }
        assert mod._replayable_block(_block("redacted_thinking", data=None)) is None
        assert mod._replayable_block(_block("image")) is None

    async def test_a_response_without_usage_counts_zero(self) -> None:
        response = types.SimpleNamespace(content=None, usage=None)
        provider = _provider("claude-sonnet-4-6", _Messages(response))

        result = await provider._complete_with_tools([], [])

        assert result.usage["input_tokens"] == 0 and result.content == ""


class TestStreaming:
    async def test_chunks_then_usage(self) -> None:
        provider = _provider("claude-sonnet-4-6", _Messages(_Stream(["Hel", "lo"])))

        items = [item async for item in provider._stream([{"role": "user", "content": "x"}])]

        assert items[:2] == ["Hel", "lo"]
        usage = items[2]
        assert isinstance(usage, dict)
        assert (usage["input_tokens"], usage["output_tokens"]) == (7, 3)

    async def test_a_refusal_before_the_first_chunk_is_retried(self) -> None:
        messages = _Messages(_BadRequest("thinking is not supported"), _Stream(["ok"]))
        provider = _provider("claude-opus-5", messages)

        items = [item async for item in provider._stream([])]

        assert items[0] == "ok"
        assert "thinking" not in messages.calls[1]

    async def test_a_failure_after_output_started_is_not_retried(self) -> None:
        stream = _Stream(["partial"], fail_after=_BadRequest("thinking is not supported"))
        provider = _provider("claude-opus-5", _Messages(stream, _Stream(["again"])))
        seen: list[Any] = []

        with pytest.raises(_BadRequest):
            async for item in provider._stream([]):
                seen.append(item)

        assert seen == ["partial"]

    def test_block_content_is_accepted(self) -> None:
        assert AnthropicProvider.supports_blocks() is True
