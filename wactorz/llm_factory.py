"""Per-call-site LLM provider resolution.

The global ``LLM_PROVIDER`` / ``LLM_MODEL`` pair selects one model for every
LLM call in the system. ``LLM_OVERRIDES`` refines that per *call site*, so e.g.
intent classification can run on a small local model while the planner stays on
a hosted one:

    LLM_OVERRIDES="intent=ollama:qwen3:4b,planner=anthropic:claude-sonnet-4-6"

Format: comma-separated ``<site>=<provider>[:<model>]`` entries. Only the first
colon splits provider from model, so Ollama tags with colons (``qwen3:4b``)
pass through intact. A site without an override keeps the default (global)
provider, as does an entry whose provider fails to construct — an override must
never take the system down.

Known sites:

| Site       | Call it configures                                          |
|------------|-------------------------------------------------------------|
| ``main``   | MainActor conversation + history summarization              |
| ``intent`` | Intent classification (ACTUATE / HA / PIPELINE / OTHER)     |
| ``planner``| PlannerAgent pipeline planning and code generation          |
| ``actuator``| OneOffActuatorAgent direct device control                  |
| ``ha``     | HomeAssistantAgent internal classification                  |
| ``dynamic``| ``get_llm()`` shim inside LLM-generated DynamicAgent code   |
"""

from __future__ import annotations

import logging
import os

from .agents.llm_agent import LLMProvider
from .config import CONFIG

logger = logging.getLogger(__name__)

# Provider instances cached per spec string so repeated spawns (planner,
# actuator) reuse one client instead of re-constructing it per request.
_provider_cache: dict[str, LLMProvider] = {}


def parse_overrides(raw: str) -> dict[str, str]:
    """Parse ``site=provider[:model],...`` into {site: spec}. Malformed entries
    are skipped with a warning rather than raising — a typo in one entry must
    not disable the others.
    """
    table: dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        site, sep, spec = entry.partition("=")
        site, spec = site.strip(), spec.strip()
        if not sep or not site or not spec:
            logger.warning("[llm-overrides] Skipping malformed entry %r", entry)
            continue
        table[site] = spec
    return table


def create_provider(provider_name: str, model: str | None = None) -> LLMProvider | None:
    """Construct an LLM provider by name, using the same env-var fallbacks as
    the global provider in ``build_system``. Returns None for ``none``/empty.
    Raises ValueError for an unknown provider name.
    """
    from .agents.llm_agent import (
        AnthropicProvider,
        GeminiProvider,
        NIMProvider,
        OllamaProvider,
        OpenAIProvider,
    )

    name = (provider_name or "").strip().lower()
    if name in ("", "none"):
        return None
    if name == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY") or CONFIG.llm_api_key
        return AnthropicProvider(model=model or CONFIG.llm_model, api_key=api_key)
    if name == "openai":
        api_key = os.getenv("OPENAI_API_KEY") or CONFIG.llm_api_key
        return OpenAIProvider(
            model=model or CONFIG.llm_model, api_key=api_key, base_url=CONFIG.openai_url or None
        )
    if name == "ollama":
        return OllamaProvider(model=model or CONFIG.llm_model, base_url=CONFIG.ollama_url)
    if name == "nim":
        return NIMProvider(
            model=model or CONFIG.llm_model,
            api_key=CONFIG.nim_api_key or CONFIG.nvidia_api_key or CONFIG.llm_api_key,
        )
    if name == "gemini":
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or CONFIG.llm_api_key
        return GeminiProvider(
            model=model or CONFIG.llm_model or "gemini-2.5-flash", api_key=api_key
        )
    raise ValueError(f"Unknown LLM provider: {provider_name!r}")


def _provider_from_spec(spec: str) -> LLMProvider | None:
    cached = _provider_cache.get(spec)
    if cached is not None:
        return cached
    provider_name, _, model = spec.partition(":")
    provider = create_provider(provider_name, model or None)
    if provider is not None:
        _provider_cache[spec] = provider
    return provider


def provider_for(
    site: str, default: LLMProvider | None = None, overrides: dict[str, str] | None = None
) -> LLMProvider | None:
    """Provider for a call site: the ``LLM_OVERRIDES`` entry for ``site`` if one
    exists and constructs cleanly, else ``default`` (the global provider).
    ``overrides`` bypasses the environment for tests.
    """
    table = overrides if overrides is not None else parse_overrides(CONFIG.llm_overrides)
    spec = table.get(site)
    if not spec:
        return default
    try:
        provider = _provider_from_spec(spec)
    except Exception as exc:
        logger.warning(
            "[llm-overrides] site '%s' → %r failed (%s) — using default provider",
            site,
            spec,
            exc,
        )
        return default
    # An explicit "site=none" disables the LLM for that site.
    return provider


def reset_provider_cache() -> None:
    """Drop cached provider instances (tests / config reload)."""
    _provider_cache.clear()
