"""What every TUI test shares."""

import subprocess
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def no_real_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """`wactorz-tui` can start a server. A test must never: it would read this machine's
    `.env` and rewrite the broker files a real broker is using."""

    def refused(*args: Any, **_kwargs: Any) -> int:
        raise AssertionError(f"a test tried to start a process: {args}")

    monkeypatch.setattr(subprocess, "call", refused)
