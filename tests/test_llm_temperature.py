"""Tests for LLM_TEMPERATURE plumbing (config → providers)."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from tests.optional_deps import ensure_importable

# Not `sys.modules.setdefault`: that asks whether openai has been imported yet,
# not whether it can be, so on a machine where it *is* installed the empty stub
# won wherever this module was imported first — shadowing the real package for
# the rest of the process.
ensure_importable("openai")

from wactorz.agents.llm.base import _ollama_options, _resolve_temperature, _temp_params
from wactorz.agents.llm_agent import OllamaProvider
from wactorz.config import CONFIG, _env_opt_float


def _with_temp(value):
    """CONFIG patched to a given llm_temperature."""
    return patch("wactorz.config.CONFIG", replace(CONFIG, llm_temperature=value))


# ── config parsing ───────────────────────────────────────────────────────────


def test_env_opt_float_unset_and_empty(monkeypatch):
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    assert _env_opt_float("LLM_TEMPERATURE") is None
    monkeypatch.setenv("LLM_TEMPERATURE", "  ")
    assert _env_opt_float("LLM_TEMPERATURE") is None


def test_env_opt_float_parses_values(monkeypatch):
    monkeypatch.setenv("LLM_TEMPERATURE", "0")
    assert _env_opt_float("LLM_TEMPERATURE") == 0.0
    monkeypatch.setenv("LLM_TEMPERATURE", "0.7")
    assert _env_opt_float("LLM_TEMPERATURE") == 0.7


# ── resolution precedence ────────────────────────────────────────────────────


def test_resolve_prefers_call_site_over_config():
    with _with_temp(0.7):
        assert _resolve_temperature({"temperature": 0.0}) == 0.0


def test_resolve_falls_back_to_config():
    with _with_temp(0.3):
        assert _resolve_temperature({}) == 0.3


def test_resolve_none_when_unconfigured():
    with _with_temp(None):
        assert _resolve_temperature({}) is None


def test_zero_temperature_is_honored_not_treated_as_unset():
    with _with_temp(0.0):
        assert _resolve_temperature({}) == 0.0
        assert _temp_params({}) == {"temperature": 0.0}


# ── request parameter shaping ────────────────────────────────────────────────


def test_temp_params_omitted_when_unconfigured():
    with _with_temp(None):
        assert _temp_params({}) == {}


def test_ollama_options_nests_under_options():
    with _with_temp(0.2):
        assert _ollama_options({}) == {"options": {"temperature": 0.2}}


def test_ollama_options_empty_when_unconfigured():
    with _with_temp(None):
        assert _ollama_options({}) == {}


# ── end-to-end through a provider payload ────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def raise_for_status(self):
        """No-op: this fake only ever stands in for a 200."""

    async def json(self):
        return {"message": {"content": "ok"}, "prompt_eval_count": 1, "eval_count": 1}


class _FakeSession:
    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def post(self, url, json=None, timeout=None):
        self.sink.append(json)
        return _FakeResponse(json)


@pytest.mark.asyncio
async def test_ollama_complete_sends_temperature():
    sent: list[dict] = []
    with _with_temp(0.0), patch("aiohttp.ClientSession", lambda *a, **k: _FakeSession(sent)):
        await OllamaProvider(model="qwen3:4b").complete(messages=[{"role": "user", "content": "x"}])
    assert sent[0]["options"] == {"temperature": 0.0}


@pytest.mark.asyncio
async def test_ollama_complete_omits_temperature_when_unset():
    sent: list[dict] = []
    with _with_temp(None), patch("aiohttp.ClientSession", lambda *a, **k: _FakeSession(sent)):
        await OllamaProvider(model="qwen3:4b").complete(messages=[{"role": "user", "content": "x"}])
    assert "options" not in sent[0]
