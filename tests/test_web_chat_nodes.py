"""`/nodes` typed into the dashboard's chat.

Main answers it, remote nodes and their readings included, so the handler here
lets it through. In the minimal profile there is no main to answer, and no
remote node either: the handler answers for the one node there is.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.web import chat, runtime


class _Registry:
    def __init__(self, *names: str) -> None:
        self._actors = [SimpleNamespace(name=name) for name in names]

    def all_actors(self) -> list[Any]:
        return self._actors


class _Replies:
    def __init__(self) -> None:
        self.lines: list[str] = []

    async def __call__(self, text: str) -> None:
        self.lines.append(text)


async def test_with_main_it_is_mains_to_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: object())
    replies = _Replies()

    assert await chat.handle_slash("/nodes", replies) is False

    assert replies.lines == []


async def test_without_main_it_lists_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: None)
    monkeypatch.setattr(runtime, "registry", _Registry("monitor", "imu"))
    replies = _Replies()

    assert await chat.handle_slash("/nodes", replies) is True

    assert replies.lines == ["Nodes:\n  local    online   @monitor, @imu"]
