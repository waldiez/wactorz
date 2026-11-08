# SPDX-License-Identifier: Apache-2.0
"""A question book reaches the Inbox only as its agent's word, by the rule main's labels use.

Anything that can publish under ``agents/<id>/`` (a node's account, a client of
an external broker, an agent's own code) could otherwise put a question in the
Inbox under any agent's name. So the web server admits a book before it keeps
it or shows it to a browser (`wactorz.agents.main.labels.admit_book`): an agent
in this process is read from the agent, and one on a node must come from the
node main places it on. What a browser is sent is the admitted book, never the
payload as it came. Until the system has started there is no main to place
agents by, so books are held and admitted once it has.
"""

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from tests.node_messages import signed_properties
from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.core.actor import ActorState
from wactorz.core.mqtt import user_properties
from wactorz.core.questions import Question, book_frame, questions_topic
from wactorz.web import mqtt, questions, runtime


def _question(qid: str, text: str = "Is the window open?") -> Question:
    return Question(id=qid, text=text, options=("yes", "no"), channel="dashboard", asked_at=1.0)


class _Moves:
    def moving_to(self, _name: str) -> str | None:
        return None


class _Agent:
    """An agent of this process, keeping its book itself."""

    def __init__(self, name: str, actor_id: str, *asked: Question) -> None:
        self.name = name
        self.actor_id = actor_id
        self._questions = {q.id: q for q in asked}

    def questions(self) -> dict[str, Question]:
        return dict(self._questions)


class _Registry:
    """The live registry: the agents of this process, by id and name."""

    def __init__(self, *agents: _Agent) -> None:
        self.agents = list(agents)

    def get(self, actor_id: str) -> _Agent | None:
        return next((a for a in self.agents if a.actor_id == actor_id), None)

    def find_by_name(self, name: str) -> _Agent | None:
        return next((a for a in self.agents if a.name == name), None)

    def all_actors(self) -> list[_Agent]:
        return list(self.agents)


class _Main:
    """What admission reads of main: where agents are placed, and its registry."""

    def __init__(self, registry: _Registry, placed: dict[str, str] | None = None) -> None:
        self.name = "main"
        self.state = ActorState.RUNNING
        self._registry = registry
        self._spawns = {name: {"node": node} for name, node in (placed or {}).items()}
        self._heartbeats: dict[str, dict[str, Any]] = {}
        self.migration = _Moves()

    def _get_spawn_registry(self) -> SpawnRegistry:
        return SpawnRegistry.from_names(self._spawns)

    @property
    def _known_nodes(self) -> dict[str, dict[str, Any]]:
        return self._heartbeats


class _Setup:
    """The web server with no broker: what it kept, and what browsers were sent."""

    def __init__(self, registry: _Registry) -> None:
        self.registry = registry
        self.main: _Main | None = None
        self.frames: list[dict[str, Any]] = []

    def relayed(self, topic: str) -> list[Any]:
        return [
            f["payload"]
            for f in self.frames
            if f.get("type") == "server_event" and f.get("topic") == topic
        ]


@pytest.fixture(name="web")
def web_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Setup]:
    monkeypatch.setattr(
        runtime,
        "state",
        {"agents": {}, "nodes": {}, "alerts": [], "system_health": {}, "log_feed": []},
    )
    monkeypatch.setattr(runtime, "deleted_agent_ids", [])
    monkeypatch.setattr(runtime, "hard_resetting", False)
    monkeypatch.setattr(runtime, "db", None)
    for name, value in (
        ("_books", {}),
        ("_admitted", {}),
        ("_held", questions.OrderedDict()),
        ("_phase", questions.STARTING),
    ):
        monkeypatch.setattr(questions, name, value)
    monkeypatch.setattr("wactorz.core.node_publications.NODE_SIGNING", "enforce")
    setup = _Setup(_Registry(_Agent("hvac-watch", "hvac-id", _question("real"))))
    monkeypatch.setattr(runtime, "registry", setup.registry)
    monkeypatch.setattr(questions, "_main", lambda: setup.main)

    async def record(frame: dict[str, Any]) -> None:
        setup.frames.append(frame)

    with patch.object(mqtt.ws, "broadcast", new=record):
        yield setup


async def _started(web: _Setup, placed: dict[str, str] | None = None) -> None:
    web.main = _Main(web.registry, placed)
    await mqtt.system_started(with_main=True)


