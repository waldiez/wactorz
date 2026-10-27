"""Main turns answered questions into labels, taking each book only from where its agent is.

An agent asks with a label spec and publishes its question book; main reads the
book and writes the label the answer makes. A book counts from where the agent
is, as a manifest does: main's own agent is read from the agent itself, and an
agent on a node only from that node, signed. A label is verified only when the
dashboard sent the answer itself.

The recorder is fed one message at a time, as its listener would, against a
real database; the listener itself is driven once through a fake broker.
"""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.node_messages import SignedMessage
from wactorz.agents.main import labels as recorder_module
from wactorz.agents.main.labels import BOOKS_TOPIC, LabelRecorder
from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.core import labels as core_labels
from wactorz.core.actor import ActorState
from wactorz.core.labels import Labels, vouch
from wactorz.core.mqtt import user_properties
from wactorz.core.persistence import WactorzDB
from wactorz.core.persistence.stores import Stores
from wactorz.core.questions import Question, answered, book_frame, questions_topic

if TYPE_CHECKING:
    from wactorz.agents.main.hosts import LabelHost

T0 = 1_760_000_000.0
SOURCE = "sensors/ahu-1"
VALUES = {"Maintenance": "maintenance", "Real problem": "fault"}


class _Moves:
    def moving_to(self, _name: str) -> str | None:
        return None


class _Local:
    def __init__(self, agents: dict[str, Any]) -> None:
        self._agents = agents

    def find_by_name(self, name: str) -> Any | None:
        return self._agents.get(name)


class _OwnAgent:
    """One of main's own agents, keeping its questions itself."""

    def __init__(self, name: str, questions: list[Question], actor_id: str = "hvac-id") -> None:
        self.name = name
        self.actor_id = actor_id
        self._questions = {q.id: q for q in questions}

    def questions(self) -> dict[str, Question]:
        return dict(self._questions)


class _Host:
    """What the recorder reads of main: where agents are, and its own agents."""

    def __init__(
        self,
        placed: dict[str, str] | None = None,
        running: dict[str, Any] | None = None,
        heartbeats: dict[str, list[str]] | None = None,
    ) -> None:
        self.name = "main"
        self.state = ActorState.RUNNING
        self._mqtt_broker = "localhost"
        self._mqtt_port = 1883
        self._registry = _Local(running or {})
        self._spawns = {name: {"node": node} for name, node in (placed or {}).items()}
        self._heartbeats = {node: {"agents": names} for node, names in (heartbeats or {}).items()}
        self.migration = _Moves()

    def _get_spawn_registry(self) -> SpawnRegistry:
        return SpawnRegistry.from_names(self._spawns)

    @property
    def _known_nodes(self) -> dict[str, dict[str, Any]]:
        return self._heartbeats


def _recorder(host: _Host) -> LabelRecorder:
    return LabelRecorder(cast("LabelHost", host))  # _Host stands in for main


