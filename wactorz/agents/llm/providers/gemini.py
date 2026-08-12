"""GeminiProvider — Google Gemini, whose SDK is sync-only."""

import asyncio
import logging
import queue
from typing import Any

from ..attachments import UNREADABLE
from ..base import LLMProvider, ToolCall, ToolCompletion, _temp_params
from ..pricing import calc_cost

logger = logging.getLogger(__name__)


_GEMINI_STREAM_STALL_TIMEOUT = 60


def _gemini_parts(blocks: list[Any]) -> list[dict[str, Any]]:
    """Attachment blocks (the ``llm.attachments.to_blocks`` shape) as Gemini parts.

    Text stays text; images and documents become ``inline_data`` — Gemini reads
    both, PDFs included, so unlike the OpenAI-shaped providers nothing needs to
    be dropped to a marker here.

    ⚠ Anything that is not an attachment block is passed through untouched. A
    list content is not always attachments: this provider's own tool loop feeds
    an assistant turn back as Gemini parts (``{"text": ...}``,
    ``{"function_call": ...}``), and translating those would drop the tool call
    while the ``function_response`` answering it stayed — leaving a request
    Gemini cannot make sense of.
    """
    parts: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            parts.append({"text": str(block)})
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append({"text": str(block.get("text", ""))})
        elif block_type in ("image", "document"):
            source = block.get("source") or {}
            data = source.get("data") or ""
            if data:
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": source.get("media_type") or "application/octet-stream",
                            "data": data,
                        }
                    }
                )
            else:
                parts.append({"text": UNREADABLE})
        else:
            parts.append(block)
    return parts


