"""Which speech-to-text branch a deployment offers, and how the browser learns it.

Capture is the part that differs: only the browser branches call ``getUserMedia``,
which needs a secure context, so a mode the deployment cannot serve shows up as a
microphone that is offered and then fails. The browser is told rather than built
with the answer, which is what lets one wheel serve every deployment.
"""

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz import config
from wactorz.web.app import build_app


class TestReadingTheSetting:
    def test_an_unset_variable_means_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WACTORZ_STT", raising=False)

        assert config._stt_mode() == "off"

    @pytest.mark.parametrize("mode", config.STT_MODES)
    def test_every_branch_is_accepted(self, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        monkeypatch.setenv("WACTORZ_STT", mode)

        assert config._stt_mode() == mode

    @pytest.mark.parametrize("written", ["HOST", " Server ", '"browser"'])
    def test_case_spacing_and_quotes_are_forgiven(
        self, monkeypatch: pytest.MonkeyPatch, written: str
    ) -> None:
        monkeypatch.setenv("WACTORZ_STT", written)

        assert config._stt_mode() == written.strip().strip('"').lower()

    def test_an_empty_value_means_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WACTORZ_STT", "   ")

        assert config._stt_mode() == "off"

    def test_an_unknown_branch_is_named_not_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WACTORZ_STT", "sever")

        # Silence would present a typo as a feature that does not work: the
        # microphone never appears and nothing says why.
        with pytest.warns(RuntimeWarning, match="sever"):
            assert config._stt_mode() == "off"


class TestTellingTheBrowser:
    @pytest.mark.parametrize("mode", config.STT_MODES)
    async def test_the_configured_branch_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        monkeypatch.setattr(config, "STT_MODE", mode)
        app = build_app()

        async with TestClient(TestServer(app)) as client:
            payload: dict[str, Any] = await (await client.get("/api/config")).json()

        assert payload["stt"]["mode"] == mode
