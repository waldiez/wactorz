"""Voice settings that outlive a reload without outliving the deployment."""

from __future__ import annotations

import json
from typing import Any

import pytest

from wactorz.core import voice_settings
from wactorz.web import api_system

#: The real resolution, captured before the suite-wide fixture replaces it. That
#: fixture exists so a developer's own choices cannot decide unrelated tests;
#: these tests are about the resolution itself, so they put it back.
_REAL_STORED = voice_settings._stored


class _Store:
    """A key-value store of the shape the real one presents."""

    def __init__(self, held: dict[str, Any] | None = None) -> None:
        self.held: dict[str, Any] = held or {}

    def kv_get(self, _owner: str, key: str) -> Any:
        return self.held.get(key)

    def kv_set(self, _owner: str, key: str, value: Any) -> None:
        self.held[key] = value


@pytest.fixture(name="store")
def store_fixture(monkeypatch: pytest.MonkeyPatch) -> _Store:
    """A deployment with somewhere to remember choices."""
    store = _Store()
    monkeypatch.setattr(voice_settings, "get_db", lambda: store)
    # The suite-wide fixture stubs this so a developer's own choices cannot
    # decide unrelated tests; these are about the resolution itself.
    monkeypatch.setattr(voice_settings, "_stored", _REAL_STORED)
    return store


class TestWhatDecidesWhenNothingHasBeenChosen:
    def test_the_environment_does(self, monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
        monkeypatch.setattr(voice_settings.config, "STT_MODE", "server")
        monkeypatch.setattr(voice_settings.config, "TTS_MODE", "browser")

        assert voice_settings.listening() == "server"
        assert voice_settings.speaking() == "browser"

    def test_and_still_does_without_a_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(voice_settings, "_stored", _REAL_STORED)
        monkeypatch.setattr(voice_settings, "get_db", lambda: None)
        monkeypatch.setattr(voice_settings.config, "STT_MODE", "host")

        # An install with no database runs on its environment rather than failing.
        assert voice_settings.listening() == "host"


class TestChoosingAnother:
    def test_a_choice_outranks_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, store: _Store
    ) -> None:
        monkeypatch.setattr(voice_settings.config, "TTS_MODE", "server")

        voice_settings.choose("speaking", "browser")

        assert voice_settings.speaking() == "browser"

    def test_forgetting_hands_it_back(self, monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
        monkeypatch.setattr(voice_settings.config, "TTS_MODE", "server")
        voice_settings.choose("speaking", "off")

        voice_settings.forget()

        assert voice_settings.speaking() == "server"

    def test_the_voice_is_kept_as_given(self, store: _Store) -> None:
        voice_settings.choose("voice", "  en_GB-alba-medium  ")

        assert voice_settings.voice() == "en_GB-alba-medium"

    def test_one_choice_does_not_erase_another(self, store: _Store) -> None:
        voice_settings.choose("speaking", "off")
        voice_settings.choose("listening", "host")

        assert voice_settings.speaking() == "off"
        assert voice_settings.listening() == "host"


class TestWhatIsRefused:
    def test_a_branch_this_version_does_not_have(self, store: _Store) -> None:
        # Refused rather than stored: it would resolve to nothing usable, and the
        # deployment would look configured for something it cannot do.
        with pytest.raises(ValueError, match="quantum"):
            voice_settings.choose("speaking", "quantum")

    def test_a_setting_that_is_not_one(self, store: _Store) -> None:
        with pytest.raises(ValueError, match="no such setting"):
            voice_settings.choose("volume", "loud")

    def test_choosing_with_nowhere_to_remember_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(voice_settings, "_stored", _REAL_STORED)
        monkeypatch.setattr(voice_settings, "get_db", lambda: None)

        # Said plainly rather than accepted and forgotten on the next read.
        with pytest.raises(RuntimeError, match="no store"):
            voice_settings.choose("speaking", "off")


class TestWhatWasStoredByAnotherVersion:
    def test_a_branch_that_no_longer_exists_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, store: _Store
    ) -> None:
        monkeypatch.setattr(voice_settings.config, "TTS_MODE", "server")
        store.held[voice_settings._KEY] = {"speaking": "telepathy"}

        # What is stored outlives the release that wrote it.
        assert voice_settings.speaking() == "server"

    def test_a_store_that_will_not_read_is_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _Broken:
            @staticmethod
            def kv_get(_owner: str, _key: str) -> Any:
                raise RuntimeError("the store is gone")

        monkeypatch.setattr(voice_settings, "_stored", _REAL_STORED)
        monkeypatch.setattr(voice_settings, "get_db", lambda: _Broken())
        monkeypatch.setattr(voice_settings.config, "STT_MODE", "server")

        assert voice_settings.listening() == "server"
        assert "the store is gone" in caplog.text


