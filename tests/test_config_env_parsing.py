# SPDX-License-Identifier: Apache-2.0
"""Reading the environment must not be able to take the process down.

`_env_int` raised a bare `ValueError` at *import* time, so one stray character
in one variable killed startup with a traceback naming neither the variable nor
the value. And one boolean was parsed inline and case-sensitively, so `FALSE`
read as true while every other flag in the file accepted it.

These call the parsers directly rather than reloading `wactorz.config`. A reload
rebinds `CONFIG`, while every module that did `from ..config import CONFIG` keeps
the object it captured at import — so the two disagree for the rest of the
session, and whichever test runs next inherits it. The parsers read the
environment when called, so nothing has to be re-imported to vary it.
"""

import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from wactorz import arguments, config
from wactorz.config import _dashboard_port, _env_int, _env_token, _env_truthy


class TestAMalformedInteger:
    def test_it_falls_back_instead_of_killing_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WACTORZ_TEST_INT", "25MB")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert _env_int("WACTORZ_TEST_INT", 4096) == 4096

    def test_it_says_which_setting_was_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Silently falling back would leave someone wondering why their limit
        # had no effect.
        monkeypatch.setenv("WACTORZ_TEST_INT", "25MB")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _env_int("WACTORZ_TEST_INT", 4096)

        assert any("WACTORZ_TEST_INT" in str(w.message) for w in caught)
        assert any("25MB" in str(w.message) for w in caught)

    @pytest.mark.parametrize(("raw", "expected"), [("1024", 1024), ("  512  ", 512), ("-3", -3)])
    def test_a_valid_value_is_still_honoured(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
    ) -> None:
        monkeypatch.setenv("WACTORZ_TEST_INT", raw)

        assert _env_int("WACTORZ_TEST_INT", 99) == expected

    def test_unset_and_empty_both_take_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WACTORZ_TEST_INT", raising=False)
        assert _env_int("WACTORZ_TEST_INT", 7) == 7

        monkeypatch.setenv("WACTORZ_TEST_INT", "   ")
        assert _env_int("WACTORZ_TEST_INT", 7) == 7


class TestTheBooleanVocabulary:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", " on "])
    def test_every_spelling_of_yes(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("WACTORZ_TEST_FLAG", value)

        assert _env_truthy("WACTORZ_TEST_FLAG") is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "", "2", "maybe"])
    def test_every_spelling_of_no(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        # `FALSE` is the regression this closes: the old inline check was
        # case-sensitive, so it read as true.
        monkeypatch.setenv("WACTORZ_TEST_FLAG", value)

        assert _env_truthy("WACTORZ_TEST_FLAG") is False

    def test_unset_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WACTORZ_TEST_FLAG", raising=False)

        assert _env_truthy("WACTORZ_TEST_FLAG") is False


class TestTheNodeSigningMode:
    """A node holding a key refuses unsigned commands unless told otherwise.

    Main signs every command it sends a node, so an unsigned one came from
    something else on the broker.
    """

    def test_unset_enforces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WACTORZ_NODE_SIGNING", raising=False)

        assert config._node_signing_mode() == "enforce"

    def test_warn_is_still_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # For finding out what sends unsigned commands before refusing them.
        monkeypatch.setenv("WACTORZ_NODE_SIGNING", "Warn")

        assert config._node_signing_mode() == "warn"

    def test_a_typo_takes_the_default_and_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WACTORZ_NODE_SIGNING", "enforc")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert config._node_signing_mode() == "enforce"

        assert any("WACTORZ_NODE_SIGNING" in str(w.message) for w in caught)


def test_the_state_bridge_flag_is_wired_to_the_shared_parser() -> None:
    """The behaviour change worth naming, and the reason for a source check.

    Only the parser above can be exercised without reloading the module, so this
    pins the wiring instead. `2` and an empty-but-set value used to read as
    *true* — the old inline test was `not in ("0", "false", "no")` — and now read
    as false. Stricter than "fix the casing", and deliberate.
    """

    source = Path("wactorz/config.py").read_text(encoding="utf-8")

    assert 'ha_state_bridge_per_entity=_env_truthy("HA_STATE_BRIDGE_PER_ENTITY")' in source


class TestTheFileIsLoadedFirst:
    """Settings read above the `.env` load see only real environment variables.

    The dataclass is built near the end of the module and the bare constants far
    above it, so the load sits between them unless something keeps it first: a
    file obeyed for one and ignored for the other, with nothing on the outside to
    tell that apart from a misspelled name.
    """

    def test_no_setting_is_read_before_the_file_is_loaded(self) -> None:
        source = Path(config.__file__).read_text(encoding="utf-8")
        loaded_at = source.index("load_dotenv(")
        read_at = min(
            source.index(f"\n{name} = ")
            for name in ("DEV_MODE", "UPLOADS_ENABLED", "UPLOAD_MAX_BYTES", "INGRESS_ENABLED")
        )

        assert loaded_at < read_at


class TestTheDashboardPort:
    """One setting under two names, and one answer."""

    def test_the_documented_name_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_PORT", "9001")
        monkeypatch.delenv("WS_PORT", raising=False)

        assert _dashboard_port() == 9001

    def test_the_older_name_is_read_when_the_other_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MONITOR_PORT", raising=False)
        monkeypatch.setenv("WS_PORT", "9002")

        assert _dashboard_port() == 9002

    def test_the_documented_name_wins_when_both_are_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MONITOR_PORT", "9001")
        monkeypatch.setenv("WS_PORT", "9002")

        assert _dashboard_port() == 9001

    def test_neither_set_is_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MONITOR_PORT", raising=False)
        monkeypatch.delenv("WS_PORT", raising=False)

        assert _dashboard_port() == 8888

    def test_the_command_line_takes_its_default_from_the_same_place(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Read separately, the two could name different ports for one server.
        monkeypatch.setenv("MONITOR_PORT", "1111")
        monkeypatch.setattr(arguments, "CONFIG", replace(config.CONFIG, ws_port=9234))

        assert arguments.get_args([]).monitor_port == 9234


class TestABotToken:
    """A chat channel's token, read from its documented name or its shorter one."""

    def test_whitespace_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A blank token would log in, be rejected, and only then say so.
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " \t")
        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)

        assert _env_token("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN") == ""

    def test_a_blank_documented_name_falls_back_to_the_shorter_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " ")
        monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")

        assert _env_token("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN") == "123:abc"

    def test_a_real_token_is_trimmed_and_otherwise_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", " MTIz.abc-def_ghi \n")

        assert _env_token("DISCORD_BOT_TOKEN", "DISCORD_TOKEN") == "MTIz.abc-def_ghi"

    def test_the_documented_name_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "first")
        monkeypatch.setenv("DISCORD_TOKEN", "second")

        assert _env_token("DISCORD_BOT_TOKEN", "DISCORD_TOKEN") == "first"

    def test_both_channels_read_through_it(self) -> None:
        source = Path(config.__file__).read_text(encoding="utf-8")

        assert 'telegram_token=_env_token("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN")' in source
        assert 'discord_token=_env_token("DISCORD_BOT_TOKEN", "DISCORD_TOKEN")' in source
