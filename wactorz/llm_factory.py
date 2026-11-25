# SPDX-License-Identifier: Apache-2.0
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

import logging
import os
from collections.abc import Callable

from .agents.llm.providers.anthropic import AnthropicProvider
from .agents.llm.providers.fake import INTENTS, FakeProvider, parse_script
from .agents.llm.providers.gemini import DEFAULT_MODEL as GEMINI_DEFAULT_MODEL
from .agents.llm.providers.gemini import GeminiProvider
from .agents.llm.providers.nim import NIMProvider
from .agents.llm.providers.ollama import OllamaProvider
from .agents.llm.providers.openai import OpenAIProvider
from .agents.llm_agent import LLMProvider
from .config import CONFIG, _unquote

logger: logging.Logger = logging.getLogger(__name__)

# Provider instances cached per spec string so repeated spawns (planner,
# actuator) reuse one client instead of re-constructing it per request.
_provider_cache: dict[str, LLMProvider] = {}

# Every site `provider_for` is called with. An override naming anything else
# configures nothing, so it is worth saying so at startup rather than leaving
# the operator to wonder why their model never took effect.
KNOWN_SITES: frozenset[str] = frozenset({"main", "intent", "planner", "actuator", "ha", "dynamic"})


def parse_overrides(raw: str | None) -> dict[str, str]:
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


# Each builder takes the model and the key to fall back on when the provider's
# own variable is unset: ``LLM_API_KEY`` for the system's call sites, nothing
# for an agent asking for a provider other than the system's (`agent_provider`).


def _build_anthropic(model: str | None, shared_key: str) -> LLMProvider:
    return AnthropicProvider(
        model=model or CONFIG.llm_model,
        api_key=os.getenv("ANTHROPIC_API_KEY") or shared_key,
    )


def _build_openai(model: str | None, shared_key: str) -> LLMProvider:
    return OpenAIProvider(
        model=model or CONFIG.llm_model,
        api_key=os.getenv("OPENAI_API_KEY") or shared_key,
        # Empty means "the public API"; the client rejects an empty string.
        base_url=CONFIG.openai_url or None,
    )


def _build_ollama(model: str | None, shared_key: str) -> LLMProvider:
    return OllamaProvider(model=model or CONFIG.llm_model, base_url=CONFIG.ollama_url)


def _build_nim(model: str | None, shared_key: str) -> LLMProvider:
    return NIMProvider(
        model=model or CONFIG.llm_model,
        api_key=CONFIG.nim_api_key or CONFIG.nvidia_api_key or shared_key,
    )


def _build_gemini(model: str | None, shared_key: str) -> LLMProvider:
    return GeminiProvider(
        # `CONFIG.llm_model` always has a value, so the trailing default only
        # answers an explicitly empty LLM_MODEL. It is not a way to catch one
        # holding another provider's model name — nothing here can tell.
        model=model or CONFIG.llm_model or GEMINI_DEFAULT_MODEL,
        api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or shared_key,
    )


def _build_fake(model: str | None, shared_key: str) -> LLMProvider:
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
_PROVIDERS: dict[str, Callable[[str | None, str], LLMProvider]] = {
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


class UnknownProviderError(ValueError):
    """An LLM provider name this release has no provider for."""

    def __init__(self, provider_name: str) -> None:
        super().__init__(
            f"Unknown LLM provider: {provider_name!r} (known: {', '.join(sorted(_PROVIDERS))})"
        )


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
        raise UnknownProviderError(provider_name)
    return build(model, CONFIG.llm_api_key)


# The extra that brings each provider's SDK, for a refusal to name.
_EXTRAS: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "nim": "openai",
    "gemini": "google",
}


class ProviderUnavailableError(ValueError):
    """A provider an agent asked for that cannot be built here, with the reason."""

    @classmethod
    def no_model(cls, provider: str) -> "ProviderUnavailableError":
        """No model named: another provider's default model would be a guess."""
        return cls(f"A model is needed with provider {provider!r}")

    @classmethod
    def unknown(cls, provider: str) -> "ProviderUnavailableError":
        """A provider name this release has no provider for."""
        return cls(f"Unknown LLM provider: {provider!r} (known: {', '.join(sorted(_PROVIDERS))})")

    @classmethod
    def no_key(cls, provider: str, variables: tuple[str, ...]) -> "ProviderUnavailableError":
        """The provider's own key variable is unset."""
        return cls(f"No key for provider {provider!r}: set {' or '.join(variables)}")

    @classmethod
    def not_installed(cls, provider: str, extra: str) -> "ProviderUnavailableError":
        """The provider's SDK is not installed."""
        return cls(f"Provider {provider!r} needs its SDK: pip install 'wactorz[{extra}]'")

    @classmethod
    def failed(cls, provider: str, exc: Exception) -> "ProviderUnavailableError":
        """The provider's client refused to be built, for its own reason."""
        return cls(f"Provider {provider!r} could not be built: {exc}")

    @classmethod
    def on_node(cls) -> "ProviderUnavailableError":
        """Asked for on a node, which makes no model calls of its own."""
        return cls("An agent on a node has no model: a node makes no model calls")


def _anthropic_key_variables(shared_key: str) -> tuple[str, ...]:
    """The variables Anthropic's client can read a credential from, if none is set.

    The client is built without a key and fails on its first request, so the
    check is made here, where it can say which variable to set.
    """
    if shared_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return ()
    return ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def agent_provider(provider_name: str, model: str) -> LLMProvider:
    """A provider for one agent's own choice of model, or the reason there is none.

    Keys and endpoints come from the environment the system already reads,
    never from the caller, since a spawn config may be written by a model.
    ``LLM_API_KEY`` is the key of the system's provider (``LLM_PROVIDER``), so it
    is used only when that is the provider asked for: another provider gets its
    own variable or nothing, never the system's key sent to another service.

    Raises :class:`ProviderUnavailableError` (a ``ValueError``) with the reason
    when the provider cannot be built, rather than falling back to the
    system's model and its price.
    """
    name = (provider_name or "").strip().lower()
    build = _PROVIDERS.get(name)
    if build is None:
        raise ProviderUnavailableError.unknown(provider_name)
    model = (model or "").strip()
    if not model:
        raise ProviderUnavailableError.no_model(name)
    shared_key = CONFIG.llm_api_key if name == CONFIG.llm_provider.strip().lower() else ""
    if name == "anthropic" and (missing := _anthropic_key_variables(shared_key)):
        raise ProviderUnavailableError.no_key(name, missing)
    try:
        return build(model, shared_key)
    except ImportError as exc:
        raise ProviderUnavailableError.not_installed(name, _EXTRAS.get(name, name)) from exc
    except Exception as exc:
        raise ProviderUnavailableError.failed(name, exc) from exc


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