@pytest.fixture(autouse=True)
def _no_vouches_from_other_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the dashboard vouched for is process-wide; each test starts with none."""
    monkeypatch.setattr(core_labels, "_vouched", {})


@pytest.fixture(name="db")
def db_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WactorzDB]:
    with WactorzDB(tmp_path / "state" / "wactorz.db") as database:
        monkeypatch.setattr(Stores, "db", database)
        yield database


def asked(question_id: str = "q1", **spec: Any) -> Question:
    return Question(
        id=question_id,
        text="Did something change on AHU-1?",
        options=(*VALUES, "Not sure"),
        channel="telegram",
        asked_at=T0 + 60,
        label={"source": SOURCE, "start": T0, "values": VALUES, **spec},
    )


def picked(option: str, question_id: str = "q1") -> Question:
    question = asked(question_id)
    return answered(question, option, question.options.index(option), "telegram")


def book(agent: str, actor_id: str, questions: list[Question], node: str = "rpi") -> SignedMessage:
    """The book ``agent`` publishes, signed by ``node``."""
    return SignedMessage(
        questions_topic(actor_id),
        json.dumps(book_frame(agent, {q.id: q for q in questions})).encode(),
        node=node,
    )


async def take(recorder: LabelRecorder, message: SignedMessage) -> list[str]:
    return await recorder.receive(message.topic, message.payload, user_properties(message))


def stored(db: WactorzDB) -> list[tuple[str, str, str, bool]]:
    return [(lab.source, lab.value, lab.node, lab.verified) for lab in Labels(db).query()]


class TestAnAgentOnANode:
    async def test_its_answer_becomes_a_label_from_the_node_it_is_placed_on(
        self, db: WactorzDB
    ) -> None:
        recorder = _recorder(_Host(placed={"hvac-watch": "rpi"}))

        written = await take(recorder, book("hvac-watch", "hvac-id", [picked("Maintenance")]))

        assert len(written) == 1
        assert stored(db) == [(SOURCE, "maintenance", "rpi", False)]

    async def test_a_book_from_another_node_is_ignored(self, db: WactorzDB) -> None:
        recorder = _recorder(_Host(placed={"hvac-watch": "rpi"}))

        written = await take(
            recorder, book("hvac-watch", "hvac-id", [picked("Real problem")], node="pc")
        )

        assert written == []
        assert stored(db) == []

    async def test_an_unsigned_book_is_refused(self, db: WactorzDB) -> None:
        recorder = _recorder(_Host(placed={"hvac-watch": "rpi"}))
        message = book("hvac-watch", "hvac-id", [picked("Maintenance")])
        message.signed = False

        assert await recorder.receive(message.topic, message.payload, {}) == []
        assert stored(db) == []

    async def test_an_agent_main_placed_nowhere_is_its_heartbeats_node(self, db: WactorzDB) -> None:
        recorder = _recorder(_Host(heartbeats={"rpi": ["hvac-watch"]}))

        await take(recorder, book("hvac-watch", "hvac-id", [picked("Maintenance")]))

        assert stored(db) == [(SOURCE, "maintenance", "rpi", False)]


class TestAnAgentOnMain:
    async def test_its_own_questions_count_whatever_the_broker_says(self, db: WactorzDB) -> None:
        own = _OwnAgent("hvac-watch", [picked("Real problem")])
        recorder = _recorder(_Host(running={"hvac-watch": own}))

        # Someone on the broker claims the operator said maintenance.
        await take(recorder, book("hvac-watch", "hvac-id", [picked("Maintenance")], node="pc"))

        assert stored(db) == [(SOURCE, "fault", "", False)]

    async def test_a_book_under_another_id_naming_main_s_agent_is_ignored(
        self, db: WactorzDB
    ) -> None:
        """A forged book must not get the real agent's answers recorded early, unverified."""
        vouch("hvac-id", "q1", "Maintenance")
        own = _OwnAgent("hvac-watch", [picked("Maintenance")])
        recorder = _recorder(_Host(running={"hvac-watch": own}))

        forged = await take(recorder, book("hvac-watch", "zzz", [picked("Maintenance")], node="pc"))
        genuine = await take(recorder, book("hvac-watch", "hvac-id", [picked("Maintenance")]))

        assert forged == []
        assert len(genuine) == 1
        assert stored(db) == [(SOURCE, "maintenance", "", True)]


