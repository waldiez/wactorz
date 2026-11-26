# SPDX-License-Identifier: Apache-2.0
"""A question asked for words by an agent on a node, answered from the dashboard.

The answer never travels on the agent's own answer topic, which anything on
the broker can write to. Main sends it over the node's signed control topic,
``nodes/<node>/answer``, and the node takes it only while it enforces main's
signature. Everything that makes that safe is checked again for every answer:
that main places the agent there, that the node is up, says it takes answers
in words and enforces the signature. A move follows the same rules.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from tests.test_node_runner import RecordingRunner
from wactorz import plugins
from wactorz.agents.main.migration import Migration, words_in_state
from wactorz.agents.main.nodes import NodeManager
from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.agents.main.typed_answers import BEING_MOVED, forward_words
from wactorz.core import node_signing
from wactorz.core.actor import Actor, Message
from wactorz.core.manifests import NodeManifest
from wactorz.core.outbound import Answer, OptionsError
from wactorz.core.questions import QUESTIONS_KEY, Question, book_of
from wactorz.node import signing as node_signing_side
from wactorz.node.hosted import NodeHosted

QUESTION = "What did you change on the AHU?"


@pytest.fixture(autouse=True)
def _fresh_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test is an install of its own: its own state directory and signing secret."""
    state = tmp_path / "state"
    monkeypatch.setenv("WACTORZ_STATE_DIR", str(state))
    monkeypatch.setattr(node_signing, "_secret", None)
    monkeypatch.setattr(node_signing, "_last_sequence", None)
    return state


def _guard(
    tmp_path: Path, mode: str = "enforce", key: bool = True
) -> node_signing_side.ControlGuard:
    node_dir = tmp_path / "node-rpi"
    node_dir.mkdir(exist_ok=True)
    return node_signing_side.ControlGuard(
        node_signing.node_key("rpi") if key else "", "", mode, str(node_dir)
    )


def _words(question_id: str = "q1", answer: str | None = None) -> Question:
    return Question(
        id=question_id,
        text=QUESTION,
        options=(),
        channel="dashboard",
        free_text=True,
        max_chars=4000,
        answer=answer,
    )


class TestTheSignedTopic:
    def test_it_is_one_main_signs_and_an_enforcing_node_checks(self, tmp_path: Path) -> None:
        payload = json.dumps({"agent": "typist", "answer": {"id": "q1", "option": "x"}}).encode()
        properties = node_signing.node_control_properties("nodes/rpi/answer", payload)
        assert properties is not None

        assert _guard(tmp_path).admit("answer", "nodes/rpi/answer", payload, dict(properties))

    def test_one_unsigned_or_forged_is_refused(self, tmp_path: Path) -> None:
        payload = b'{"agent": "typist", "answer": {"id": "q1", "option": "Forged."}}'
        guard = _guard(tmp_path)

        assert not guard.admit("answer", "nodes/rpi/answer", payload, {})
        signed = dict(node_signing.node_control_properties("nodes/rpi/answer", b"{}") or {})
        assert not guard.admit("answer", "nodes/rpi/answer", payload, signed), (
            "signed for other bytes"
        )

    def test_one_replayed_is_refused(self, tmp_path: Path) -> None:
        payload = b'{"agent": "typist", "answer": {"id": "q1", "option": "Once."}}'
        properties = dict(node_signing.node_control_properties("nodes/rpi/answer", payload) or {})
        guard = _guard(tmp_path)

        assert guard.admit("answer", "nodes/rpi/answer", payload, properties)
        assert not guard.admit("answer", "nodes/rpi/answer", payload, properties)


class _Asker(Actor):
    """An agent that asks for words as it starts, and keeps what it was given."""

    given: list[str]

    async def on_start(self) -> None:
        self.given = []
        self._asking = asyncio.create_task(self._ask())

    async def _ask(self) -> None:
        self.given.append(await self.ask_operator(QUESTION, free_text=True, id="q1"))

    async def handle_message(self, msg: Message) -> None:
        return None


@pytest.fixture(name="runner")
async def runner_fixture(tmp_path: Path) -> AsyncIterator[RecordingRunner]:
    runner = RecordingRunner(tmp_path / "node-state")
    runner._control = _guard(tmp_path)
    await runner.supervisor.start()
    try:
        yield runner
    finally:
        await runner.stop_all()
        if runner.supervisor._watch_task:
            runner.supervisor._watch_task.cancel()


