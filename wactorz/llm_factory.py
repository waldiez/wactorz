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

Surrounding quotes are stripped from the value and from each site and spec, so
a value quoted as a whole behaves the same however it was set. An entry naming
a site not in the table below is skipped with a warning: nothing would read it,
and a silent no-op is indistinguishable from an override that did not work.

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
from collections.abc import Callable

from .agents.llm.providers.anthropic import AnthropicProvider
from .agents.llm.providers.fake import INTENTS, FakeProvider, parse_script
from .agents.llm.providers.gemini import GeminiProvider
from .agents.llm.providers.nim import NIMProvider
from .agents.llm.providers.ollama import OllamaProvider
from .agents.llm.providers.openai import OpenAIProvider
from .agents.llm_agent import LLMProvider
from .config import CONFIG, _unquote

logger = logging.getLogger(__name__)

# Provider instances cached per spec string so repeated spawns (planner,
# actuator) reuse one client instead of re-constructing it per request.
_provider_cache: dict[str, LLMProvider] = {}

# Every site `provider_for` is called with. An override naming anything else
# configures nothing, so it is worth saying so at startup rather than leaving
# the operator to wonder why their model never took effect.
KNOWN_SITES: frozenset[str] = frozenset({"main", "intent", "planner", "actuator", "ha", "dynamic"})


def parse_overrides(raw: str) -> dict[str, str]:
    """Parse ``site=provider[:model],...`` into {site: spec}. Malformed entries
    are skipped with a warning rather than raising — a typo in one entry must
    not disable the others.
    """
    table: dict[str, str] = {}
    for entry in _unquote(raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        site, sep, spec = entry.partition("=")
        site, spec = _unquote(site), _unquote(spec)
        if not sep or not site or not spec:
            logger.warning("[llm-overrides] Skipping malformed entry %r", entry)
            continue
        if site not in KNOWN_SITES:
            logger.warning(
                "[llm-overrides] Ignoring entry for unknown site %r — nothing reads it "
                "(known sites: %s)",
                site,
                ", ".join(sorted(KNOWN_SITES)),
            )
            continue
        table[site] = spec
    return table


def _build_anthropic(model: str | None) -> LLMProvider:
    return AnthropicProvider(
        model=model or CONFIG.llm_model,
        api_key=os.getenv("ANTHROPIC_API_KEY") or CONFIG.llm_api_key,
    )


def _build_openai(model: str | None) -> LLMProvider:
    return OpenAIProvider(
        model=model or CONFIG.llm_model,
        api_key=os.getenv("OPENAI_API_KEY") or CONFIG.llm_api_key,
        # Empty means "the public API"; the client rejects an empty string.
        base_url=CONFIG.openai_url or None,
    )


def _build_ollama(model: str | None) -> LLMProvider:
    return OllamaProvider(model=model or CONFIG.llm_model, base_url=CONFIG.ollama_url)


def _build_nim(model: str | None) -> LLMProvider:
    return NIMProvider(
        model=model or CONFIG.llm_model,
        api_key=CONFIG.nim_api_key or CONFIG.nvidia_api_key or CONFIG.llm_api_key,
    )


def _build_gemini(model: str | None) -> LLMProvider:
    return GeminiProvider(
        # `CONFIG.llm_model` always has a value, so the trailing default only
        # answers an explicitly empty LLM_MODEL. It is not a way to catch one
        # holding another provider's model name — nothing here can tell.
        model=model or CONFIG.llm_model or "gemini-2.5-flash",
        api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or CONFIG.llm_api_key,
    )


def _build_fake(model: str | None) -> LLMProvider:
    """The deterministic provider, configured from the environment.

    Registered like any other backend so it is reached through the same factory
    and the same spend accounting rather than by patching something at runtime.
    ``LLM_FAKE_SCRIPT`` is a JSON object of substring → reply and
    ``LLM_FAKE_INTENT`` names what the classifier is told; both are optional and
    the defaults answer every request.
    """
    intent = (os.getenv("LLM_FAKE_INTENT") or "OTHER").strip().upper()
    return FakeProvider(
        model=model or CONFIG.llm_model,
        script=parse_script(os.getenv("LLM_FAKE_SCRIPT", "")),
        intent=intent if intent in INTENTS else "OTHER",
    )


# Provider name → constructor. Adding a backend means adding its module under
# ``agents/llm/providers/`` and one entry here; nothing else selects on name.
_PROVIDERS: dict[str, Callable[[str | None], LLMProvider]] = {
    "anthropic": _build_anthropic,
    "openai": _build_openai,
    "ollama": _build_ollama,
    "nim": _build_nim,
    "gemini": _build_gemini,
    # Answers deterministically and never calls out. Shipped rather than kept in
    # the test tree so an end-to-end run reaches it the way a user would, and so
    # the dashboard is usable without an API key. It is not a degraded model —
    # it does not think at all, and `docs/` says so where the option is listed.
    "fake": _build_fake,
}


def create_provider(provider_name: str, model: str | None = None) -> LLMProvider | None:
    """Construct an LLM provider by name, using the same env-var fallbacks as
    the global provider in ``build_system``. Returns None for ``none``/empty.
    Raises ValueError for an unknown provider name.
    """
    name = (provider_name or "").strip().lower()
    if name in ("", "none"):
        return None
    build = _PROVIDERS.get(name)
    if build is None:
        raise ValueError(
            f"Unknown LLM provider: {provider_name!r} (known: {', '.join(sorted(_PROVIDERS))})"
        )
    return build(model)


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
