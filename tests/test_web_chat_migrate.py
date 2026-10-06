"""`/migrate` typed into the dashboard's chat.

The command exists in three places — main's command table, the CLI and this
handler — and each parses its own words, so each is checked on its own: a
`--force` one of them drops would refuse a move the user asked to force.
"""

from typing import Any

import pytest

from wactorz.web import chat


class _Main:
    """The one method the handler calls on main, recording how it was called."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def migrate_agent(
        self, agent_name: str, target_node: str, *, force: bool = False
    ) -> dict[str, Any]:
        self.calls.append((agent_name, target_node, force))
        return {"success": True, "message": "moved"}


class _Replies:
    def __init__(self) -> None:
        self.lines: list[str] = []

    async def __call__(self, text: str) -> None:
        self.lines.append(text)


@pytest.fixture
def main(monkeypatch: pytest.MonkeyPatch) -> _Main:
    found = _Main()
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: found)
    return found


@pytest.mark.parametrize(
    ("text", "force"),
    [
        ("/migrate counter rpi", False),
        ("/migrate counter rpi --force", True),
        ("/migrate --force counter rpi", True),
    ],
)
async def test_force_is_passed_on_wherever_it_is_written(
    main: _Main, text: str, force: bool
) -> None:
    replies = _Replies()

    assert await chat.handle_slash(text, replies) is True

    assert main.calls == [("counter", "rpi", force)]
    assert replies.lines[-1] == "[OK] moved"


async def test_the_flag_alone_is_not_an_agent_and_a_node(main: _Main) -> None:
    replies = _Replies()

    await chat.handle_slash("/migrate counter --force", replies)

    assert main.calls == []
    assert replies.lines == ["[usage] /migrate <agent-name> <target-node> [--force]"]
