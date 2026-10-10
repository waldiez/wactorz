"""`/migrate` typed into the dashboard's chat.

The dashboard answers it with main's own command, so it reads the same as from
any other channel; each place that parses the words is checked on its own, and
a `--force` dropped on the way would refuse a move the user asked to force.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.web import chat


class _Moves:
    """Stands in for main's `migrate_agent`, recording how it was called."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def __call__(
        self, agent_name: str, target_node: str, *, force: bool = False
    ) -> dict[str, Any]:
        self.calls.append((agent_name, target_node, force))
        return {"success": True, "message": "moved"}


class _Replies:
    def __init__(self) -> None:
        self.lines: list[str] = []

    async def __call__(self, text: str) -> None:
        self.lines.append(text)


@pytest.fixture(name="moves")
def moves_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Moves:
    """Main, found by the chat's slash commands, moving nothing but recording each move."""
    main = MainActor(llm_provider=None, persistence_dir=str(tmp_path))
    recorded = _Moves()
    monkeypatch.setattr(main, "migrate_agent", recorded)
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: main)
    return recorded


@pytest.mark.parametrize(
    ("text", "force"),
    [
        ("/migrate counter rpi", False),
        ("/migrate counter rpi --force", True),
        ("/migrate --force counter rpi", True),
    ],
)
async def test_force_is_passed_on_wherever_it_is_written(
    moves: _Moves, text: str, force: bool
) -> None:
    replies = _Replies()

    assert await chat.handle_slash(text, replies) is True

    assert moves.calls == [("counter", "rpi", force)]
    assert replies.lines == ["[OK] moved"]


async def test_the_flag_alone_is_not_an_agent_and_a_node(moves: _Moves) -> None:
    replies = _Replies()

    await chat.handle_slash("/migrate counter --force", replies)

    assert moves.calls == []
    assert replies.lines[0].startswith("Usage: /migrate <agent-name> <target-node> [--force]")


async def test_on_a_stream_the_answer_is_one_message(moves: _Moves) -> None:
    replies, streamed = _Replies(), _Replies()
    ended: list[bool] = []

    async def _end() -> None:
        ended.append(True)

    await chat.handle_slash("/migrate counter rpi", replies, streamed, _end)

    assert replies.lines == []
    assert streamed.lines == ["[OK] moved"]
    assert ended == [True]


async def test_without_main_there_is_nowhere_to_move_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: None)
    replies = _Replies()

    await chat.handle_slash("/migrate counter rpi", replies)

    assert replies.lines == [f"[error] {chat.NO_MAIN_FOR_NODES}"]
