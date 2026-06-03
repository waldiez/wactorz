"""
LLMAgent - An actor backed by a Large Language Model.
Supports Anthropic Claude, OpenAI, Ollama (local), and custom providers.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from ..core.actor import Actor, Message, MessageType
from ..core.persistence import get_db

logger = logging.getLogger(__name__)


# ── Global cost limit ────────────────────────────────────────────────────────

def _period_key(period: str) -> str:
    now = datetime.now()
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    if period == "weekly":
        # ISO week (%G-W%V): weeks run Mon–Sun and never collapse into a
        # partial "W00" at the start of January the way %Y-W%W does.
        return now.strftime("%G-W%V")
    return now.strftime("%Y-%m")


def _global_cost_kv_key(period: str) -> str:
    return f"_global_cost_{_period_key(period)}"


def get_global_cost_info() -> dict:
    """Return current period spend and limit. Used by GET /api/cost."""
    from ..config import CONFIG
    db = get_db()
    # Runtime override (set via POST /api/cost/limit) takes priority over env var
    limit = CONFIG.llm_cost_limit_usd
    period = CONFIG.llm_cost_limit_period
    if db is not None:
        try:
            override = db.kv_get("_system", "_cost_limit_override")
            if isinstance(override, dict):
                limit = float(override.get("limit_usd", limit))
                period = override.get("period", period)
        except Exception:
            pass
    key = _global_cost_kv_key(period)
    spend = 0.0
    if db is not None:
        try:
            spend = float(db.kv_get("_system", key) or 0.0)
        except Exception:
            pass
    pct = round(spend / limit * 100, 1) if limit > 0 else None
    return {
        "period": period,
        "period_key": _period_key(period),
        "spend_usd": round(spend, 6),
        "limit_usd": limit if limit > 0 else None,
        "pct_used": pct,
        "limit_reached": limit > 0 and spend >= limit,
        "warning": limit > 0 and spend >= limit * 0.8,
    }


def set_cost_limit(limit_usd: float, period: str) -> None:
    """Persist a runtime cost limit override to SQLite."""
    if period not in ("daily", "weekly", "monthly"):
        raise ValueError(f"period must be daily, weekly, or monthly (got {period!r})")
    db = get_db()
    if db is None:
        raise RuntimeError("Database not available")
    db.kv_set("_system", "_cost_limit_override", {"limit_usd": limit_usd, "period": period})


def reset_global_cost() -> dict:
    """Clear accumulated spend for all periods. Returns new spend info."""
    db = get_db()
    if db is None:
        raise RuntimeError("Database not available")
    for period in ("daily", "weekly", "monthly"):
        db.kv_set("_system", _global_cost_kv_key(period), 0.0)
    return get_global_cost_info()


def _accumulate_global_cost(delta: float) -> None:
    if delta <= 0:
        return
    db = get_db()
    if db is None:
        return
    # Always accumulate, even when no limit is configured. Gating this on a
    # limit meant period spend stayed at $0 until a cap existed, so enabling a
    # cap mid-period gave false protection (spend already incurred was never
    # recorded) and the "Current spend (no limit set)" readout was always $0.
    for period in ("daily", "weekly", "monthly"):
        key = _global_cost_kv_key(period)
        try:
            current = float(db.kv_get("_system", key) or 0.0)
            db.kv_set("_system", key, round(current + delta, 6))
        except Exception as exc:
            logger.debug("[cost-limit] global accumulate failed (%s): %s", period, exc)


def _check_cost_limit() -> None:
    info = get_global_cost_info()
    if not info.get("limit_usd"):
        return
    if info["limit_reached"]:
        raise RuntimeError(
            f"LLM cost limit of ${info['limit_usd']:.2f} reached "
            f"for {info['period_key']}. Blocking further LLM calls."
        )
    if info["warning"]:
        logger.warning(
            "[cost-limit] %.1f%% of $%.2f %s budget used ($%.4f)",
            info["pct_used"], info["limit_usd"],
            info["period"], info["spend_usd"],
        )


# Fallback pricing per 1M tokens (input, output) in USD.
# Used when the dynamic fetch fails or the model isn't in the LiteLLM catalogue.
# Supports prefix matching so "gpt-5" covers "gpt-5-mini", etc.
_FALLBACK_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-4-6":        ( 5.00, 25.00),
    "claude-sonnet-4-6":      ( 3.00, 15.00),
    "claude-haiku-4-5":       ( 1.00,  5.00),
    # OpenAI
    "gpt-5.4-pro":            (30.00, 180.00),
    "gpt-5.4-mini":           ( 0.75,   4.50),
    "gpt-5.4-nano":           ( 0.20,   1.25),
    "gpt-5.4":                ( 2.50,  15.00),
    "gpt-5.2-pro":            (21.00, 168.00),
    "gpt-5.2-chat-latest":    ( 1.75,  14.00),
    "gpt-5.2-codex":          ( 1.75,  14.00),
    "gpt-5.2":                ( 1.75,  14.00),
    "gpt-5.1-codex-max":      ( 1.25,  10.00),
    "gpt-5.1-codex-mini":     ( 0.275,  2.20),
    "gpt-5.1-chat-latest":    ( 1.25,  10.00),
    "gpt-5.1-codex":          ( 1.25,  10.00),
    "gpt-5.1":                ( 1.25,  10.00),
    "gpt-5-search-api":       ( 1.25,  10.00),
    "gpt-5-pro":              (15.00, 120.00),
    "gpt-5-chat-latest":      ( 1.25,  10.00),
    "gpt-5-codex":            ( 1.25,  10.00),
    "gpt-5-mini":             ( 0.275,  2.20),
    "gpt-5-nano":             ( 0.055,  0.44),
    "gpt-5":                  ( 1.25,  10.00),
    "gpt-4o":                 ( 2.50, 10.00),
    "gpt-4o-mini":            ( 0.15,  0.60),
    "gpt-4-turbo":            (10.00, 30.00),
    # Ollama — local, no cost
    "ollama":                 ( 0.00,  0.00),
    # NVIDIA NIM — free tier covers most usage; paid tier pricing varies by model
    "nim/":                          ( 0.00,  0.00),
    # Google Gemini
    "gemini-2.5-flash-lite":         ( 0.10,  0.40),
    "gemini-2.0-flash":              ( 0.075, 0.30),
    "gemini-2.5-flash":              ( 0.30,  2.50),
    "gemini-3-flash":                ( 0.50,  3.00),
    "gemini-2.5-pro":                ( 1.25, 10.00),
    "gemini-3.1-pro":                ( 2.00, 12.00),
    "gemini-":                       ( 0.30,  2.50),
}

_LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main"
    "/model_prices_and_context_window.json"
)
_PRICING_TTL = 86400.0  # 24 hours

# Populated by _refresh_pricing(); maps exact model name → ($/1M input, $/1M output)
_dynamic_pricing: dict[str, tuple[float, float]] = {}
_dynamic_pricing_ts: float = 0.0
_pricing_fetch_in_progress: bool = False


async def _refresh_pricing() -> None:
    """Fetch latest model prices from LiteLLM's catalogue and cache them for 24 h."""
    global _dynamic_pricing, _dynamic_pricing_ts, _pricing_fetch_in_progress
    if time.time() - _dynamic_pricing_ts < _PRICING_TTL and _dynamic_pricing:
        return
    if _pricing_fetch_in_progress:
        return
    _pricing_fetch_in_progress = True
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _LITELLM_PRICING_URL,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
        pricing: dict[str, tuple[float, float]] = {}
        for model, info in data.items():
            if not isinstance(info, dict):
                continue
            inp = info.get("input_cost_per_token")
            out = info.get("output_cost_per_token")
            if inp is not None and out is not None:
                # Store as $/1M tokens to match _FALLBACK_PRICING units
                pricing[model] = (float(inp) * 1_000_000, float(out) * 1_000_000)
        _dynamic_pricing = pricing
        _dynamic_pricing_ts = time.time()
        logger.info("[pricing] Fetched %d model prices from LiteLLM", len(pricing))
    except Exception as exc:
        logger.warning("[pricing] Failed to fetch dynamic pricing: %s - using fallback", exc)
    finally:
        _pricing_fetch_in_progress = False


