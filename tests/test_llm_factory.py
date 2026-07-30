"""Tests for per-call-site LLM provider overrides (wactorz/llm_factory.py)."""

import pytest

from wactorz.agents.llm_agent import OllamaProvider
from wactorz.llm_factory import (
    create_provider,
    parse_overrides,
    provider_for,
    reset_provider_cache,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_provider_cache()
    yield
    reset_provider_cache()


# ── parse_overrides ──────────────────────────────────────────────────────────


def test_parse_empty():
    assert parse_overrides("") == {}
    assert parse_overrides(None) == {}


def test_parse_single_entry():
    assert parse_overrides("intent=ollama:llama3") == {"intent": "ollama:llama3"}


def test_parse_multiple_entries_with_whitespace():
    raw = " intent=ollama:llama3 , planner=anthropic:claude-sonnet-4-6 "
    assert parse_overrides(raw) == {
        "intent": "ollama:llama3",
        "planner": "anthropic:claude-sonnet-4-6",
    }


def test_parse_ollama_tag_keeps_colons():
    # Only the site= split happens here; the spec keeps its colons intact.
    assert parse_overrides("intent=ollama:qwen3:4b") == {"intent": "ollama:qwen3:4b"}


def test_parse_skips_malformed_entries():
    assert parse_overrides("intent=ollama:llama3,garbage,=x,planner=") == {
        "intent": "ollama:llama3"
    }


# ── create_provider ──────────────────────────────────────────────────────────


def test_create_provider_none():
    assert create_provider("none") is None
    assert create_provider("") is None


def test_create_provider_unknown_raises():
    with pytest.raises(ValueError):
        create_provider("magicllm")


def test_create_provider_ollama_model_with_tag():
    provider = create_provider("ollama", "qwen3:4b")
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "qwen3:4b"


def test_create_provider_ollama_default_model():
    from wactorz.config import CONFIG

    provider = create_provider("ollama")
    assert provider.model == CONFIG.llm_model


# ── provider_for ─────────────────────────────────────────────────────────────


def test_no_override_returns_default():
    default = object()
    assert provider_for("intent", default, overrides={}) is default


def test_override_builds_site_provider():
    provider = provider_for("intent", None, overrides={"intent": "ollama:llama3"})
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "llama3"


def test_override_spec_splits_on_first_colon_only():
    provider = provider_for("intent", None, overrides={"intent": "ollama:qwen3:4b"})
    assert provider.model == "qwen3:4b"


def test_override_provider_is_cached_across_sites():
    overrides = {"intent": "ollama:llama3", "actuator": "ollama:llama3"}
    first = provider_for("intent", None, overrides=overrides)
    second = provider_for("actuator", None, overrides=overrides)
    assert first is second


def test_bad_override_falls_back_to_default():
    default = object()
    assert provider_for("intent", default, overrides={"intent": "magicllm:x"}) is default


def test_override_none_disables_site_llm():
    default = object()
    assert provider_for("intent", default, overrides={"intent": "none"}) is None
