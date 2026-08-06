"""AnthropicProvider — Claude, via the anthropic SDK."""

import logging
from typing import Any

from ..base import LLMProvider, ToolCall, ToolCompletion, _temp_params
from ..pricing import calc_cost

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None):
        import anthropic

        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    @staticmethod
    def _extract_text(response) -> str:
        """Join every text block, skipping thinking / redacted_thinking and any
        other non-text block. Robust whether or not extended thinking is enabled;
        without this, content[0] is a ThinkingBlock and .text raises.
        """
        parts = []
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", "") or "")
        return "".join(parts).strip()

    async def _complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        response = await self.client.messages.create(  # pyright: ignore[reportCallIssue]
            model=self.model,
            max_tokens=kwargs.get("max_tokens", 16384),
            system=system,
            messages=messages,
            **_temp_params(kwargs),
        )
        text = self._extract_text(response)
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cost_usd": calc_cost(
                self.model, response.usage.input_tokens, response.usage.output_tokens
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
        response = await self.client.messages.create(  # pyright: ignore[reportCallIssue]
            model=self.model,
            max_tokens=kwargs.get("max_tokens", 16384),
            system=system,
            messages=self._anthropic_messages(messages),  # pyright: ignore[reportArgumentType]
            tools=self._anthropic_tools(tools),  # pyright: ignore[reportArgumentType]
            **_temp_params(kwargs),
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        assistant_content: list[dict[str, Any]] = []
        for block in getattr(response, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text = getattr(block, "text", "")
                text_parts.append(text)
                assistant_content.append({"type": "text", "text": text})
            elif btype == "tool_use":
                name = getattr(block, "name", "")
                call_id = getattr(block, "id", "")
                args = getattr(block, "input", {}) or {}
                if not isinstance(args, dict):
                    args = {}
                tool_calls.append(ToolCall(id=call_id, name=name, arguments=args))
                assistant_content.append(
                    {"type": "tool_use", "id": call_id, "name": name, "input": args}
                )
        usage_obj = getattr(response, "usage", None)
        input_tok = getattr(usage_obj, "input_tokens", 0) if usage_obj else 0
        output_tok = getattr(usage_obj, "output_tokens", 0) if usage_obj else 0
        usage = {
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cost_usd": calc_cost(self.model, input_tok, output_tok),
        }
        return ToolCompletion(
            content="".join(text_parts).strip(),
            usage=usage,
            tool_calls=tool_calls,
            assistant_message={"role": "assistant", "content": assistant_content},
        )

    @staticmethod
    def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
            }
            for tool in tools
        ]

    @staticmethod
    def _anthropic_messages(messages: list[dict]) -> list[dict]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.get("tool_call_id")
                                or message.get("id")
                                or "",
                                "content": str(message.get("content", "")),
                                "is_error": bool(message.get("is_error")),
                            }
                        ],
                    }
                )
                continue
            content = message.get("content", "")
            converted.append(
                {
                    "role": "assistant" if role == "assistant" else "user",
                    "content": content if isinstance(content, list) else str(content),
                }
            )
        return converted

    async def _stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        input_tokens = output_tokens = 0
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=kwargs.get("max_tokens", 16384),
            system=system,
            messages=messages,
            **_temp_params(kwargs),
        ) as s:
            async for chunk in s.text_stream:
                yield chunk
            # Final message has usage counts
            final = await s.get_final_message()
            input_tokens = final.usage.input_tokens
            output_tokens = final.usage.output_tokens
        yield {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": calc_cost(self.model, input_tokens, output_tokens),
        }