class GeminiProvider(LLMProvider):
    """Google Gemini via the official google-genai SDK.
    Install: pip install google-genai

    Recommended models (March 2026):
      gemini-2.5-flash-lite   — cheapest ($0.10/$0.40 per 1M tokens), fast, free tier
      gemini-2.0-flash        — fast & capable ($0.10/$0.40), free tier available
      gemini-2.5-flash        — hybrid reasoning ($0.30/$2.50), free tier available
      gemini-2.5-pro          — best for coding & complex tasks ($1.25/$10.00)
      gemini-3.1-pro          — flagship ($2.00/$12.00), no free tier

    Get a free API key at: https://aistudio.google.com
    Note: Pro models charge 2x for prompts >200K tokens.
    """

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        api_key: str | None = None,
    ):
        from google import genai
        from google.genai import types as genai_types

        self.model_name = model
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self._types = genai_types

    @classmethod
    def supports_blocks(cls) -> bool:
        """Block content is translated to Gemini parts (``inline_data``) in
        ``_to_gemini_contents``, so a list content is safe here.
        """
        return True

    async def _complete(
        self, messages: list[dict[str, Any]], system: str = "", **kwargs
    ) -> tuple[str, dict[str, Any]]:
        contents = self._to_gemini_contents(messages)
        config = self._types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=kwargs.get("max_tokens"),
            **_temp_params(kwargs),
        )

        # client.aio is the SDK's async surface; the sync `client.models` call
        # would block the shared event loop for the whole round-trip, starving
        # every other actor (and MQTT keepalive) until the model replies.
        response = await self.client.aio.models.generate_content(
            model=self.model_name,
            contents=contents,  # pyright: ignore[reportArgumentType]
            config=config,
        )

        text = getattr(response, "text", "") or ""
        usage_meta = getattr(response, "usage_metadata", None)
        input_tokens = getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0
        output_tokens = getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0

        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": calc_cost(self.model_name, input_tokens, output_tokens),
        }
        return text, usage

    async def _complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> ToolCompletion:
        contents = self._to_gemini_contents(messages)
        function_declarations = [self._gemini_function_declaration(tool) for tool in tools]
        config = self._types.GenerateContentConfig(
            system_instruction=system or None,
            tools=[self._types.Tool(function_declarations=function_declarations)],
            max_output_tokens=kwargs.get("max_tokens"),
            **_temp_params(kwargs),
        )
        # client.aio is the SDK's async surface; the sync `client.models` call
        # would block the shared event loop for the whole round-trip, starving
        # every other actor (and MQTT keepalive) until the model replies.
        response = await self.client.aio.models.generate_content(
            model=self.model_name,
            contents=contents,  # pyright: ignore[reportArgumentType]
            config=config,
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", []) or []:
                text = getattr(part, "text", None)
                if text:
                    text_parts.append(text)
                function_call = getattr(part, "function_call", None)
                if function_call:
                    call_id = str(
                        getattr(function_call, "id", "") or f"gemini_tool_{len(tool_calls)}"
                    )
                    args = getattr(function_call, "args", {}) or {}
                    tool_calls.append(
                        ToolCall(
                            id=call_id,
                            name=str(getattr(function_call, "name", "")),
                            arguments=args if isinstance(args, dict) else dict(args),
                        )
                    )
        usage_meta = getattr(response, "usage_metadata", None)
        input_tokens = getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0
        output_tokens = getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0
        assistant_parts: list[dict[str, Any]] = []
        if text_parts:
            assistant_parts.append({"text": "".join(text_parts)})
        for call in tool_calls:
            assistant_parts.append(
                {"function_call": {"id": call.id, "name": call.name, "args": call.arguments}}
            )
        return ToolCompletion(
            content="".join(text_parts).strip(),
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": calc_cost(self.model_name, input_tokens, output_tokens),
            },
            tool_calls=tool_calls,
            assistant_message={"role": "assistant", "content": assistant_parts},
        )

    def _gemini_function_declaration(self, tool: dict[str, Any]) -> Any:
        return self._types.FunctionDeclaration(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters=tool.get("parameters", {"type": "object", "properties": {}}),
        )

    async def _stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        contents = self._to_gemini_contents(messages)
        config = self._types.GenerateContentConfig(
            system_instruction=system or None, **_temp_params(kwargs)
        )

        # Stream via SDK in a thread, bridge to async via queue
        q: queue.Queue[tuple[str, Any]] = queue.Queue()
        input_tokens = output_tokens = 0

        def _stream_thread():
            try:
                for chunk in self.client.models.generate_content_stream(
                    model=self.model_name,
                    contents=contents,  # pyright: ignore[reportArgumentType]
                    config=config,
                ):
                    text = getattr(chunk, "text", "")
                    if text:
                        q.put(("text", text))
                    usage_metadata = getattr(chunk, "usage_metadata", None)
                    if usage_metadata:
                        q.put(("usage", usage_metadata))
            except Exception as e:
                q.put(("error", str(e)))
            finally:
                q.put(("done", None))

        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _stream_thread)

        error: str | None = None
        while True:
            try:
                kind, value = await loop.run_in_executor(
                    None, lambda: q.get(timeout=_GEMINI_STREAM_STALL_TIMEOUT)
                )
            except queue.Empty:
                # Only Empty is an expected outcome here. A bare `except` also
                # caught bugs in this loop and ended the stream as if the model
                # had simply finished.
                error = f"stream stalled: nothing received for {_GEMINI_STREAM_STALL_TIMEOUT}s"
                logger.warning("[GeminiProvider] %s", error)
                break
            if kind == "done":
                break
            elif kind == "text":
                yield value
            elif kind == "usage":
                input_tokens = value.prompt_token_count or 0
                output_tokens = value.candidates_token_count or 0
            elif kind == "error":
                error = str(value)
                logger.error(f"[GeminiProvider] Stream error: {value}")
                break

        # Text that arrived is real work and is delivered either way, but the
        # final item says whether the model actually finished. Without this a
        # truncated reply is indistinguishable from a complete one, so the chat
        # log, the feed and any retry logic all treat half an answer as whole.
        # Usage is reported regardless: tokens spent were spent.
        final: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": calc_cost(self.model_name, input_tokens, output_tokens),
        }
        if error:
            final["error"] = error
        yield final

    @staticmethod
    def _to_gemini_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-style messages to Gemini contents format."""
        contents = []
        for m in messages:
            role = m.get("role", "user")
            if role == "tool":
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "function_response": {
                                    "id": m.get("tool_call_id") or m.get("id") or "",
                                    "name": m.get("name") or m.get("tool_name") or "",
                                    "response": {"result": str(m.get("content", ""))},
                                }
                            }
                        ],
                    }
                )
                continue
            content = m.get("content", "")
            parts = (
                _gemini_parts(content) if isinstance(content, list) else [{"text": str(content)}]
            )
            # Gemini uses "user" and "model" (not "assistant")
            gemini_role = "model" if role == "assistant" else "user"
            # Merge consecutive same-role messages (Gemini requires alternating)
            if contents and contents[-1]["role"] == gemini_role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": gemini_role, "parts": parts})
        return contents
