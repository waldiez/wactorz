"""Every desktop test writes under its own temporary directory.

The desktop modules take their paths from the user's data directory when they
are imported, so a test that starts the backend, saves a setting or keeps the
window's place would otherwise write into the home directory of whoever runs
the suite.
"""

from pathlib import Path

import pytest

from wactorz.desktop import backend, backend_config, settings, window_state


@pytest.fixture(autouse=True)
def _data_dir_of_its_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = tmp_path / "wactorz-data"
    monkeypatch.setattr(backend, "DATA_DIR", data)
    monkeypatch.setattr(backend, "BACKEND_LOG", data / "desktop-backend.log")
    monkeypatch.setattr(backend_config, "_USER_ENV", data / "user.env")
    monkeypatch.setattr(settings, "_SETTINGS_FILE", data / "desktop_settings.json")
    monkeypatch.setattr(window_state, "WINDOW_STATE_FILE", data / "window_state.json")
