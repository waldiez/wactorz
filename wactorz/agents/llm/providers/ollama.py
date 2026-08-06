"""OllamaProvider — a local model over HTTP."""

import json
import logging
from typing import Any

import aiohttp

from ..base import LLMProvider, ToolCall, ToolCompletion, _ollama_options
from ..openai_shape import _openai_tools, _parse_tool_arguments

logger = logging.getLogger(__name__)


_OLLAMA_TIMEOUT = aiohttp.ClientTimeout(total=600, sock_connect=10)
_OLLAMA_STREAM_TIMEOUT = aiohttp.ClientTimeout(sock_connect=10, sock_read=120)


class OllamaProvider(LLMProvider):
    """Local LLM via Ollama."""

    def __init__(self, model: str = "llama3", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url

    @staticmethod
    def _chat_messages(messages: list[dict], system: str = "") -> list[dict]:
        if not system:
            return list(messages)
        return [{"role": "system", "content": system}, *list(messages)]

    async def _complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "messages": self._chat_messages(messages, system),
            "stream": False,
            **_ollama_options(kwargs),
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/api/chat", json=payload, timeout=_OLLAMA_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        text = (data.get("message") or {}).get("content") or ""
        prompt_eval = data.get("prompt_eval_count", 0)
        eval_count = data.get("eval_count", 0)
        usage = {"input_tokens": prompt_eval, "output_tokens": eval_count, "cost_usd": 0.0}
        return text, usage

    async def _complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> ToolCompletion:
        ollama_messages = []
        for message in messages:
            if message.get("role") == "tool":
                ollama_messages.append(
                    {
                        "role": "tool",
                        "content": str(message.get("content", "")),
                        "tool_name": message.get("name") or message.get("tool_name") or "",
                    }
                )
            else:
                ollama_messages.append(message)
        payload = {
            "model": self.model,
            "messages": self._chat_messages(ollama_messages, system),
            "stream": False,
            "tools": _openai_tools(tools),
            **_ollama_options(kwargs),
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/api/chat", json=payload, timeout=_OLLAMA_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        message = data.get("message") or {}
        raw_calls = message.get("tool_calls") or []
        tool_calls: list[ToolCall] = []
        for idx, call in enumerate(raw_calls):
            function = call.get("function", {}) if isinstance(call, dict) else {}
            tool_calls.append(
                ToolCall(
                    id=str(call.get("id") or f"ollama_tool_{idx}")
                    if isinstance(call, dict)
                    else f"ollama_tool_{idx}",
                    name=str(function.get("name") or ""),
                    arguments=_parse_tool_arguments(function.get("arguments") or {}),
                )
            )
        usage = {
            "input_tokens": data.get("prompt_eval_count", 0),
            "output_tokens": data.get("eval_count", 0),
            "cost_usd": 0.0,
        }
        assistant_message = {
            "role": "assistant",
            "content": message.get("content", ""),
        }
        if raw_calls:
            assistant_message["tool_calls"] = raw_calls
        return ToolCompletion(
            content=message.get("content", "") or "",
            usage=usage,
            tool_calls=tool_calls,
            assistant_message=assistant_message,
        )

    async def _stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        payload = {
            "model": self.model,
            "messages": self._chat_messages(messages, system),
            "stream": True,
            **_ollama_options(kwargs),
        }
        input_tokens = output_tokens = 0
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/api/chat", json=payload, timeout=_OLLAMA_STREAM_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                async for raw in resp.content:
                    if not raw.strip():
                        continue
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    delta = (data.get("message") or {}).get("content", "")
                    if delta:
                        yield delta
                    if data.get("done"):
                        input_tokens = data.get("prompt_eval_count", 0)
                        output_tokens = data.get("eval_count", 0)
        yield {"input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": 0.0}