def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    # Exact match against live catalogue
    if model in _dynamic_pricing:
        price_in, price_out = _dynamic_pricing[model]
        return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
    # Prefix match against local fallback table
    key = next((k for k in _FALLBACK_PRICING if model.startswith(k)), None)
    if not key:
        return 0.0
    price_in, price_out = _FALLBACK_PRICING[key]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def pricing_info(model: str) -> dict[str, object]:
    """Return pricing source and rates for a model — useful for debugging."""
    if model in _dynamic_pricing:
        inp, out = _dynamic_pricing[model]
        age = time.time() - _dynamic_pricing_ts
        return {"source": "live", "input_per_1m": inp, "output_per_1m": out,
                "cache_age_s": round(age)}
    key = next((k for k in _FALLBACK_PRICING if model.startswith(k)), None)
    if key:
        inp, out = _FALLBACK_PRICING[key]
        return {"source": "fallback", "input_per_1m": inp, "output_per_1m": out,
                "matched_prefix": key}
    return {"source": "unknown", "input_per_1m": 0.0, "output_per_1m": 0.0}


class LLMProvider:
    """Base class for LLM providers."""

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        """Returns (text, usage) where usage = {input_tokens, output_tokens, cost_usd}"""
        raise NotImplementedError

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> "ToolCompletion":
        raise NotImplementedError(f"{self.__class__.__name__} does not support tool calls")


