"""Tests for LLM_TEMPERATURE plumbing (config → providers)."""

import types
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest

from tests.optional_deps import ensure_importable

# Not `sys.modules.setdefault`: that asks whether openai has been imported yet,
# not whether it can be, so on a machine where it *is* installed the empty stub
# won wherever this module was imported first — shadowing the real package for
# the rest of the process.
ensure_importable("openai", "anthropic")

from wactorz.agents.llm.base import _ollama_options, _resolve_temperature, _temp_params
from wactorz.agents.llm.providers import anthropic as anthropic_provider
from wactorz.agents.llm_agent import AnthropicProvider, OllamaProvider
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


# ── Anthropic: models that dropped the sampling parameters ───────────────────


class _BadRequest(Exception):
    """Stands in for `anthropic.BadRequestError`, which the SDK builds from an
    httpx response the fake client never has."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.status_code = 400


_TEMPERATURE_DEPRECATED = (
    "Error code: 400 - {'error': {'message': '`temperature` is deprecated for this model.'}}"
)


class _FakeAnthropicMessages:
    """Records every request, and can fail the first one."""

    def __init__(self, fail_first_with: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_first_with = fail_first_with

    async def create(self, **kwargs: Any) -> types.SimpleNamespace:
        self._record(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="ok")],
            usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    def stream(self, **kwargs: Any) -> "_FakeStream":
        self._record(kwargs)
        return _FakeStream()

    def _record(self, kwargs: dict[str, Any]) -> None:
        self.calls.append(kwargs)
        if self.fail_first_with is not None and len(self.calls) == 1:
            raise self.fail_first_with


class _FakeStream:
    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    async def text_stream(self):
        yield "ok"

    async def get_final_message(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))


def _provider(model: str, messages: _FakeAnthropicMessages) -> AnthropicProvider:
    """A provider wired to a fake client, without touching the anthropic SDK."""
    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.model = model
    provider.client = types.SimpleNamespace(messages=messages)  # pyright: ignore[reportAttributeAccessIssue]
    return provider


@pytest.fixture(autouse=True)
def _forget_learned_models():
    """What one test taught the provider must not leak into the next."""
    anthropic_provider._learned_no_sampling.clear()
    yield
    anthropic_provider._learned_no_sampling.clear()


@pytest.mark.asyncio
async def test_anthropic_sends_temperature_to_a_model_that_takes_it():
    messages = _FakeAnthropicMessages()
    with _with_temp(0.0):
        await _provider("claude-sonnet-4-6", messages).complete([{"role": "user", "content": "x"}])
    assert messages.calls[0]["temperature"] == 0.0


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5", "claude-opus-4-8", "claude-sonnet-5", "anthropic.claude-opus-5"],
)
@pytest.mark.asyncio
async def test_anthropic_omits_temperature_for_models_that_dropped_it(model):
    """These models 400 on `temperature`, so it never goes out in the first place."""
    messages = _FakeAnthropicMessages()
    with _with_temp(0.0):
        await _provider(model, messages).complete([{"role": "user", "content": "x"}])
    assert "temperature" not in messages.calls[0]


@pytest.mark.asyncio
async def test_anthropic_retries_without_temperature_when_a_new_model_rejects_it():
    """A model too new to be on the list costs one 400, then behaves."""
    messages = _FakeAnthropicMessages(fail_first_with=_BadRequest(_TEMPERATURE_DEPRECATED))
    provider = _provider("claude-opus-9", messages)
    with _with_temp(0.0):
        text, _ = await provider.complete([{"role": "user", "content": "x"}])
        assert text == "ok"
        assert messages.calls[0]["temperature"] == 0.0
        assert "temperature" not in messages.calls[1]

        # And the lesson sticks: the next call never sends it at all.
        await provider.complete([{"role": "user", "content": "x"}])
    assert "temperature" not in messages.calls[2]


@pytest.mark.asyncio
async def test_anthropic_stream_retries_without_temperature():
    messages = _FakeAnthropicMessages(fail_first_with=_BadRequest(_TEMPERATURE_DEPRECATED))
    with _with_temp(0.0):
        chunks = [
            chunk
            async for chunk in _provider("claude-opus-9", messages).stream(
                [{"role": "user", "content": "x"}]
            )
        ]
    assert chunks[0] == "ok"
    assert "temperature" not in messages.calls[1]


@pytest.mark.asyncio
async def test_anthropic_does_not_swallow_an_out_of_range_temperature():
    """A value the model *would* take in another range is a config error the
    caller has to see — not something to retry into silent success."""
    error = _BadRequest("Error code: 400 - temperature: Input should be less than or equal to 1")
    messages = _FakeAnthropicMessages(fail_first_with=error)
    with _with_temp(2.5), pytest.raises(_BadRequest):
        await _provider("claude-sonnet-4-6", messages).complete([{"role": "user", "content": "x"}])
    assert len(messages.calls) == 1
