"""What every provider implements, and the shapes they exchange.

The three public methods check the spend cap and delegate to the ``_``-prefixed
implementation a provider supplies, so no provider can be reached without the
check and a new one inherits it.
"""

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from .cost import check_cost_limit

logger = logging.getLogger(__name__)


def _resolve_temperature(kwargs: dict) -> float | None:
    """Explicit call-site ``temperature`` kwarg, else the global
    ``LLM_TEMPERATURE`` env setting, else None (keep the provider's default).
    """
    temperature = kwargs.get("temperature")
    if temperature is not None:
        return float(temperature)
    from ...config import CONFIG

    return CONFIG.llm_temperature


def _temp_params(kwargs: dict) -> dict:
    """``{"temperature": t}`` when a temperature is configured, else ``{}``."""
    temperature = _resolve_temperature(kwargs)
    return {} if temperature is None else {"temperature": temperature}


def _ollama_options(kwargs: dict) -> dict:
    """Ollama takes sampling settings under ``options``, not at the top level."""
    temperature = _resolve_temperature(kwargs)
    return {} if temperature is None else {"options": {"temperature": temperature}}


class LLMProvider:
    """Base class for LLM providers.

    Every route to a paid model goes through one of the three public methods
    here, and each checks the spend limit before delegating to the provider's
    own ``_``-prefixed implementation. Subclasses implement those and inherit
    the check, so a new provider — or a new call site — cannot reach the model
    without it. The limit previously guarded three call sites in this class
    while the providers were reachable directly, which most callers did.
    """

    # ── Public surface: guarded, and not meant to be overridden ─────────────

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        """Returns (text, usage) where usage = {input_tokens, output_tokens, cost_usd}"""
        check_cost_limit()
        return await self._complete(messages, system, **kwargs)

    async def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> "ToolCompletion":
        check_cost_limit()
        return await self._complete_with_tools(messages, tools, system, **kwargs)

    async def stream(
        self, messages: list[dict], system: str = "", **kwargs: Any
    ) -> AsyncGenerator[str, None]:
        """Yield response chunks as they arrive.

        The check runs on first iteration rather than at call time, because
        that is when the request is actually made — and it is where a caller
        that builds the generator early still gets an honest answer.
        """
        check_cost_limit()
        async for chunk in self._stream(messages, system, **kwargs):
            yield chunk

    @classmethod
    def supports_streaming(cls) -> bool:
        """Whether this provider implements streaming.

        `hasattr(provider, "stream")` cannot answer this any more: `stream` is
        defined here, so every provider has one. What varies is whether
        `_stream` was overridden.
        """
        return cls._stream is not LLMProvider._stream

    @classmethod
    def supports_blocks(cls) -> bool:
        """Whether this provider takes list-of-block message content.

        Off by default, and the default is the safe answer: a provider that
        flattens content with `str()` would put a base64 payload into the prompt
        rather than drop it. Callers send `attachments.flatten` instead when this
        is False, so every provider names the file and reads text attachments —
        only the image itself needs the provider to say yes.
        """
        return False

    # ── Provider implementations ────────────────────────────────────────────

    async def _complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        raise NotImplementedError

    async def _complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> "ToolCompletion":
        raise NotImplementedError(f"{self.__class__.__name__} does not support tool calls")

    async def _stream(self, messages: list[dict], system: str = "", **kwargs):
        raise NotImplementedError(f"{self.__class__.__name__} does not support streaming")
        yield ""  # pragma: no cover - unreachable, makes this an async generator


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
