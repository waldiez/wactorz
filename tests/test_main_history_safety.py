"""Raw, external and interrupted replies cannot persist delegation syntax."""

import asyncio
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from wactorz.agents.llm.base import LLMProvider
from wactorz.agents.main.actor import MainActor
from wactorz.core.actor import Message, MessageType

RAW = 'Working. <delegate>{"agent":"home-assistant-agent"}</delegate>'


def _capture_history(saved: list[list[dict]], key: str, value: Any) -> None:
    if key == "conversation_history":
        saved.append([dict(item) for item in value])


class _Provider:
    def __init__(self, interrupted: bool = False) -> None:
        self.interrupted = interrupted

    async def complete(self, **_kwargs: Any) -> tuple[str, dict]:
        return RAW, {}

    def supports_streaming(self) -> bool:
        return True

    async def stream(self, **_kwargs: Any) -> AsyncIterator[str | dict]:
        yield RAW if not self.interrupted else 'Working. <delegate>{"agent":"ha"'
        if self.interrupted:
            raise asyncio.CancelledError
        yield {}


@pytest.mark.parametrize("path", ["chat", "stream", "interrupted", "task", "external"])
async def test_all_reply_paths_strip_transport_before_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    main = MainActor(
        llm_provider=cast(LLMProvider, _Provider(interrupted=path == "interrupted")),
        persistence_dir=str(tmp_path),
    )
    saved: list[list[dict]] = []

    monkeypatch.setattr(main, "persist", partial(_capture_history, saved))
    monkeypatch.setattr(main, "_mqtt_publish", AsyncMock())
    monkeypatch.setattr(main, "_reply_to_task", AsyncMock())
    monkeypatch.setattr(main, "_maybe_summarize", AsyncMock())
    monkeypatch.setattr(main, "run_detached", lambda coro, **_kwargs: coro.close())
    if path == "chat":
        assert await main.chat("hello") == RAW
    elif path in ("stream", "interrupted"):
        if path == "interrupted":
            with pytest.raises(asyncio.CancelledError):
                _ = [chunk async for chunk in main.chat_stream("hello")]
        else:
            _ = [chunk async for chunk in main.chat_stream("hello")]
    elif path == "task":
        await main._handle_task(
            Message(type=MessageType.TASK, sender_id="test", payload={"text": "hello"})
        )
    else:
        await main._record_external_exchange("hello", RAW, ts_user=1.0)

    assert saved
    assert all("<delegate" not in str(history) for history in saved)
    assert main._conversation_history[-1]["content"] == "Working."