async def _hosted(runner: RecordingRunner) -> NodeHosted:
    with plugins.registered([_Asker]) as (plugin,):
        await runner.spawn_agent({"name": "asker", "type": "module", "target": plugin.target})
    hosted = runner.hosted("asker")
    assert hosted is not None
    for _ in range(100):
        if hosted.questions():
            break
        await asyncio.sleep(0.01)
    return hosted


def _forwarded(hosted: NodeHosted, words: str) -> dict[str, Any]:
    answer = Answer(id="q1", agent_id=hosted.actor_id, option=words, index=0, channel="dashboard")
    return {"agent": hosted.name, "actor_id": hosted.actor_id, "answer": answer.to_payload()}


class TestTheNode:
    async def test_it_says_it_takes_answers_in_words(self, runner: RecordingRunner) -> None:
        await runner._publish_manifest()

        (manifest,) = [d for t, d, _r in runner.published if t == "nodes/rpi/manifest"]
        assert manifest["free_text_answers"] is True

    @pytest.mark.parametrize(
        ("mode", "key", "takes"),
        [("enforce", True, True), ("warn", True, False), ("enforce", False, False)],
    )
    def test_its_agents_may_ask_only_while_it_enforces_the_signature(
        self, tmp_path: Path, mode: str, key: bool, takes: bool
    ) -> None:
        runner = RecordingRunner(tmp_path / "node-state")
        runner._control = _guard(tmp_path, mode, key)
        actor = _Asker(name="asker")

        runner._inject(actor)

        assert actor.takes_words is takes

    async def test_an_agent_on_a_node_that_does_not_enforce_it_cannot_ask(
        self, tmp_path: Path
    ) -> None:
        runner = RecordingRunner(tmp_path / "node-state")
        runner._control = _guard(tmp_path, "warn")
        actor = _Asker(name="asker")
        runner._inject(actor)

        with pytest.raises(OptionsError, match="does not enforce main's signature"):
            await actor.ask_operator(QUESTION, free_text=True)

    async def test_the_words_main_forwards_reach_the_agent_and_its_answer_topic(
        self, runner: RecordingRunner
    ) -> None:
        hosted = await _hosted(runner)

        await runner._on_answer("nodes/rpi/answer", _forwarded(hosted, "Swapped the belt."), None)

        assert hosted.questions()["q1"].answer == "Swapped the belt."
        for _ in range(100):
            if getattr(hosted, "given", None):
                break
            await asyncio.sleep(0.01)
        assert hosted.given == ["Swapped the belt."]  # pyright: ignore[reportAttributeAccessIssue]  # the asker's own record
        topics = [t for t, _d, _r in runner.published]
        echo = topics.index(f"agents/{hosted.actor_id}/answer")
        last_book = max(
            i for i, t in enumerate(topics[:echo]) if t == f"agents/{hosted.actor_id}/questions"
        )
        assert last_book < echo, "the book records it before it is published"

    async def test_one_that_stopped_enforcing_takes_none(
        self, runner: RecordingRunner, tmp_path: Path
    ) -> None:
        # Asked while it enforced; the node runs on without, after a change of its settings.
        hosted = await _hosted(runner)
        runner._control = _guard(tmp_path, "warn")

        await runner._on_answer("nodes/rpi/answer", _forwarded(hosted, "Forged."), None)

        assert hosted.questions()["q1"].open

    async def test_one_for_an_agent_not_here_is_dropped(self, runner: RecordingRunner) -> None:
        hosted = await _hosted(runner)
        wrong = {**_forwarded(hosted, "x"), "actor_id": "someone-else"}

        await runner._on_answer("nodes/rpi/answer", wrong, None)

        assert hosted.questions()["q1"].open


class _Main:
    """What forwarding reads of main: where agents are, the nodes, and its signed publish."""

    def __init__(
        self,
        *,
        placed: str = "rpi",
        manifest: dict[str, Any] | None = None,
        signing: str = "enforce",
        online: bool = True,
        agent: str = "typist",
    ) -> None:
        self._registry = None
        self.nodes = NodeManager(self, None)  # pyright: ignore[reportArgumentType]  # only its tables are read
        self.nodes.known["rpi"] = {"last_seen": time.time() if online else 0.0, "signing": signing}
        given = manifest if manifest is not None else {"free_text_answers": True}
        self.nodes.node_manifests["rpi"] = cast(NodeManifest, given)
        self._spawns = {agent: {"name": agent, "node": placed}} if placed else {}
        self.published: list[tuple[str, Any]] = []
        #: Main's moves, as a real one records them.
        self.migration = Migration()

    def _get_spawn_registry(self) -> SpawnRegistry:
        return SpawnRegistry.from_names(dict(self._spawns))

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload))