@dataclass
class ToolCall:
    """Provider-neutral LLM tool request."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCompletion:
    """Provider-neutral LLM response that may request tool execution."""

    content: str
    usage: dict[str, Any]
    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant_message: dict[str, Any] | None = None


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
        import json

        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _usage_from_openai(model: str, usage_obj: Any) -> dict[str, Any]:
    input_tok = getattr(usage_obj, "prompt_tokens", 0) if usage_obj else 0
    output_tok = getattr(usage_obj, "completion_tokens", 0) if usage_obj else 0
    return {
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "cost_usd": _calc_cost(model, input_tok, output_tok),
    }


def _openai_tool_result_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": message.get("tool_call_id") or message.get("id") or "",
        "name": message.get("name") or message.get("tool_name") or "",
        "content": str(message.get("content", "")),
    }


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str = "claude-sonnet-4-6", api_key: Optional[str] = None):
        import anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=kwargs.get("max_tokens", 4096),
            system=system,
            messages=messages,
        )
        text = response.content[0].text
        usage = {
            "input_tokens":  response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cost_usd":      _calc_cost(self.model,
                                        response.usage.input_tokens,
                                        response.usage.output_tokens),
        }
        return text, usage

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> ToolCompletion:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=kwargs.get("max_tokens", 4096),
            system=system,
            messages=self._anthropic_messages(messages),
            tools=self._anthropic_tools(tools),
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
            "cost_usd": _calc_cost(self.model, input_tok, output_tok),
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
                                "tool_use_id": message.get("tool_call_id") or message.get("id") or "",
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

    async def stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        input_tokens = output_tokens = 0
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=kwargs.get("max_tokens", 4096),
            system=system,
            messages=messages,
        ) as s:
            async for chunk in s.text_stream:
                yield chunk
            # Final message has usage counts
            final = await s.get_final_message()
            input_tokens  = final.usage.input_tokens
            output_tokens = final.usage.output_tokens
        yield {
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      _calc_cost(self.model, input_tokens, output_tokens),
        }


class OpenAIProvider(LLMProvider):
    def __init__(self, model: str = "gpt-4o", api_key: Optional[str] = None):
        import openai
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        params = {
            "model": self.model,
            "messages": full_messages,
            "max_completion_tokens": kwargs.get("max_tokens", 4096),
        }
        reasoning_effort = kwargs.get("reasoning_effort")
        if reasoning_effort:
            params["reasoning_effort"] = reasoning_effort
        try:
            response = await self.client.chat.completions.create(**params)
        except Exception as exc:
            if reasoning_effort and "reasoning_effort" in str(exc):
                params.pop("reasoning_effort", None)
                response = await self.client.chat.completions.create(**params)
            else:
                raise
        text = response.choices[0].message.content
        usage = {
            "input_tokens":  response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
            "cost_usd":      _calc_cost(self.model,
                                        response.usage.prompt_tokens,
                                        response.usage.completion_tokens),
        }
        return text, usage

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> ToolCompletion:
        full_messages = ([{"role": "system", "content": system}] if system else []) + [
            _openai_tool_result_message(m) if m.get("role") == "tool" else m
            for m in messages
        ]
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            tools=_openai_tools(tools),
            tool_choice=kwargs.get("tool_choice", "auto"),
            max_completion_tokens=kwargs.get("max_tokens", 4096),
        )
        message = response.choices[0].message
        raw_calls = getattr(message, "tool_calls", None) or []
        tool_calls = [
            ToolCall(
                id=getattr(call, "id", ""),
                name=getattr(getattr(call, "function", None), "name", ""),
                arguments=_parse_tool_arguments(getattr(getattr(call, "function", None), "arguments", "{}")),
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

    async def stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        input_tokens = output_tokens = 0
        params = {
            "model": self.model,
            "messages": full_messages,
            "max_completion_tokens": kwargs.get("max_tokens", 4096),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        reasoning_effort = kwargs.get("reasoning_effort")
        if reasoning_effort:
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
                    input_tokens  = chunk.usage.prompt_tokens
                    output_tokens = chunk.usage.completion_tokens
        yield {
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      _calc_cost(self.model, input_tokens, output_tokens),
        }


class OllamaProvider(LLMProvider):
    """Local LLM via Ollama."""
    def __init__(self, model: str = "llama3", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url

    @staticmethod
    def _chat_messages(messages: list[dict], system: str = "") -> list[dict]:
        if not system:
            return list(messages)
        return [{"role": "system", "content": system}] + list(messages)

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        import aiohttp
        payload = {
            "model": self.model,
            "messages": self._chat_messages(messages, system),
            "stream": False,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/api/chat", json=payload) as resp:
                data = await resp.json()
        text = data["message"]["content"]
        prompt_eval = data.get("prompt_eval_count", 0)
        eval_count  = data.get("eval_count", 0)
        usage = {"input_tokens": prompt_eval, "output_tokens": eval_count, "cost_usd": 0.0}
        return text, usage

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> ToolCompletion:
        import aiohttp

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
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/api/chat", json=payload) as resp:
                data = await resp.json()

        message = data.get("message") or {}
        raw_calls = message.get("tool_calls") or []
        tool_calls: list[ToolCall] = []
        for idx, call in enumerate(raw_calls):
            function = call.get("function", {}) if isinstance(call, dict) else {}
            tool_calls.append(
                ToolCall(
                    id=str(call.get("id") or f"ollama_tool_{idx}") if isinstance(call, dict) else f"ollama_tool_{idx}",
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

    async def stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        import aiohttp, json as _json
        payload = {
            "model": self.model,
            "messages": self._chat_messages(messages, system),
            "stream": True,
        }
        input_tokens = output_tokens = 0
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/api/chat", json=payload) as resp:
                async for raw in resp.content:
                    if not raw.strip():
                        continue
                    try:
                        data = _json.loads(raw)
                    except Exception:
                        continue
                    delta = (data.get("message") or {}).get("content", "")
                    if delta:
                        yield delta
                    if data.get("done"):
                        input_tokens  = data.get("prompt_eval_count", 0)
                        output_tokens = data.get("eval_count", 0)
        yield {"input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": 0.0}


class NIMProvider(LLMProvider):
    """
    NVIDIA NIM — OpenAI-compatible API hosted at integrate.api.nvidia.com.
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
        model:    str = "meta/llama-3.3-70b-instruct",
        api_key:  Optional[str] = None,
        base_url: str = NIM_BASE_URL,
    ):
        import openai
        self.model  = model
        self.client = openai.AsyncOpenAI(
            api_key=api_key or "dummy",   # NIM free tier may not require a key locally
            base_url=base_url,
        )

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            max_tokens=kwargs.get("max_tokens", 4096),
        )
        text = response.choices[0].message.content
        input_tok  = response.usage.prompt_tokens     if response.usage else 0
        output_tok = response.usage.completion_tokens if response.usage else 0
        usage = {
            "input_tokens":  input_tok,
            "output_tokens": output_tok,
            "cost_usd":      _calc_cost(self.model, input_tok, output_tok),
        }
        return text, usage

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> ToolCompletion:
        full_messages = ([{"role": "system", "content": system}] if system else []) + [
            _openai_tool_result_message(m) if m.get("role") == "tool" else m
            for m in messages
        ]
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=full_messages,
                tools=_openai_tools(tools),
                tool_choice=kwargs.get("tool_choice", "auto"),
                max_tokens=kwargs.get("max_tokens", 4096),
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
                arguments=_parse_tool_arguments(getattr(getattr(call, "function", None), "arguments", "{}")),
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

    async def stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        input_tokens = output_tokens = 0
        async with await self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            max_tokens=kwargs.get("max_tokens", 4096),
            stream=True,
        ) as s:
            async for chunk in s:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield delta
                if chunk.usage:
                    input_tokens  = chunk.usage.prompt_tokens
                    output_tokens = chunk.usage.completion_tokens
        yield {
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      _calc_cost(self.model, input_tokens, output_tokens),
        }


