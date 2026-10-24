"""Which speech-to-text branch a deployment offers, and how the browser learns it.

Capture is the part that differs: only the browser branches call ``getUserMedia``,
which needs a secure context, so a mode the deployment cannot serve shows up as a
microphone that is offered and then fails. The browser is told rather than built
with the answer, which is what lets one wheel serve every deployment.
"""

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.ext.voice import settings as voice_settings
from wactorz.web.app import build_app


class TestReadingTheSetting:
    def test_an_unset_variable_means_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WACTORZ_STT", raising=False)

        assert voice_settings._stt_mode() == "off"

    @pytest.mark.parametrize("mode", voice_settings.STT_MODES)
    def test_every_branch_is_accepted(self, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        monkeypatch.setenv("WACTORZ_STT", mode)

        assert voice_settings._stt_mode() == mode

    @pytest.mark.parametrize("written", ["HOST", " Server ", '"browser"'])
    def test_case_spacing_and_quotes_are_forgiven(
        self, monkeypatch: pytest.MonkeyPatch, written: str
    ) -> None:
        monkeypatch.setenv("WACTORZ_STT", written)

        assert voice_settings._stt_mode() == written.strip().strip('"').lower()

    def test_an_empty_value_means_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WACTORZ_STT", "   ")

        assert voice_settings._stt_mode() == "off"

    def test_an_unknown_branch_is_named_not_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WACTORZ_STT", "sever")

        # Silence would present a typo as a feature that does not work: the
        # microphone never appears and nothing says why.
        with pytest.warns(RuntimeWarning, match="sever"):
            assert voice_settings._stt_mode() == "off"


class TestTellingTheBrowser:
    @pytest.mark.parametrize("mode", voice_settings.STT_MODES)
    async def test_the_configured_branch_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        monkeypatch.setattr(voice_settings, "STT_MODE", mode)
        app = build_app()

        async with TestClient(TestServer(app)) as client:
            payload: dict[str, Any] = await (await client.get("/api/config")).json()

        assert payload["stt"]["mode"] == mode

    async def test_what_settings_may_offer_is_reported_up_to_the_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(voice_settings, "STT_MODE", "server")
        monkeypatch.setattr(voice_settings, "TTS_MODE", "host")
        monkeypatch.setattr(voice_settings, "WAKE_MODE", "off")
        app = build_app()

        async with TestClient(TestServer(app)) as client:
            payload: dict[str, Any] = await (await client.get("/api/config")).json()

        # Told rather than worked out in the page, so the dashboard never offers
        # a branch the server is going to refuse.
        assert payload["voice"]["allowed"] == {
            "listening": ["off", "browser", "server"],
            "speaking": ["off", "browser", "server", "host"],
            "waking": ["off"],
        }