def _answer() -> Answer:
    return Answer(id="q1", agent_id="typist-id", option="Swapped it.", index=0, channel="dashboard")


class TestMainForwarding:
    async def test_it_goes_signed_to_the_node_the_agent_is_on(self) -> None:
        main = _Main()

        assert await forward_words(main, "typist", _answer()) is None  # pyright: ignore[reportArgumentType]  # main's surface

        ((topic, payload),) = main.published
        assert topic == "nodes/rpi/answer"
        assert node_signing.node_control_properties(topic, json.dumps(payload).encode()) is not None
        assert payload["agent"] == "typist" and payload["answer"]["option"] == "Swapped it."

    @pytest.mark.parametrize(
        ("over", "reason"),
        [
            pytest.param({"placed": ""}, "neither", id="placed nowhere"),
            pytest.param({"online": False}, "not answering", id="node down"),
            pytest.param({"manifest": {}}, "deploy it again", id="an older node"),
            pytest.param({"signing": "warn"}, "does not enforce", id="signing warn"),
            pytest.param({"signing": "off"}, "does not enforce", id="no key"),
        ],
    )
    async def test_it_is_refused_with_the_reason_and_nothing_goes(
        self, over: dict[str, Any], reason: str
    ) -> None:
        # Built here, not when the module is read: the node's last heartbeat is "now".
        main = _Main(**over)

        refused = await forward_words(main, "typist", _answer())  # pyright: ignore[reportArgumentType]  # main's surface

        assert refused is not None and reason in refused
        assert main.published == []


class TestDuringAMove:
    """While the agent is being moved, no copy of it is sure to be the one that runs.

    The registry still names the source, which has stopped the agent or is
    about to, and the target has not confirmed it. Words given to either could
    be lost, so they are refused, to be sent again once the move is done.
    """

    @pytest.mark.parametrize(
        ("pending", "move"),
        [
            pytest.param(
                "pending_returns", {"from_node": "rpi", "target_node": ""}, id="node to main"
            ),
            pytest.param(
                "pending_spawns", {"from_node": "rpi", "target_node": "nuc"}, id="node to node"
            ),
            pytest.param(
                "pending_returns",
                {"from_node": "rpi", "target_node": "nuc"},
                id="handed back on the way",
            ),
        ],
    )
    async def test_an_answer_is_refused_to_retry_and_nothing_goes(
        self, pending: str, move: dict[str, Any]
    ) -> None:
        main = _Main()
        getattr(main.migration, pending)["t1"] = {"agent_name": "typist", **move}

        refused = await forward_words(main, "typist", _answer())  # pyright: ignore[reportArgumentType]  # main's surface

        assert refused == BEING_MOVED
        assert main.published == [], "never to the stopped source"

    async def test_a_move_in_hand_counts_too(self) -> None:
        main = _Main()
        main.migration._in_hand[("typist", "rpi", "nuc")] += 1

        assert await forward_words(main, "typist", _answer()) == BEING_MOVED  # pyright: ignore[reportArgumentType]  # main's surface

    async def test_another_agents_move_holds_nothing(self) -> None:
        main = _Main()
        main.migration.pending_returns["t1"] = {"agent_name": "someone-else", "from_node": "rpi"}

        assert await forward_words(main, "typist", _answer()) is None  # pyright: ignore[reportArgumentType]  # main's surface


class TestMoves:
    """The rules for a move, read from the target node now: capability and signing."""

    def test_a_node_that_takes_words_is_none_of_why_not(self) -> None:
        assert _Main().nodes.why_no_words("rpi") is None
        assert "deploy it again" in (_Main(manifest={}).nodes.why_no_words("rpi") or "")
        assert "does not enforce" in (_Main(signing="warn").nodes.why_no_words("rpi") or "")

    def test_state_handed_back_with_an_open_question_is_held_for_a_node_that_cannot(self) -> None:
        state = {QUESTIONS_KEY: book_of({"q1": _words()})}

        assert words_in_state(state, "node 'nuc' does not enforce main's signature") is not None
        assert words_in_state(state, None) is None, "a target that takes words"
        answered = {QUESTIONS_KEY: book_of({"q1": _words(answer="x")})}
        assert words_in_state(answered, "it cannot") is None
