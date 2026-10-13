"""AG2 1.x beside Wactorz: its agents on the system's model, their spend on the dashboard.

An AG2 agent takes a model configuration of AG2's own. :func:`model_config`
builds one from the model Wactorz runs on, so ``LLM_PROVIDER`` and its key
drive the AG2 agents too. After a reply, :func:`record_reply` reports what
it cost to the actor that ran it, through
:meth:`~wactorz.core.actor.Actor.record_llm_cost`::

    from wactorz.core.integrations.ag2 import model_config, record_reply

    writer = Agent("writer", "You improve drafts.", config=model_config(me))
    reply = await writer.ask(text)
    await record_reply(me, reply, prices=PRICES)

AG2 reports tokens by model, not money, so ``prices`` maps a model name to
dollars per million input and output tokens; the name AG2 reports is the one
the provider resolved to, so a listed family name matches it as a prefix. An
unpriced model is counted at no cost. Written for AG2 1.x (``import ag2``);
the 0.x line, ``import autogen``, is a different library.
"""

import os
from typing import Any

from .pricing import Prices, cost_of

__all__ = ["Prices", "cost_of", "model_config", "record_reply"]

NVIDIA_NIM_URL = "https://integrate.api.nvidia.com/v1"

#: The provider classes :func:`model_config` knows how to map; any other gives ``None``.
KNOWN_PROVIDERS = frozenset(
    {"AnthropicProvider", "OpenAIProvider", "OllamaProvider", "NIMProvider", "GeminiProvider"}
)


def _config_classes() -> tuple[Any, Any, Any]:
    """AG2's Anthropic, Gemini and OpenAI configuration classes, imported on first use.

    Importing this module must not need AG2, so a program can be written
    against the bridge and still start on a machine without it; only building
    a configuration does. Each class is a stand-in that fails on construction
    until its provider's extra (``ag2[anthropic]``, ``ag2[openai]``, ...) is
    installed, and :func:`_build` turns that into a plain message.
    """
    try:
        # Optional dependency: `pip install 'wactorz[ag2]'` is not required to import this module.
        from ag2.config import (  # pyright: ignore[reportMissingImports]
            AnthropicConfig,
            GeminiConfig,
            OpenAIConfig,
        )
    except ImportError as exc:
        raise RuntimeError(
            "AG2 is not installed. Install it with: pip install 'wactorz[ag2]'"
        ) from exc
    return AnthropicConfig, GeminiConfig, OpenAIConfig


def model_config(actor: Any) -> Any:
    """AG2's model configuration for the model ``actor`` runs on, or ``None`` without one.

    Reads the provider the system gave the actor (``me.llm``), so the agents
    an AG2 program builds answer with the same model, key and endpoint as
    everything else. Anthropic and Gemini use AG2's clients of the same name;
    OpenAI, NVIDIA NIM and Ollama go through the OpenAI protocol, Ollama at
    its ``/v1`` endpoint with no key. The settings come from where the
    provider took them: the environment and the system's configuration.
    """
    # Only here: the configuration module reads the environment at import.
    from wactorz.config import CONFIG

    provider = getattr(actor.llm, "provider", actor.llm)
    kind = type(provider).__name__
    if kind not in KNOWN_PROVIDERS:
        return None
    AnthropicConfig, GeminiConfig, OpenAIConfig = _config_classes()
    model = str(getattr(provider, "model", "") or "")
    key = CONFIG.llm_api_key
    if kind == "AnthropicProvider":
        return _build(
            "anthropic",
            AnthropicConfig,
            model=model,
            api_key=os.environ.get("ANTHROPIC_API_KEY") or key,
        )
    if kind == "OpenAIProvider":
        return _build(
            "openai",
            OpenAIConfig,
            model=model,
            api_key=os.environ.get("OPENAI_API_KEY") or key,
            base_url=CONFIG.openai_url or None,
        )
    if kind == "OllamaProvider":
        base = str(getattr(provider, "base_url", "") or CONFIG.ollama_url).rstrip("/")
        return _build("openai", OpenAIConfig, model=model, base_url=f"{base}/v1", api_key="ollama")
    if kind == "NIMProvider":
        return _build(
            "openai",
            OpenAIConfig,
            model=model,
            base_url=NVIDIA_NIM_URL,
            api_key=CONFIG.nim_api_key or CONFIG.nvidia_api_key or key,
        )
    if kind == "GeminiProvider":
        return _build(
            "gemini",
            GeminiConfig,
            model=model,
            api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or key,
        )
    raise AssertionError(f"unmapped provider {kind}")  # KNOWN_PROVIDERS and the chain disagree


def _build(extra: str, config_class: Any, **settings: Any) -> Any:
    """One of AG2's configurations, or a plain error naming the extra it needs.

    AG2 talks to each provider through that provider's SDK, and asks for it
    through an extra of its own with a version floor of its own: a class whose
    SDK is absent, or too old, is a stand-in that fails on construction.
    """
    try:
        return config_class(**settings)
    except Exception as exc:
        raise RuntimeError(
            f"AG2's {extra} client is not usable ({exc}). Install it with: "
            f"pip install 'ag2[{extra}]'"
        ) from exc


def usage_totals(report: Any, prices: Prices | None = None) -> tuple[float, int, int, str]:
    """Cost, input tokens, output tokens and the model, from an AG2 usage report.

    The report's per-model breakdown is priced model by model; its total is
    what the tokens are read from, so a run with no records still counts.
    """
    prices = prices or {}
    by_model = dict(getattr(report, "by_model", {}) or {})
    cost = 0.0
    for model, usage in by_model.items():
        cost += cost_of(
            prices,
            str(model),
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
        )
    total = getattr(report, "total", report)
    input_tokens = int(getattr(total, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(total, "completion_tokens", 0) or 0)
    model = ", ".join(str(m) for m in by_model) if by_model else ""
    return cost, input_tokens, output_tokens, model


async def record_reply(actor: Any, reply: Any, *, prices: Prices | None = None) -> float:
    """Report to ``actor`` what producing ``reply`` cost. Returns that cost.

    A reply's usage covers the whole run that produced it: every model call
    of the agent, its tools and its follow-ups on that conversation. Call it
    once per reply you keep, with the conversation's last reply covering the
    turns before it.
    """
    report = await reply.usage()
    cost, input_tokens, output_tokens, model = usage_totals(report, prices)
    if cost or input_tokens or output_tokens:
        actor.record_llm_cost(
            cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider="ag2",
        )
    return cost