class GeminiProvider(LLMProvider):
    """
    Google Gemini via the official google-genai SDK.
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
        model:   str = "gemini-2.5-flash",
        api_key: Optional[str] = None,
    ):
        from google import genai
        from google.genai import types as genai_types

        self.model_name = model
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self._types = genai_types

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        contents = self._to_gemini_contents(messages)
        config = self._types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=kwargs.get("max_tokens", None),
        )

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=contents,
            config=config,
        )

        text = getattr(response, "text", "") or ""
        usage_meta = getattr(response, "usage_metadata", None)
        input_tokens = getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0
        output_tokens = getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0

        usage = {
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      _calc_cost(self.model_name, input_tokens, output_tokens),
        }
        return text, usage

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> ToolCompletion:
        contents = self._to_gemini_contents(messages)
        function_declarations = [self._gemini_function_declaration(tool) for tool in tools]
        config = self._types.GenerateContentConfig(
            system_instruction=system or None,
            tools=[self._types.Tool(function_declarations=function_declarations)],
            max_output_tokens=kwargs.get("max_tokens", None),
        )
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=contents,
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
                    call_id = str(getattr(function_call, "id", "") or f"gemini_tool_{len(tool_calls)}")
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
                "cost_usd": _calc_cost(self.model_name, input_tokens, output_tokens),
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

    async def stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        import asyncio
        import queue as _queue

        contents = self._to_gemini_contents(messages)
        config = self._types.GenerateContentConfig(system_instruction=system or None)

        # Stream via SDK in a thread, bridge to async via queue
        q: _queue.Queue = _queue.Queue()
        input_tokens = output_tokens = 0

        def _stream_thread():
            try:
                for chunk in self.client.models.generate_content_stream(
                    model=self.model_name,
                    contents=contents,
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

        while True:
            try:
                kind, value = await loop.run_in_executor(None, lambda: q.get(timeout=60))
            except Exception:
                break
            if kind == "done":
                break
            elif kind == "text":
                yield value
            elif kind == "usage":
                input_tokens  = value.prompt_token_count     or 0
                output_tokens = value.candidates_token_count or 0
            elif kind == "error":
                logger.error(f"[GeminiProvider] Stream error: {value}")
                break

        yield {
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      _calc_cost(self.model_name, input_tokens, output_tokens),
        }

    @staticmethod
    def _to_gemini_contents(messages: list[dict]) -> list[dict]:
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
            if isinstance(content, list):
                parts = content
            else:
                parts = [{"text": str(content)}]
            # Gemini uses "user" and "model" (not "assistant")
            gemini_role = "model" if role == "assistant" else "user"
            # Merge consecutive same-role messages (Gemini requires alternating)
            if contents and contents[-1]["role"] == gemini_role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": gemini_role, "parts": parts})
        return contents


class LLMAgent(Actor):
    """
    An Actor that uses an LLM to process tasks.
    Maintains conversation history and supports tool use.
    """

    def __init__(
        self,
        llm_provider: Optional[LLMProvider] = None,
        system_prompt: str = "You are a helpful AI agent.",
        max_history: int = 20,
        summarize_threshold: int = 30,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.llm = llm_provider
        self.system_prompt = system_prompt
        self.max_history = max_history
        self.summarize_threshold = summarize_threshold  # compress when history exceeds this
        self._conversation_history: list[dict] = []
        self._history_summary: str = ""   # rolling summary of compressed messages
        self._current_task = "idle"
        # Cost / token tracking — must be set here so subclasses (MainActor etc.) inherit them
        self.total_input_tokens  = 0
        self.total_output_tokens = 0
        self.total_cost_usd      = 0.0
        self._last_persisted_usd = 0.0

    def _current_task_description(self) -> str:
        return self._current_task

    async def on_start(self):
        _ = asyncio.create_task(_refresh_pricing())
        # Restore conversation history and rolling summary from persistence
        saved = self.recall("conversation_history", [])
        clean = []
        for m in saved:
            if not isinstance(m, dict):
                continue
            role    = m.get("role", "")
            content = m.get("content", "")
            if role not in ("user", "assistant"):
                continue
            if not isinstance(content, str):
                content = str(content)
            if content.strip():
                entry: dict = {"role": role, "content": content}
                if "ts" in m and isinstance(m["ts"], (int, float)):
                    entry["ts"] = m["ts"]
                clean.append(entry)
        self._conversation_history = clean[-self.max_history:]
        self._history_summary = self.recall("history_summary", "")

        # Restore lifetime cost so heartbeats carry accurate totals after restart
        saved_cost = self.recall("_final_cost", {})
        if isinstance(saved_cost, dict):
            self.total_input_tokens  += saved_cost.get("input_tokens", 0)
            self.total_output_tokens += saved_cost.get("output_tokens", 0)
            self.total_cost_usd      += saved_cost.get("cost_usd", 0.0)
        # Align persisted baseline so global cost doesn't re-add lifetime spend on first
        # _persist_cost() after restart.
        self._last_persisted_usd = self.total_cost_usd

        # Migration: if _messages_processed key doesn't exist yet, seed from
        # conversation_history so the overview counter isn't always 0 on first
        # start after upgrading. messages_processed was set from SQLite in
        # actor.start() — if it's still 0 here, the key was absent.
        if self.metrics.messages_processed == 0 and self._conversation_history:
            user_turns = sum(1 for m in self._conversation_history if m.get("role") == "user")
            self.metrics.messages_processed = user_turns

        # Publish capability manifest so main's topic registry knows this agent exists
        description = (
            getattr(self, "DESCRIPTION", None)
            or (self.__class__.__doc__ or "").strip().split("\n")[0]
            or self.name
        )
        capabilities  = getattr(self, "CAPABILITIES", [])
        input_schema  = getattr(self, "INPUT_SCHEMA",  {})
        output_schema = getattr(self, "OUTPUT_SCHEMA", {})
        await self.publish_manifest(
            description=description,
            capabilities=capabilities,
            input_schema=input_schema,
            output_schema=output_schema,
        )

    async def on_stop(self):
        self.persist("conversation_history", self._conversation_history)
        self.persist("history_summary", self._history_summary)

    async def _maybe_summarize(self):
        """
        If history exceeds summarize_threshold, compress the oldest half into a
        rolling summary and keep only the most recent max_history messages.
        The summary is prepended as a system-style context message when sending
        to the LLM so no facts are lost.
        """
        if len(self._conversation_history) < self.summarize_threshold:
            return
        if self.llm is None:
            # No LLM — just truncate
            self._conversation_history = self._conversation_history[-self.max_history:]
            return

        # Split: compress the older half, keep the recent half
        split = len(self._conversation_history) // 2
        to_compress = self._conversation_history[:split]
        to_keep     = self._conversation_history[split:]

        # Build compression prompt
        prior_summary = f"Previous summary:\n{self._history_summary}\n\n" if self._history_summary else ""
        messages_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:400]}"
            for m in to_compress
        )
        prompt = (
            f"{prior_summary}"
            f"Summarize the following conversation segment concisely. "
            f"Preserve: key facts, decisions, user preferences, entity names, URLs, credentials, "
            f"any technical details mentioned. Be specific, not vague.\n\n"
            f"{messages_text}"
        )
        try:
            summary, usage = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You are a conversation summarizer. Output a dense, factual summary. No preamble.",
                max_tokens=400,
            )
            self.total_input_tokens  += usage.get("input_tokens", 0)
            self.total_output_tokens += usage.get("output_tokens", 0)
            self.total_cost_usd      += usage.get("cost_usd", 0.0)
            self._persist_cost()
            self._history_summary = summary.strip()
            self._conversation_history = to_keep
            self.persist("history_summary", self._history_summary)
            self.persist("conversation_history", self._conversation_history)
            logger.info(f"[{self.name}] History summarized: {len(to_compress)} messages → summary ({len(summary)} chars), keeping {len(to_keep)}")
        except Exception as e:
            logger.warning(f"[{self.name}] Summarization failed: {e} — truncating instead")
            self._conversation_history = self._conversation_history[-self.max_history:]

    def _build_messages_with_summary(self, n: int) -> list[dict]:
        """
        Build the message list to send to the LLM, prepending the rolling summary
        as context if one exists.
        """
        recent = self._conversation_history[-n:]
        if not self._history_summary:
            return recent
        # Inject summary as a user/assistant exchange so it fits the messages format
        summary_ctx = [{
            "role": "user",
            "content": f"[Context from earlier in our conversation]\n{self._history_summary}"
        }, {
            "role": "assistant",
            "content": "Understood, I have that context."
        }]
        return summary_ctx + recent

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            await self._handle_task(msg)

    async def _handle_task(self, msg: Message):
        if isinstance(msg.payload, dict):
            # Accept "text", "task", "message", or fall back to JSON dump
            task_text = (
                msg.payload.get("text")
                or msg.payload.get("task")
                or msg.payload.get("message")
                or msg.payload.get("query")
                or str(msg.payload)
            )
        else:
            task_text = str(msg.payload) if msg.payload is not None else ""
        self._current_task = task_text[:60]

        if self.llm is None:
            logger.warning(f"[{self.name}] No LLM provider configured.")
            return

        try:
            _check_cost_limit()
        except RuntimeError as e:
            payload_dict = msg.payload if isinstance(msg.payload, dict) else {}
            task_id  = payload_dict.get("_task_id")
            reply_to = payload_dict.get("_reply_to") or msg.reply_to or msg.sender_id
            if reply_to:
                result = {"text": str(e), "task": task_text}
                if task_id:
                    result["_task_id"] = task_id
                await self.send(reply_to, MessageType.RESULT, result)
            return

        start = time.time()
        try:
            self._conversation_history.append({"role": "user", "content": task_text, "ts": start})

            safe_history = [
                {"role": m["role"], "content": str(m["content"])}
                for m in self._conversation_history[-self.max_history:]
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            ]
            response, usage = await self.llm.complete(
                messages=safe_history,
                system=self.system_prompt,
            )

            self._conversation_history.append({"role": "assistant", "content": response, "ts": time.time()})
            self.metrics.tasks_completed += 1
            duration = time.time() - start

            self.total_input_tokens  += usage.get("input_tokens", 0)
            self.total_output_tokens += usage.get("output_tokens", 0)
            self.total_cost_usd      += usage.get("cost_usd", 0.0)

            # Persist after each exchange
            self.persist("conversation_history", self._conversation_history)
            self._persist_cost()

            # Publish completion
            await self._mqtt_publish(
                f"agents/{self.actor_id}/completed",
                {
                    "result_preview": response[:200],
                    "duration": duration,
                    "task": task_text[:60],
                },
            )

            # Reply to sender — echo _task_id so send_to() futures resolve
            payload_dict = msg.payload if isinstance(msg.payload, dict) else {}
            task_id  = payload_dict.get("_task_id")
            reply_to = payload_dict.get("_reply_to") or msg.reply_to or msg.sender_id
            if reply_to:
                result = {"text": response, "task": task_text, "duration": duration}
                if task_id:
                    result["_task_id"] = task_id
                await self.send(reply_to, MessageType.RESULT, result)

        except Exception as e:
            self.metrics.tasks_failed += 1
            self.state_value = "failed_task"
            logger.error(f"[{self.name}] LLM task failed: {e}", exc_info=True)

        finally:
            self._current_task = "idle"

    async def chat(self, user_message: str) -> str:
        """Direct async call - useful for the main conversation actor."""
        if self.llm is None:
            return "[No LLM configured]"
        try:
            _check_cost_limit()
        except RuntimeError as e:
            return str(e)

        self.metrics.messages_processed += 1
        ts_user = time.time()
        self._conversation_history.append({"role": "user", "content": user_message, "ts": ts_user})

        safe_history = [
            {"role": m["role"], "content": str(m["content"])}
            for m in self._build_messages_with_summary(self.max_history)
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and m.get("content") is not None
        ]
        response, usage = await self.llm.complete(
            messages=safe_history,
            system=self.system_prompt,
        )
        ts_reply = time.time()
        self._conversation_history.append({"role": "assistant", "content": response, "ts": ts_reply})
        await self._maybe_summarize()
        self.persist("conversation_history", self._conversation_history)
        self._log_chat_turn(user_message, response, ts_user=ts_user, ts_reply=ts_reply)

        # Accumulate token usage and cost
        self.total_input_tokens  += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd      += usage.get("cost_usd", 0.0)
        self._persist_cost()

        await self._mqtt_publish(
            f"agents/{self.actor_id}/metrics",
            self._build_metrics(),
        )
        return response

    async def chat_stream(self, user_message: str):
        """
        Streaming version of chat(). Yields text chunks, then a final usage dict.
        The caller is responsible for printing chunks as they arrive.

        Usage:
            async for chunk in agent.chat_stream("hello"):
                if isinstance(chunk, dict):
                    usage = chunk   # final usage summary
                else:
                    print(chunk, end="", flush=True)
        """
        try:
            _check_cost_limit()
        except RuntimeError as e:
            yield str(e)
            return

        if self.llm is None or not hasattr(self.llm, "stream"):
            # Fallback: non-streaming — yield whole response as single chunk
            response = await self.chat(user_message)
            yield response
            return

        self.metrics.messages_processed += 1
        self._conversation_history.append({"role": "user", "content": user_message, "ts": time.time()})

        full_text = []
        usage     = {}

        safe_history = [
            {"role": m["role"], "content": str(m["content"])}
            for m in self._build_messages_with_summary(self.max_history)
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and m.get("content") is not None
        ]
        try:
            async for chunk in self.llm.stream(
                messages=safe_history,
                system=self.system_prompt,
            ):
                if isinstance(chunk, dict):
                    usage = chunk
                else:
                    full_text.append(chunk)
                    yield chunk
        except BaseException:
            # Interrupted mid-stream (Ctrl+C, task cancellation, network error).
            # Persist whatever chunks arrived so neither the user message nor
            # the partial response are lost on restart.
            if full_text:
                partial = "".join(full_text)
                self._conversation_history.append({"role": "assistant", "content": partial})
                self.persist("conversation_history", self._conversation_history)
            raise

        response = "".join(full_text)
        self._conversation_history.append({"role": "assistant", "content": response, "ts": time.time()})
        await self._maybe_summarize()
        self.persist("conversation_history", self._conversation_history)

        self.total_input_tokens  += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd      += usage.get("cost_usd", 0.0)
        self._persist_cost()

        await self._mqtt_publish(
            f"agents/{self.actor_id}/metrics",
            self._build_metrics(),
        )

        # Yield final usage dict so caller can log it
        yield usage

    def _log_chat_turn(self, user_msg: str, reply: str,
                       ts_user: float, ts_reply: float) -> None:
        """Write both halves of a turn to SQLite chat_log and InfluxDB (if enabled)."""
        db = get_db()
        if db is not None:
            try:
                db.log_chat(self.name, "user",      user_msg, ts=ts_user,  session_id=self.actor_id)
                db.log_chat(self.name, "assistant",  reply,   ts=ts_reply, session_id=self.actor_id)
            except Exception as exc:
                logger.debug("[%s] chat_log SQLite write failed: %s", self.name, exc)
        try:
            from ..monitoring.influx import write_chat as _influx_chat
            _influx_chat(self.name, "user",      user_msg, ts=ts_user)
            _influx_chat(self.name, "assistant",  reply,   ts=ts_reply)
        except Exception as exc:
            logger.debug("[%s] chat_log InfluxDB write failed: %s", self.name, exc)

    def _persist_cost(self):
        """Write lifetime cost to durable SQLite storage after each exchange."""
        delta = self.total_cost_usd - self._last_persisted_usd
        if delta > 0:
            _accumulate_global_cost(delta)
            self._last_persisted_usd = self.total_cost_usd
        self.persist("_final_cost", {
            "input_tokens":  self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "cost_usd":      round(self.total_cost_usd, 6),
            "name":          self.name,
        })

    def _build_metrics(self) -> dict:
        m = super()._build_metrics()
        m["input_tokens"]  = self.total_input_tokens
        m["output_tokens"] = self.total_output_tokens
        m["cost_usd"]      = round(self.total_cost_usd, 6)
        return m

    def clear_history(self):
        self._conversation_history = []