class TestWhatABookMakes:
    async def test_an_answer_the_dashboard_sent_is_verified(self, db: WactorzDB) -> None:
        vouch("hvac-id", "q1", "Maintenance")
        recorder = _recorder(_Host(placed={"hvac-watch": "rpi"}))

        await take(recorder, book("hvac-watch", "hvac-id", [picked("Maintenance")]))

        assert stored(db) == [(SOURCE, "maintenance", "rpi", True)]

    async def test_open_questions_and_not_sure_make_no_label(self, db: WactorzDB) -> None:
        recorder = _recorder(_Host(placed={"hvac-watch": "rpi"}))

        await take(recorder, book("hvac-watch", "hvac-id", [asked("q1"), picked("Not sure", "q2")]))

        assert stored(db) == []

    async def test_a_book_seen_again_writes_nothing_new(self, db: WactorzDB) -> None:
        recorder = _recorder(_Host(placed={"hvac-watch": "rpi"}))
        message = book("hvac-watch", "hvac-id", [picked("Maintenance")])

        await take(recorder, message)
        again = await take(recorder, book("hvac-watch", "hvac-id", [picked("Maintenance")]))

        assert again == []
        assert len(Labels(db).query(include_superseded=True)) == 1

    async def test_one_answer_that_cannot_be_recorded_does_not_stop_the_others(
        self, db: WactorzDB, caplog: pytest.LogCaptureFixture
    ) -> None:
        recorder = _recorder(_Host(placed={"hvac-watch": "rpi"}))
        broken = picked("Maintenance", "q1")
        broken = Question(
            **{
                **broken.to_dict(),
                "options": broken.options,
                "label": {"source": "sensors/#", "start": T0, "values": VALUES},
            }
        )
        fine = picked("Real problem", "q2")

        written = await take(recorder, book("hvac-watch", "hvac-id", [broken, fine]))
        again = await take(recorder, book("hvac-watch", "hvac-id", [broken, fine]))

        assert len(written) == 1
        assert again == []
        assert stored(db) == [(SOURCE, "fault", "rpi", False)]

    async def test_answers_from_one_book_are_written_in_the_order_they_were_answered(
        self, db: WactorzDB
    ) -> None:
        recorder = _recorder(_Host(placed={"hvac-watch": "rpi"}))
        first = answered(asked("q1"), "Maintenance", 0, "telegram")
        later = answered(asked("q2"), "Real problem", 1, "telegram")
        # Listed the other way round in the book, answered in this order.
        first = Question(**{**first.to_dict(), "options": first.options, "answered_at": T0 + 100})
        later = Question(**{**later.to_dict(), "options": later.options, "answered_at": T0 + 200})

        await take(recorder, book("hvac-watch", "hvac-id", [later, first]))

        held = [i.label.value for i in Labels(db).effective(SOURCE)]
        assert held == ["fault"]

    async def test_a_cleared_book_or_one_that_is_not_json_is_nothing(self, db: WactorzDB) -> None:
        recorder = _recorder(_Host())

        assert await recorder.receive(questions_topic("hvac-id"), b"") == []
        assert await recorder.receive(questions_topic("hvac-id"), b"{not json") == []
        assert stored(db) == []

    async def test_without_persistence_nothing_is_written(
        self, db: WactorzDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Stores, "db", None)
        recorder = _recorder(_Host(placed={"hvac-watch": "rpi"}))

        assert await take(recorder, book("hvac-watch", "hvac-id", [picked("Maintenance")])) == []


class _Client:
    def __init__(self, messages: list[SignedMessage], host: _Host) -> None:
        self._messages = messages
        self._host = host
        self.subscribed: list[str] = []

    async def subscribe(self, topic: str, qos: int = 0, **_kwargs: Any) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for message in self._messages:
            yield message
        self._host.state = ActorState.STOPPED


class _Broker:
    def __init__(self, client: _Client) -> None:
        self.client = client

    def __call__(self, _host: str, _port: int, **_kwargs: Any) -> "_Broker":
        return self

    async def __aenter__(self) -> _Client:
        return self.client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


async def test_the_listener_follows_every_book_and_records_what_it_brings(
    db: WactorzDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _Host(placed={"hvac-watch": "rpi"})
    client = _Client([book("hvac-watch", "hvac-id", [picked("Maintenance")])], host)
    monkeypatch.setattr(recorder_module, "mqtt_client", _Broker(client))

    await asyncio.wait_for(_recorder(host).listener(), timeout=5)

    assert client.subscribed == [BOOKS_TOPIC]
    assert stored(db) == [(SOURCE, "maintenance", "rpi", False)]
