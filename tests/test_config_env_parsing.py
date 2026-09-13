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
from pathlib import Path

import pytest

from wactorz import config
from wactorz.config import _env_int, _env_truthy


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


def test_the_state_bridge_flag_is_wired_to_the_shared_parser() -> None:
    """The behaviour change worth naming, and the reason for a source check.

    Only the parser above can be exercised without reloading the module, so this
    pins the wiring instead. `2` and an empty-but-set value used to read as
    *true* — the old inline test was `not in ("0", "false", "no")` — and now read
    as false. Stricter than "fix the casing", and deliberate.
    """
    from pathlib import Path

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
