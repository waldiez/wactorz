"""Backend configuration written from the desktop Configure view.

This file holds real secrets — the LLM API key, the HA token, the MQTT password
— so the permission check below is a security assertion, not a detail. The rest
pins the merge rules the Configure form depends on: blank clears a key, and keys
a user added by hand are never destroyed by saving the form.
"""

# pylint: disable=missing-function-docstring

import stat

import pytest

from wactorz.desktop import backend_config


@pytest.fixture(name="user_env", autouse=True)
def user_env_fixture(tmp_path, monkeypatch):
    """Redirect user.env at a tmp path."""
    path = tmp_path / "sub" / "user.env"
    monkeypatch.setattr(backend_config, "_USER_ENV", path)
    return path


# ── reading ─────────────────────────────────────────────────────────────────


def test_load_is_empty_without_a_file():
    assert backend_config.load() == {}


def test_load_returns_only_managed_keys_that_have_a_value(user_env):
    user_env.parent.mkdir(parents=True)
    user_env.write_text("MQTT_HOST=broker\nMQTT_PORT=\nNOT_MANAGED=x\n")
    assert backend_config.load() == {"MQTT_HOST": "broker"}


def test_reading_ignores_blanks_comments_and_junk_lines(user_env):
    user_env.parent.mkdir(parents=True)
    user_env.write_text("\n# a comment\nMQTT_HOST = broker \nnonsense-without-equals\n")
    # Surrounding whitespace is stripped from both sides.
    assert backend_config.load() == {"MQTT_HOST": "broker"}


def test_a_value_containing_equals_is_kept_whole(user_env):
    # Base64-ish tokens routinely contain '='.
    user_env.parent.mkdir(parents=True)
    user_env.write_text("LLM_API_KEY=abc=def==\n")
    assert backend_config.load()["LLM_API_KEY"] == "abc=def=="


def test_an_unreadable_file_reads_as_empty(tmp_path, monkeypatch):
    blocked = tmp_path / "user.env"
    blocked.mkdir()  # a directory where the file should be
    monkeypatch.setattr(backend_config, "_USER_ENV", blocked)
    assert backend_config.load() == {}


# ── writing ─────────────────────────────────────────────────────────────────


def test_save_persists_values_and_creates_the_directory(user_env):
    backend_config.save({"MQTT_HOST": "broker", "MQTT_PORT": "1883"})
    assert user_env.exists()
    assert backend_config.load() == {"MQTT_HOST": "broker", "MQTT_PORT": "1883"}


def test_saved_file_is_owner_only(user_env):
    # It holds the API key, HA token and MQTT password.
    backend_config.save({"LLM_API_KEY": "secret"})
    mode = stat.S_IMODE(user_env.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR  # 0600 — no group/other access


def test_a_blank_value_clears_the_key(user_env):
    backend_config.save({"MQTT_HOST": "broker"})
    backend_config.save({"MQTT_HOST": "   "})
    assert "MQTT_HOST" not in backend_config.load()


def test_omitting_a_key_clears_it(user_env):
    # The Configure form posts every field, so an absent key means "cleared".
    backend_config.save({"MQTT_HOST": "broker"})
    backend_config.save({})
    assert backend_config.load() == {}


def test_values_are_stripped_before_saving(user_env):
    backend_config.save({"MQTT_HOST": "  broker  "})
    assert backend_config.load()["MQTT_HOST"] == "broker"


def test_hand_added_keys_survive_a_form_save(user_env):
    user_env.parent.mkdir(parents=True)
    user_env.write_text("CUSTOM_THING=keepme\nMQTT_HOST=old\n")
    backend_config.save({"MQTT_HOST": "new"})
    assert backend_config.env_for_backend() == {"CUSTOM_THING": "keepme", "MQTT_HOST": "new"}


def test_saving_never_raises_when_the_path_is_unwritable(tmp_path, monkeypatch):
    blocked = tmp_path / "user.env"
    blocked.mkdir()
    monkeypatch.setattr(backend_config, "_USER_ENV", blocked)
    backend_config.save({"MQTT_HOST": "broker"})  # must not raise


# ── env for the child ───────────────────────────────────────────────────────


def test_env_for_backend_includes_unmanaged_keys(user_env):
    user_env.parent.mkdir(parents=True)
    user_env.write_text("MQTT_HOST=broker\nEXTRA=1\n")
    assert backend_config.env_for_backend() == {"MQTT_HOST": "broker", "EXTRA": "1"}


def test_env_for_backend_drops_empty_values(user_env):
    user_env.parent.mkdir(parents=True)
    user_env.write_text("MQTT_HOST=broker\nEMPTY=\n")
    assert backend_config.env_for_backend() == {"MQTT_HOST": "broker"}


def test_env_for_backend_is_empty_without_a_file():
    assert backend_config.env_for_backend() == {}