class TestChangingItOverTheWire:
    """`POST /api/voice` is what the message in a refusal points at."""

    @staticmethod
    async def _post(body: Any) -> tuple[int, dict[str, Any]]:
        class _Request:
            @staticmethod
            async def json() -> Any:
                if isinstance(body, Exception):
                    raise body
                return body

        response = await api_system.voice_settings_handler(_Request())  # type: ignore[arg-type]
        return response.status, json.loads(response.body or b"{}")  # type: ignore[arg-type]

    async def test_a_branch_can_be_changed(self, store: _Store) -> None:
        status, body = await self._post({"speaking": "off"})

        assert status == 200
        assert body["speaking"] == "off"

    async def test_a_branch_this_version_lacks_is_refused(self, store: _Store) -> None:
        status, body = await self._post({"speaking": "telepathy"})

        assert status == 400
        assert "telepathy" in body["error"]

    async def test_the_addresses_are_not_settable(self, store: _Store) -> None:
        # They name services this process dials; one a browser can write is one
        # it can point at anything reachable from the machine.
        status, body = await self._post({"listening_uri": "http://elsewhere"})

        assert status == 400
        assert "nothing to change" in body["error"]

    async def test_everything_can_be_handed_back(
        self, monkeypatch: pytest.MonkeyPatch, store: _Store
    ) -> None:
        monkeypatch.setattr(voice_settings.config, "TTS_MODE", "server")
        await self._post({"speaking": "off"})

        status, body = await self._post({"reset": True})

        assert status == 200
        assert body["speaking"] == "server"

    async def test_a_body_that_is_not_an_object(self, store: _Store) -> None:
        status, _body = await self._post(["speaking", "off"])

        assert status == 400

    async def test_a_bad_setting_leaves_the_good_one_alone(self, store: _Store) -> None:
        # Half-applied is a state nobody asked for, and one the answer does not
        # describe: the refusal names the bad key and says nothing of the good.
        status, body = await self._post({"speaking": "off", "listening": "telepathy"})

        assert status == 400
        assert "telepathy" in body["error"]
        assert voice_settings.speaking() != "off"

    async def test_a_change_says_again_what_the_new_branch_needs(
        self, monkeypatch: pytest.MonkeyPatch, store: _Store
    ) -> None:
        # Startup spoke about the branch it started on. Someone switching to one
        # that wants hardware has heard nothing about the hardware.
        said: list[str] = []
        monkeypatch.setattr(api_system.stt, "warn_if_it_cannot_listen", lambda: said.append("stt"))
        monkeypatch.setattr(
            api_system.tts, "warn_if_the_room_will_stay_quiet", lambda: said.append("tts")
        )

        await self._post({"listening": "host"})

        assert said == ["stt", "tts"]

    async def test_a_refused_change_says_nothing_new(
        self, monkeypatch: pytest.MonkeyPatch, store: _Store
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(api_system.stt, "warn_if_it_cannot_listen", lambda: said.append("stt"))

        await self._post({"listening": "telepathy"})

        assert not said
