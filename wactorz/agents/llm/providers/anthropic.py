"""AnthropicProvider — Claude, via the anthropic SDK."""

import logging
from typing import Any

from ..base import LLMProvider, ToolCall, ToolCompletion, _temp_params
from ..pricing import calc_cost

logger = logging.getLogger(__name__)

# Anthropic removed the sampling parameters on Claude Opus 4.7 and has kept them
# out of every model released since: `temperature` is a 400, not a field the API
# ignores. Substring rather than exact match, so dated and platform-prefixed ids
# ("anthropic.claude-opus-5", "claude-opus-5-20260401") answer like the alias.
_NO_SAMPLING_PARAMS: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-",
    "claude-mythos-",
)

# Models that turned out to reject `temperature` at runtime. The list above
# cannot name a model released after it was written, so the first request on one
# pays a 400 and lands here; every later request in the process skips the
# parameter instead of paying that 400 again.
_learned_no_sampling: set[str] = set()


def _rejects_temperature(model: str) -> bool:
    """Whether this model refuses `temperature` outright."""
    name = model.lower()
    return model in _learned_no_sampling or any(family in name for family in _NO_SAMPLING_PARAMS)


def _is_temperature_rejection(exc: Exception) -> bool:
    """Whether `exc` is the API refusing to accept `temperature` at all.

    Narrower than "the message mentions temperature": a value the model would
    accept in a different range is a real configuration error and must surface,
    not be papered over by a retry that quietly drops the setting.
    """
    if getattr(exc, "status_code", None) != 400:
        return False
    message = str(exc).lower()
    if "temperature" not in message:
        return False
    return any(
        phrase in message
        for phrase in ("deprecated", "not supported", "unsupported", "unexpected", "removed")
    )


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None):
        import anthropic

        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    # ── Request shaping ─────────────────────────────────────────────────────

    def _request_params(self, messages: list[dict], system: str, kwargs: dict) -> dict[str, Any]:
        """The common request body, with `temperature` only where it is legal."""
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": kwargs.get("max_tokens", 16384),
            "system": system,
            "messages": messages,
        }
        if not _rejects_temperature(self.model):
            params.update(_temp_params(kwargs))
        return params

    def _retry_without_temperature(self, exc: Exception, params: dict[str, Any]) -> bool:
        """Whether dropping `temperature` gives this request a second chance.

        Mutates `params` when it does, and remembers the model so the rest of
        the process omits the parameter from the start.
        """
        if "temperature" not in params or not _is_temperature_rejection(exc):
            return False
        params.pop("temperature")
        _learned_no_sampling.add(self.model)
        logger.warning(
            "[anthropic] %s does not accept `temperature` (%s); retrying without it "
            "and omitting it for the rest of this process",
            self.model,
            exc,
        )
        return True

    async def _create(self, params: dict[str, Any]):
        """`messages.create`, retried once without `temperature` if rejected."""
        try:
            return await self.client.messages.create(**params)  # pyright: ignore[reportCallIssue,reportArgumentType]
        except Exception as exc:  # re-raised unless it is the one case we handle
            if not self._retry_without_temperature(exc, params):
                raise
        return await self.client.messages.create(**params)  # pyright: ignore[reportCallIssue,reportArgumentType]

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
        response = await self._create(self._request_params(messages, system, kwargs))
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
        params = self._request_params(self._anthropic_messages(messages), system, kwargs)
        params["tools"] = self._anthropic_tools(tools)
        response = await self._create(params)
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
        params = self._request_params(messages, system, kwargs)
        started = False
        try:
            async for item in self._stream_once(params):
                started = True
                yield item
            return
        except Exception as exc:  # re-raised unless it is the one case we handle
            # The rejection lands on the opening request, before the first
            # chunk, so retrying cannot replay output the caller already saw.
            # `started` keeps that true even if a later release moves the error.
            if started or not self._retry_without_temperature(exc, params):
                raise
        async for item in self._stream_once(params):
            yield item

    async def _stream_once(self, params: dict[str, Any]):
        """One streaming attempt, from the request to the closing usage dict."""
        input_tokens = output_tokens = 0
        async with self.client.messages.stream(**params) as s:  # pyright: ignore[reportCallIssue,reportArgumentType]
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
