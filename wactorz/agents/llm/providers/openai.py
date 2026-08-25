"""OpenAIProvider — the OpenAI SDK, and the wire format others reuse."""

import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    # The shaping helpers are provider-agnostic and stay free of the SDK, so the
    # SDK's own parameter types are attached here, at the one call that needs them.
    from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolUnionParam

from ..base import LLMProvider, ToolCall, ToolCompletion, _temp_params
from ..openai_shape import (
    _openai_tool_result_message,
    _openai_tools,
    _parse_tool_arguments,
    _usage_from_openai,
    openai_messages,
)
from ..pricing import calc_cost

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    def __init__(
        self, model: str = "gpt-4o", api_key: str | None = None, base_url: str | None = None
    ):
        import openai

        # Two calls rather than a conditional `**{...}`: unpacking a dict of
        # unknown keys means a type checker cannot tell which parameter each
        # value lands on, so it reports the conflict against every one of them
        # — eight errors for a line that was always correct.
        self.client = (
            openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
            if base_url
            else openai.AsyncOpenAI(api_key=api_key)
        )
        self.model = model
        self.base_url = base_url or None

    @classmethod
    def supports_blocks(cls) -> bool:
        """Block content is translated to this format's parts on the way out
        (see ``openai_shape.openai_content``), so a list content is safe here.
        """
        return True

    async def _complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        full_messages = (
            [{"role": "system", "content": system}] if system else []
        ) + openai_messages(messages)
        params = {
            "model": self.model,
            "messages": full_messages,
            "max_completion_tokens": kwargs.get("max_tokens", 16384),
            **_temp_params(kwargs),
        }
        reasoning_effort = kwargs.get("reasoning_effort")
        if (
            (reasoning_effort == "none" or reasoning_effort is None)
            and self.base_url is not None
            and "mistral" not in self.model.lower()
        ):
            params["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        if reasoning_effort and "mistral" not in self.model.lower():
            params["reasoning_effort"] = reasoning_effort
        try:
            response = await self.client.chat.completions.create(**params)
        except Exception as exc:
            if reasoning_effort and "reasoning_effort" in str(exc):
                params.pop("reasoning_effort", None)
                response = await self.client.chat.completions.create(**params)
            else:
                raise
        text = response.choices[0].message.content or ""
        usage = {
            "input_tokens": response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
            "cost_usd": calc_cost(
                self.model, response.usage.prompt_tokens, response.usage.completion_tokens
            ),
        }
        return text, usage

    async def _complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> ToolCompletion:
        full_messages = (
            [{"role": "system", "content": system}] if system else []
        ) + openai_messages(
            [_openai_tool_result_message(m) if m.get("role") == "tool" else m for m in messages]
        )
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=cast("list[ChatCompletionMessageParam]", full_messages),
            tools=cast("list[ChatCompletionToolUnionParam]", _openai_tools(tools)),
            tool_choice=kwargs.get("tool_choice", "auto"),
            max_completion_tokens=kwargs.get("max_tokens", 16384),
            **_temp_params(kwargs),
        )
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
        full_messages = (
            [{"role": "system", "content": system}] if system else []
        ) + openai_messages(messages)
        input_tokens = output_tokens = 0
        params = {
            "model": self.model,
            "messages": full_messages,
            "max_completion_tokens": kwargs.get("max_tokens", 16384),
            "stream": True,
            "stream_options": {"include_usage": True},
            **_temp_params(kwargs),
        }
        reasoning_effort = kwargs.get("reasoning_effort")
        if reasoning_effort and "mistral" not in self.model.lower():
            params["reasoning_effort"] = reasoning_effort
        try:
            stream = await self.client.chat.completions.create(**params)
        except Exception as exc:
            if reasoning_effort and "reasoning_effort" in str(exc):
                params.pop("reasoning_effort", None)
                stream = await self.client.chat.completions.create(**params)
            else:
                raise
        async with stream as s:
            async for chunk in s:
                delta = chunk.choices[0].delta.content if chunk.choices else None
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


# A host that never completes the connection is dead and should fail fast, but
# Ollama is usually a LAN box that can legitimately think for minutes on a large
# prompt — so the whole-request ceiling is deliberately generous. Streaming gets
# no ceiling at all: a long answer is not a fault, and a total would cut off
# healthy generations. What means something there is silence, so the gap between
# chunks is bounded instead.
