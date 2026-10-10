"""Chat turns reach the console, not only the dashboard's feed.

The server quiets `wactorz.web` to warnings, so a turn logged under the module
that carries it would never be seen. It goes out under `wactorz.chat` instead,
redacted the way chat_log is, and cut short: the whole turn is in chat_log.
"""

import logging

import pytest

from wactorz.web import ws


def test_both_halves_of_a_turn_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="wactorz.chat"):
        ws.log_chat_turn("user", "@flic pair", "flic")
        ws.log_chat_turn("assistant", "Paired 'kitchen'.", "flic")

    assert [r.getMessage() for r in caplog.records] == [
        "[chat] user → @flic: @flic pair",
        "[chat] @flic → user: Paired 'kitchen'.",
    ]


def test_quieting_the_web_server_does_not_quiet_the_chat(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # What `app._start_web_ui` does to keep access logs off the console.
    web_logger = logging.getLogger("wactorz.web")
    previous = web_logger.level
    web_logger.setLevel(logging.WARNING)
    try:
        with caplog.at_level(logging.INFO, logger="wactorz.chat"):
            ws.log_chat_turn("user", "hello", "main")
    finally:
        web_logger.setLevel(previous)

    assert caplog.records[0].name == "wactorz.chat"


def test_a_long_turn_is_cut_short(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="wactorz.chat"):
        ws.log_chat_turn("assistant", "x" * (ws.CHAT_LOG_PREVIEW_CHARS * 2), "main")

    line = caplog.records[0].getMessage()
    assert line.endswith("…")
    assert len(line) < ws.CHAT_LOG_PREVIEW_CHARS + 50


def test_a_credential_is_redacted(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ws, "redact", lambda text: text.replace("hunter2", "***"))

    with caplog.at_level(logging.INFO, logger="wactorz.chat"):
        ws.log_chat_turn("user", "my password is hunter2", "main")

    assert "hunter2" not in caplog.records[0].getMessage()