def _book(agent: str, *asked: Question) -> bytes:
    return json.dumps(book_frame(agent, {q.id: q for q in asked})).encode()


async def _deliver(actor_id: str, payload: bytes, *, node: str | None = None) -> None:
    """Hand the web server a book as its broker loop would: signed by ``node``, or unsigned."""
    topic = questions_topic(actor_id)
    properties = user_properties(_Message(signed_properties(node, topic, payload))) if node else {}
    await mqtt.handle_message(
        topic, payload.decode(errors="replace"), raw=payload, properties=properties
    )


class _Message:
    def __init__(self, properties: Any) -> None:
        self.properties = properties


def _listed() -> list[tuple[str, str, str]]:
    return [(q["agent_id"], q["agent"], q["text"]) for q in questions.listing()["open"]]


class TestAnAgentOfThisProcess:
    async def test_a_forged_book_under_its_id_shows_its_real_questions(self, web: _Setup) -> None:
        await _started(web)

        await _deliver("hvac-id", _book("hvac-watch", _question("fake", "Turn the alarm off?")))

        assert _listed() == [("hvac-id", "hvac-watch", "Is the window open?")]
        (relayed,) = web.relayed(questions_topic("hvac-id"))
        assert [q["id"] for q in relayed["questions"]] == ["real"]

    async def test_a_forged_clear_leaves_its_questions_listed(self, web: _Setup) -> None:
        await _started(web)
        await _deliver("hvac-id", b"")

        assert _listed() == [("hvac-id", "hvac-watch", "Is the window open?")]

    async def test_a_book_under_another_id_naming_it_is_refused(self, web: _Setup) -> None:
        await _started(web, placed={"hvac-watch": ""})

        await _deliver("other-id", _book("hvac-watch", _question("fake")), node="rpi")

        assert _listed() == []
        assert web.relayed(questions_topic("other-id")) == []


