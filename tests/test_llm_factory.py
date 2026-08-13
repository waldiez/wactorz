"""Tests for per-call-site LLM provider overrides (wactorz/llm_factory.py)."""

import sys

import pytest

from wactorz.agents.llm_agent import OllamaProvider
from wactorz.config import _env_name
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


def test_parse_strips_quotes_that_survived_the_shell():
    """`set LLM_OVERRIDES="a,b"` keeps the quotes in the value; split on `,`
    and `=`, they land on the first site and the last model — where the site
    matches nothing and the model 404s one character away from real."""
    raw = '"intent=ollama:qwen3:4b,planner=anthropic:claude-sonnet-4-6"'
    assert parse_overrides(raw) == {
        "intent": "ollama:qwen3:4b",
        "planner": "anthropic:claude-sonnet-4-6",
    }


@pytest.mark.parametrize(
    "raw",
    [
        'planner="anthropic:claude-sonnet-4-6"',
        "planner='anthropic:claude-sonnet-4-6'",
        'planner=anthropic:claude-sonnet-4-6"',
        '"planner"="anthropic:claude-sonnet-4-6"',
    ],
)
def test_parse_strips_quotes_wherever_they_land(raw):
    assert parse_overrides(raw) == {"planner": "anthropic:claude-sonnet-4-6"}


def test_parse_survives_a_value_that_is_only_quotes():
    """Regression: stripping by slice looped forever once the value ran out."""
    assert parse_overrides('""') == {}
    assert parse_overrides('"') == {}


def test_parse_skips_unknown_site_with_a_warning(caplog):
    """An override nothing reads is a typo, and silence makes it look applied."""
    with caplog.at_level("WARNING"):
        assert parse_overrides("plannr=anthropic:claude-sonnet-4-6") == {}
    assert "plannr" in caplog.text


def test_parse_keeps_good_entries_beside_an_unknown_site():
    raw = "intent=ollama:llama3,plannr=anthropic:claude-sonnet-4-6"
    assert parse_overrides(raw) == {"intent": "ollama:llama3"}


# ── the same quoting hazard on LLM_PROVIDER / LLM_MODEL ─────────────────────


def test_env_name_strips_quotes(monkeypatch):
    """A quoted LLM_MODEL reaches the API verbatim and 404s, with a blast
    radius of every call rather than one site."""
    monkeypatch.setenv("LLM_MODEL", '"claude-sonnet-4-6"')
    assert _env_name("LLM_MODEL", "fallback") == "claude-sonnet-4-6"
    monkeypatch.setenv("LLM_MODEL", 'claude-sonnet-4-6"')
    assert _env_name("LLM_MODEL", "fallback") == "claude-sonnet-4-6"


def test_env_name_falls_back_when_unset_or_empty(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert _env_name("LLM_MODEL", "fallback") == "fallback"
    monkeypatch.setenv("LLM_MODEL", '  ""  ')
    assert _env_name("LLM_MODEL", "fallback") == "fallback"


def test_known_sites_matches_the_sites_actually_read():
    """The list is hand-maintained; this is what catches a site added to the
    code and forgotten here, which would reject a legitimate override."""
    import re
    from pathlib import Path

    from wactorz.llm_factory import KNOWN_SITES

    root = Path(__file__).resolve().parent.parent / "wactorz"
    called = {
        match
        for path in root.rglob("*.py")
        for match in re.findall(r'provider_for\(\s*"([^"]+)"', path.read_text(encoding="utf-8"))
    }
    assert called == set(KNOWN_SITES)


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


# ── model flags ──────────────────────────────────────────────────────────────


class TestTheModelFlagsDoNotInventAModel:
    """⚠ argparse fills a `default=` in whether or not the flag was passed.

    A default on a model flag therefore reaches `create_provider` as an explicit
    model on every run, outranking `LLM_MODEL` — which pinned Gemini to one
    model no `.env` could change, and answered every request with a 404 once
    that model was retired.
    """

    @pytest.mark.parametrize("flag", ["ollama_model", "nim_model", "gemini_model"])
    def test_an_unpassed_flag_stays_unset(self, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
        from wactorz.cli import get_args

        monkeypatch.setattr(sys, "argv", ["wactorz"])

        assert getattr(get_args(), flag) is None

    def test_gemini_falls_back_to_the_configured_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wactorz.config import CONFIG

        # Building the provider constructs a real SDK client, which refuses to
        # exist without a key — and a machine with one configured would pass
        # this while CI, having none, could not. The key is nothing to do with
        # what is under test, so it is supplied rather than depended on.
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")

        provider = create_provider("gemini", None)

        assert provider is not None
        # `model_name` here, `model` on every other provider — see the startup
        # log in app.py, which reads both.
        assert provider.model_name == CONFIG.llm_model  # pyright: ignore[reportAttributeAccessIssue]


class TestASecretOnTheCommandLineIsCalledOut:
    """⚠ argv is world-readable on Linux: `ps` shows it to any local user, and
    the shell writes it to history. The environment variable carries the same
    value without either — so a token passed as a flag earns a warning.

    Warned, not refused: an existing launcher should keep working, and it cannot
    act on a failure it never sees.
    """

    @pytest.mark.parametrize("flag", ["--discord-token", "--telegram-token"])
    def test_it_warns_and_still_accepts_the_value(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str
    ) -> None:
        from wactorz.cli import get_args

        monkeypatch.setattr(sys, "argv", ["wactorz", flag, "s3cret"])

        args = get_args()

        warning = capsys.readouterr().err
        assert flag in warning
        assert "ps" in warning
        assert getattr(args, flag.removeprefix("--").replace("-", "_")) == "s3cret"

    def test_the_secret_itself_is_not_echoed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Printing it would put the value in a second place — the logs.
        from wactorz.cli import get_args

        monkeypatch.setattr(sys, "argv", ["wactorz", "--discord-token", "s3cret"])

        get_args()

        assert "s3cret" not in capsys.readouterr().err

    def test_a_run_without_tokens_is_silent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from wactorz.cli import get_args

        monkeypatch.setattr(sys, "argv", ["wactorz"])

        get_args()

        assert capsys.readouterr().err == ""
