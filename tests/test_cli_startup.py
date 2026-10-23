"""How the `wactorz` command reports a configuration it cannot start with."""

import logging
import sys

import pytest

import wactorz.app
from wactorz import cli
from wactorz.errors import StartupError


async def _refuses(_args: object) -> None:
    raise StartupError("TELEGRAM_BOT_TOKEN not set.")


def test_a_refusal_is_one_line_and_a_status_to_not_restart_on(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["wactorz"])
    monkeypatch.setattr(wactorz.app, "app", _refuses)

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 1
    reported = [r for r in caplog.records if r.getMessage().startswith("[startup]")]
    assert [r.getMessage() for r in reported] == ["[startup] TELEGRAM_BOT_TOKEN not set."]
    assert reported[0].exc_info is None
