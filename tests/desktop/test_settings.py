"""Desktop preferences file.

Only preferences with no OS-level source of truth live here, so the contract is
narrow: a missing or unreadable file must never surface as an error — the app
falls back to defaults and carries on.
"""

# pylint: disable=missing-function-docstring

import pytest

from wactorz.desktop import settings


@pytest.fixture(name="settings_file", autouse=True)
def settings_file_fixture(tmp_path, monkeypatch):
    """Redirect the preferences file at a tmp path."""
    path = tmp_path / "sub" / "desktop_settings.json"
    monkeypatch.setattr(settings, "_SETTINGS_FILE", path)
    return path


def test_auto_update_check_defaults_to_off(settings_file):
    assert not settings_file.exists()
    assert settings.auto_update_check() is False


def test_setting_the_toggle_persists_it(settings_file):
    settings.set_auto_update_check(True)
    assert settings_file.exists()
    assert settings.auto_update_check() is True


def test_the_toggle_can_be_turned_back_off():
    settings.set_auto_update_check(True)
    settings.set_auto_update_check(False)
    assert settings.auto_update_check() is False


def test_a_truthy_value_is_stored_as_a_bool(settings_file):
    settings.set_auto_update_check("yes")  # type: ignore[arg-type]
    assert settings_file.read_text() == '{"auto_update_check": true}'


def test_corrupt_preferences_fall_back_to_defaults(settings_file):
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text("{ not json")
    assert settings.auto_update_check() is False


def test_unknown_keys_in_the_file_are_preserved(settings_file):
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text('{"auto_update_check": true, "future_option": 42}')
    settings.set_auto_update_check(False)
    assert '"future_option": 42' in settings_file.read_text()


def test_saving_never_raises_when_the_path_is_unwritable(tmp_path, monkeypatch):
    blocked = tmp_path / "prefs.json"
    blocked.mkdir()  # a directory where the file should be
    monkeypatch.setattr(settings, "_SETTINGS_FILE", blocked)
    settings.set_auto_update_check(True)  # must not raise
    assert settings.auto_update_check() is False  # unreadable → defaults