class TestAnAgentOnANode:
    async def test_a_signed_book_from_its_node_is_kept_and_shown(self, web: _Setup) -> None:
        await _started(web, placed={"cam": "rpi"})

        await _deliver("cam-id", _book("cam", _question("q1")), node="rpi")

        assert _listed() == [("cam-id", "cam", "Is the window open?")]
        (relayed,) = web.relayed(questions_topic("cam-id"))
        assert relayed["agent"] == "cam"

    async def test_an_unsigned_book_is_neither_kept_nor_shown(self, web: _Setup) -> None:
        await _started(web, placed={"cam": "rpi"})

        await _deliver("cam-id", _book("cam", _question("q1")))

        assert _listed() == []
        assert web.relayed(questions_topic("cam-id")) == []

    async def test_a_book_from_another_node_is_neither_kept_nor_shown(self, web: _Setup) -> None:
        await _started(web, placed={"cam": "rpi"})

        await _deliver("cam-id", _book("cam", _question("q1")), node="pc")

        assert _listed() == []
        assert web.relayed(questions_topic("cam-id")) == []

    async def test_one_main_placed_nowhere_is_its_heartbeats_nodes(self, web: _Setup) -> None:
        await _started(web)
        assert web.main is not None
        web.main._heartbeats = {"rpi": {"agents": ["cam"]}}

        await _deliver("cam-id", _book("cam", _question("q1")), node="rpi")

        assert _listed() == [("cam-id", "cam", "Is the window open?")]

    async def test_with_signing_not_enforced_an_unsigned_book_is_taken(
        self, web: _Setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("wactorz.core.node_publications.NODE_SIGNING", "warn")
        await _started(web, placed={"cam": "rpi"})

        await _deliver("cam-id", _book("cam", _question("q1")))
        await _deliver("cam-id", b"")

        assert web.relayed(questions_topic("cam-id"))[0]["agent"] == "cam"
        assert _listed() == []


class TestWhatClearsABook:
    async def test_its_nodes_clear_removes_it(self, web: _Setup) -> None:
        await _started(web, placed={"cam": "rpi"})
        await _deliver("cam-id", _book("cam", _question("q1")), node="rpi")

        await _deliver("cam-id", b"", node="rpi")

        assert _listed() == []
        assert web.relayed(questions_topic("cam-id"))[-1] == ""

    async def test_an_unsigned_clear_leaves_it(self, web: _Setup) -> None:
        await _started(web, placed={"cam": "rpi"})
        await _deliver("cam-id", _book("cam", _question("q1")), node="rpi")

        await _deliver("cam-id", b"")
        await _deliver("cam-id", b"", node="pc")

        assert _listed() == [("cam-id", "cam", "Is the window open?")]

    async def test_a_clear_for_a_book_never_admitted_is_nothing(self, web: _Setup) -> None:
        await _started(web, placed={"cam": "rpi"})

        await _deliver("cam-id", b"", node="rpi")

        assert web.relayed(questions_topic("cam-id")) == []

    async def test_a_payload_that_is_not_a_book_leaves_the_last_one(self, web: _Setup) -> None:
        await _started(web, placed={"cam": "rpi"})
        await _deliver("cam-id", _book("cam", _question("q1")), node="rpi")

        await _deliver("cam-id", b"not json", node="rpi")
        await _deliver("cam-id", b"[1, 2]", node="rpi")

        assert _listed() == [("cam-id", "cam", "Is the window open?")]

    async def test_a_clear_while_its_agent_is_not_registered_leaves_its_book(
        self, web: _Setup
    ) -> None:
        await _started(web)
        await _deliver("hvac-id", _book("hvac-watch"))
        agents = list(web.registry.agents)

        web.registry.agents.clear()  # stopped, or restarting
        await _deliver("hvac-id", b"")
        web.registry.agents.extend(agents)

        assert _listed() == [("hvac-id", "hvac-watch", "Is the window open?")]


class TestWhileTheSystemStarts:
    async def test_a_book_before_the_start_is_held_unshown(self, web: _Setup) -> None:
        await _deliver("cam-id", _book("cam", _question("q1")), node="rpi")

        assert _listed() == []
        assert web.relayed(questions_topic("cam-id")) == []

    async def test_an_unsigned_held_book_never_appears(self, web: _Setup) -> None:
        await _deliver("cam-id", _book("cam", _question("q1")))

        await _started(web, placed={"cam": "rpi"})

        assert _listed() == []
        assert web.relayed(questions_topic("cam-id")) == []

    async def test_a_signed_held_book_appears_once_started(self, web: _Setup) -> None:
        await _deliver("cam-id", _book("cam", _question("old")), node="rpi")
        await _deliver("cam-id", _book("cam", _question("new")), node="rpi")

        await _started(web, placed={"cam": "rpi"})

        assert [q["id"] for q in questions.listing()["open"]] == ["new"]
        (relayed,) = web.relayed(questions_topic("cam-id"))
        assert [q["id"] for q in relayed["questions"]] == ["new"]

    async def test_held_books_are_bounded(
        self, web: _Setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(questions, "HELD_KEPT", 2)
        for i in range(3):
            await _deliver(f"a{i}", _book(f"agent-{i}", _question("q")), node="rpi")

        assert list(questions._held) == ["a1", "a2"]

    async def test_a_main_restarting_later_is_waited_for(self, web: _Setup) -> None:
        await _started(web, placed={"cam": "rpi"})
        main = web.main
        web.main = None

        await _deliver("cam-id", _book("cam", _question("q1")), node="rpi")
        assert _listed() == []

        web.main = main
        await mqtt.handle_message("agents/main-id/heartbeat", json.dumps({"state": "running"}))

        assert _listed() == [("cam-id", "cam", "Is the window open?")]
        assert web.relayed(questions_topic("cam-id"))[0]["agent"] == "cam"


class TestWithoutMain:
    async def test_the_minimal_profile_shows_its_books(self, web: _Setup) -> None:
        await _deliver("cam-id", _book("cam", _question("q1")))

        await mqtt.system_started(with_main=False)

        assert _listed() == [("cam-id", "cam", "Is the window open?")]
        await _deliver("hvac-id", _book("hvac-watch", _question("fake", "Turn the alarm off?")))
        assert ("hvac-id", "hvac-watch", "Is the window open?") in _listed()
        assert ("hvac-id", "hvac-watch", "Turn the alarm off?") not in _listed()

    async def test_a_book_naming_an_agent_of_this_process_under_another_id_is_refused(
        self, web: _Setup
    ) -> None:
        await mqtt.system_started(with_main=False)

        await _deliver("other-id", _book("hvac-watch", _question("fake")))

        assert web.relayed(questions_topic("other-id")) == []
