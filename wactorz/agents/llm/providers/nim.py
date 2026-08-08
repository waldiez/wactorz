"""NIMProvider — NVIDIA NIM, OpenAI-shaped."""

import logging
from typing import Any

from ..base import LLMProvider, ToolCall, ToolCompletion, _temp_params
from ..openai_shape import (
    _apply_openai_reasoning,
    _content_or_reasoning,
    _openai_tool_result_message,
    _openai_tools,
    _parse_tool_arguments,
    _usage_from_openai,
)
from ..pricing import calc_cost

logger = logging.getLogger(__name__)


class NIMProvider(LLMProvider):
    """NVIDIA NIM — OpenAI-compatible API hosted at integrate.api.nvidia.com.
    Free tier: 1000 requests/month per model. No local GPU required.

    Popular free models:
      meta/llama-3.1-8b-instruct          — fast, lightweight
      meta/llama-3.3-70b-instruct         — strong general purpose
      mistralai/mistral-7b-instruct-v0.3  — fast & capable
      mistralai/mixtral-8x7b-instruct-v0.1
      google/gemma-3-27b-it
      microsoft/phi-3-mini-128k-instruct
      deepseek-ai/deepseek-r1             — reasoning model
      deepseek-ai/deepseek-r1-distill-qwen-7b
      nvidia/llama-3.1-nemotron-70b-instruct
      nvidia/llama-3.3-nemotron-super-49b-v1

    Get a free API key at: https://build.nvidia.com
    """

    NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"

    def __init__(
        self,
        model: str = "meta/llama-3.3-70b-instruct",
        api_key: str | None = None,
        base_url: str = NIM_BASE_URL,
    ):
        import openai

        self.model = model
        self.base_url = base_url
        self.client = openai.AsyncOpenAI(
            api_key=api_key or "dummy",  # NIM free tier may not require a key locally
            base_url=base_url,
        )

    async def _create_completion(self, params: dict[str, Any]):
        """Create a completion, retrying without reasoning knobs if rejected.

        Some NIM models don't accept the ``enable_thinking`` chat-template flag
        or ``reasoning_effort``; rather than fail the whole turn, drop those
        knobs and retry once so non-reasoning models keep working.
        """
        try:
            return await self.client.chat.completions.create(**params)
        except Exception as exc:
            msg = str(exc).lower()
            retryable = any(
                token in msg
                for token in ("reasoning", "enable_thinking", "chat_template", "extra_body")
            )
            if retryable and ("extra_body" in params or "reasoning_effort" in params):
                params.pop("extra_body", None)
                params.pop("reasoning_effort", None)
                return await self.client.chat.completions.create(**params)
            raise

    async def _complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        params: dict[str, Any] = {
            "model": self.model,
            "messages": full_messages,
            "max_tokens": kwargs.get("max_tokens", 8192),
            **_temp_params(kwargs),
        }
        _apply_openai_reasoning(params, self.model, kwargs.get("reasoning_effort"))
        response = await self._create_completion(params)
        text = _content_or_reasoning(response.choices[0].message)
        input_tok = response.usage.prompt_tokens if response.usage else 0
        output_tok = response.usage.completion_tokens if response.usage else 0
        usage = {
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cost_usd": calc_cost(self.model, input_tok, output_tok),
        }
        return text, usage

    async def _complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> ToolCompletion:
        full_messages = ([{"role": "system", "content": system}] if system else []) + [
            _openai_tool_result_message(m) if m.get("role") == "tool" else m for m in messages
        ]
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=full_messages,
                tools=_openai_tools(tools),
                tool_choice=kwargs.get("tool_choice", "auto"),
                max_tokens=kwargs.get("max_tokens", 8192),
                **_temp_params(kwargs),
            )
        except Exception as exc:
            raise RuntimeError(
                f"NIM tool calling failed; verify the selected model supports tools: {exc}"
            ) from exc
        message = response.choices[0].message
        raw_calls = getattr(message, "tool_calls", None) or []
        tool_calls = [
            ToolCall(
                id=getattr(call, "id", ""),
                name=getattr(getattr(call, "function", None), "name", ""),
                arguments=_parse_tool_arguments(
                    getattr(getattr(call, "function", None), "arguments", "{}")
                ),
            )
            for call in raw_calls
        ]
        assistant_message = {
            "role": "assistant",
            "content": getattr(message, "content", None),
        }
        if raw_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": getattr(call, "id", ""),
                    "type": "function",
                    "function": {
                        "name": getattr(getattr(call, "function", None), "name", ""),
                        "arguments": getattr(getattr(call, "function", None), "arguments", "{}"),
                    },
                }
                for call in raw_calls
            ]
        return ToolCompletion(
            content=getattr(message, "content", None) or "",
            usage=_usage_from_openai(self.model, getattr(response, "usage", None)),
            tool_calls=tool_calls,
            assistant_message=assistant_message,
        )

    async def _stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        input_tokens = output_tokens = 0
        params: dict[str, Any] = {
            "model": self.model,
            "messages": full_messages,
            "max_tokens": kwargs.get("max_tokens", 8192),
            "stream": True,
            **_temp_params(kwargs),
        }
        _apply_openai_reasoning(params, self.model, kwargs.get("reasoning_effort"))
        stream_cm = await self._create_completion(params)
        async with stream_cm as s:
            async for chunk in s:
                if not chunk.choices:
                    if chunk.usage:
                        input_tokens = chunk.usage.prompt_tokens
                        output_tokens = chunk.usage.completion_tokens
                    continue
                delta = chunk.choices[0].delta.content
                if not delta:
                    # Reasoning models stream chain-of-thought on a separate
                    # field; surface it so a thinking response isn't silent.
                    delta = getattr(chunk.choices[0].delta, "reasoning_content", None)
                if delta:
                    yield delta
                if chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens
                    output_tokens = chunk.usage.completion_tokens
        yield {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": calc_cost(self.model, input_tokens, output_tokens),
        }
