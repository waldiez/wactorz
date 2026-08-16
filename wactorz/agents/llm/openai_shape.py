"""Helpers for providers speaking the OpenAI wire format.

OpenAI, NIM and Ollama all accept OpenAI-shaped tool definitions and return
OpenAI-shaped messages, so the translation lives once here rather than three
times.
"""

import json
import logging
from typing import Any

from .attachments import NO_DOCUMENTS, UNREADABLE
from .pricing import calc_cost

logger = logging.getLogger(__name__)


def _openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
            },
        }
        for tool in tools
    ]


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _apply_openai_reasoning(
    params: dict[str, Any], model: str, reasoning_effort: str | None
) -> None:
    """Wire reasoning controls into an OpenAI-compatible request, in place.

    OpenAI-compatible reasoning endpoints (NVIDIA NIM, vLLM, SGLang, …) leave
    chain-of-thought ON by default. When the caller wants no reasoning
    (``reasoning_effort`` "none" or unset) we must *explicitly* disable it via
    the chat-template flag — otherwise a small ``max_tokens`` budget is spent
    entirely on hidden reasoning tokens and ``message.content`` comes back empty
    (this is why the NIM intent classifier always returned "OTHER"). When a real
    effort level is requested, pass it straight through. Mistral models on these
    endpoints reject both knobs, so they are skipped.
    """
    if "mistral" in model.lower():
        return
    if reasoning_effort in (None, "none"):
        extra = params.setdefault("extra_body", {})
        extra.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
    else:
        params["reasoning_effort"] = reasoning_effort


def _content_or_reasoning(message: Any) -> str:
    """Text of an OpenAI-compatible message, falling back to reasoning_content.

    Reasoning models can return an empty ``content`` while placing everything in
    ``reasoning_content`` (e.g. when thinking could not be disabled or the answer
    budget was exhausted). Surfacing the reasoning text is far better than
    returning an empty string that silently collapses to a default.
    """
    text = getattr(message, "content", None)
    if text:
        return text
    return getattr(message, "reasoning_content", None) or ""


def _usage_from_openai(model: str, usage_obj: Any) -> dict[str, Any]:
    input_tok = getattr(usage_obj, "prompt_tokens", 0) if usage_obj else 0
    output_tok = getattr(usage_obj, "completion_tokens", 0) if usage_obj else 0
    return {
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "cost_usd": calc_cost(model, input_tok, output_tok),
    }


def _openai_tool_result_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": message.get("tool_call_id") or message.get("id") or "",
        "name": message.get("name") or message.get("tool_name") or "",
        "content": str(message.get("content", "")),
    }


def openai_content(content: Any) -> Any:
    """Message content for the OpenAI wire format.

    Strings pass through untouched. A list of attachment blocks (the shape
    ``llm.attachments.to_blocks`` produces) becomes chat-completions parts:
    text stays text, an image becomes an ``image_url`` part carrying a data
    URL, and a document becomes a text marker — the format has no inline
    document part, and the name note ``to_blocks`` emits ahead of the block
    already tells the model what the file was called.

    Anything that is not one of those blocks is passed through as it stands.
    A list content is not always attachments: it can be parts this format
    already understands, so translating only what is recognisable is what keeps
    this from eating them.
    """
    if not isinstance(content, list):
        return content
    parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append({"type": "text", "text": str(block)})
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append({"type": "text", "text": str(block.get("text", ""))})
        elif block_type == "image":
            source = block.get("source") or {}
            data = source.get("data") or ""
            if data:
                media_type = source.get("media_type") or "image/png"
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{data}"},
                    }
                )
            else:
                parts.append({"type": "text", "text": UNREADABLE})
        elif block_type == "document":
            parts.append({"type": "text", "text": NO_DOCUMENTS})
        else:
            parts.append(block)
    return parts


def openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A message list with every content field run through :func:`openai_content`."""
    return [
        {**m, "content": openai_content(m.get("content"))} if isinstance(m, dict) else m
        for m in messages
    ]
