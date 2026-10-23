"""`/agents` typed into the dashboard's chat.

Main answers every form of it -- the list, a capability filter, and the verbs
that stop, delete or restart an agent -- so the handler here lets them all
through. In the minimal profile there is no main, and the handler lists the
agents running in this process.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.orchestration import main_commands
from wactorz.web import chat, runtime


class _Actor:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"{name:0<32}"
        self.protected = False

    def get_status(self) -> dict[str, Any]:
        return {"state": "running"}


class _Registry:
    def __init__(self, *names: str) -> None:
        self._actors = [_Actor(name) for name in names]

    def all_actors(self) -> list[Any]:
        return self._actors


class _Replies:
    def __init__(self) -> None:
        self.lines: list[str] = []

    async def __call__(self, text: str) -> None:
        self.lines.append(text)


@pytest.mark.parametrize(
    "typed",
    ["/agents", "/agents weather", "/agents restart weather", "/agents stop weather"],
)
async def test_with_main_every_form_is_mains_to_answer(
    monkeypatch: pytest.MonkeyPatch, typed: str
) -> None:
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: SimpleNamespace())
    monkeypatch.setattr(runtime, "orchestrator", None)
    monkeypatch.setattr(runtime, "registry", _Registry("weather"))
    replies = _Replies()

    assert await chat.handle_slash(typed, replies) is False

    assert replies.lines == []
    assert chat.command_word(typed) in main_commands(), "and main says it answers it"


class _Orchestrator:
    def __init__(self, *claimed: str) -> None:
        self.claimed = frozenset(claimed)

    def commands(self) -> frozenset[str]:
        return self.claimed


@pytest.mark.parametrize(("claimed", "listed_here"), [(("/agents",), False), (("/report",), True)])
async def test_beside_an_orchestrator_it_lists_only_what_that_does_not_claim(
    monkeypatch: pytest.MonkeyPatch, claimed: tuple[str, ...], listed_here: bool
) -> None:
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: SimpleNamespace())
    monkeypatch.setattr(runtime, "orchestrator", _Orchestrator(*claimed))
    monkeypatch.setattr(runtime, "registry", _Registry("weather"))
    replies = _Replies()

    assert await chat.handle_slash("/agents", replies) is listed_here
    assert bool(replies.lines) is listed_here


async def test_without_main_it_lists_this_processs_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: None)
    monkeypatch.setattr(runtime, "orchestrator", None)
    monkeypatch.setattr(runtime, "registry", _Registry("monitor", "imu"))
    replies = _Replies()

    assert await chat.handle_slash("/agents", replies) is True

    (listing,) = replies.lines
    assert "@monitor" in listing
    assert "@imu" in listing
