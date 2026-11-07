# SPDX-License-Identifier: Apache-2.0
"""What each way of reaching an agent from the chat says, in order, and how the turn ends.

A chat turn reaches an agent through one of several entry points, chosen by
what the agent has: a stream, a single answer, a ``chat`` method, or only a
mailbox. An agent on a node is reached over the broker instead. Each test here
records everything one turn said, replies, streamed chunks and the turn's
ending alike, in the order it said them: the sender holds its composer until
the ending arrives, so a turn must end exactly once, after its last word.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.core.actor import ActorState, Message
from wactorz.core.addresses import Address, Resolution
from wactorz.web import chat, runtime

FILES = [{"name": "notes.txt"}]
"""The stored records of one attachment, as the caller resolves them."""


class _Turn:
    """Everything one chat turn said, in order: ``("reply", text)``, ``("chunk", text)``, ``("end",)``."""

    def __init__(self) -> None:
        self.said: list[tuple[str, ...]] = []

    async def reply(self, text: str) -> None:
        self.said.append(("reply", text))

    async def chunk(self, text: str) -> None:
        self.said.append(("chunk", text))

    async def end(self) -> None:
        self.said.append(("end",))


async def _say(
    content: str, attachments: list[dict[str, Any]] | None = None
) -> list[tuple[str, ...]]:
    turn = _Turn()
    await chat.route_chat(content, turn.reply, turn.chunk, turn.end, attachments=attachments)
    return turn.said


# ── Agents in this process ─────────────────────────────────────────────────


class _Agents:
    """A registry of a few agents, with the reply slot a mailbox-only agent answers into."""

    def __init__(self, *agents: Any) -> None:
        self._by_name = {agent.name: agent for agent in agents}
        self.slot: asyncio.Future[Any] | None = None

    def get(self, _actor_id: str) -> None:
        return None

    def find_by_name(self, name: str) -> Any:
        return self._by_name.get(name)

    def all_actors(self) -> list[Any]:
        return list(self._by_name.values())

    @asynccontextmanager
    async def reply_slot(self) -> AsyncIterator[tuple[str, asyncio.Future[Any]]]:
        self.slot = asyncio.get_running_loop().create_future()
        yield "reply-slot:test", self.slot


class _Streamer:
    """An agent that streams its answer and reads no attachments."""

    name = "streamer"
    state = ActorState.RUNNING

    async def process_user_input_stream(self, text: str) -> AsyncIterator[Any]:
        yield f"heard {text}"
        yield {"type": "progress"}  # not words: never said
        yield 42


class _FileReader:
    """An agent that streams its answer and reads the attachments it is given."""

    name = "reader"
    state = ActorState.RUNNING

    def __init__(self) -> None:
        self.given: list[Any] = []

    async def process_user_input_stream(
        self, text: str, attachments: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[str]:
        self.given.append(attachments)
        yield text


class _Answerer:
    """An agent that gives one answer, not a stream."""

    name = "answerer"
    state = ActorState.RUNNING

    async def process_user_input(self, text: str) -> int:
        return len(text)


class _Chatter:
    """An agent with a ``chat`` method and nothing else to talk through."""

    name = "chatter"
    state = ActorState.RUNNING

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure

    async def chat(self, text: str) -> str:
        if self.failure is not None:
            raise self.failure
        return text.upper()


class _Tasked:
    """An agent reached only through its mailbox, which answers into the reply slot."""

    name = "tasked"
    state = ActorState.RUNNING

    def __init__(
        self, agents: _Agents, result: Any = None, failure: BaseException | None = None
    ) -> None:
        self.agents = agents
        self.result = result
        self.failure = failure
        self.received: list[Message] = []

    async def handle_message(self, message: Message) -> None:
        self.received.append(message)
        if self.failure is not None:
            raise self.failure
        assert self.agents.slot is not None
        self.agents.slot.set_result(self.result)


class _DynamicTasked(_Tasked):
    """A generated agent: it has a ``chat``, but its tasks go through its handler."""

    name = "dynamic"
    _fn_handle_task = None

    async def chat(self, text: str) -> str:
        raise AssertionError(text)


@pytest.fixture(autouse=True)
def _restore_runtime() -> Iterator[None]:
    registry, orchestrator = runtime.registry, runtime.orchestrator
    runtime.orchestrator = None
    yield
    runtime.registry, runtime.orchestrator = registry, orchestrator


@pytest.fixture(autouse=True)
def _files_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attachments read into one content block each, without a disk behind them."""
    monkeypatch.setattr(
        chat,
        "to_blocks",
        lambda records, _read: [{"type": "text", "text": r["name"]} for r in records],
    )


@pytest.fixture(name="no_banner", autouse=True)
def no_banner_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "experimental_first_use_banner", lambda _name: None)


class TestAStreamingAgent:
    async def test_its_chunks_are_said_as_text_and_the_turn_ends_once(self) -> None:
        runtime.registry = _Agents(_Streamer())

        assert await _say("@streamer hi") == [("chunk", "heard hi"), ("chunk", "42"), ("end",)]

    async def test_files_it_cannot_read_are_named_before_it_answers(self) -> None:
        runtime.registry = _Agents(_Streamer())

        said = await _say("@streamer hi", FILES)

        assert said == [
            ("chunk", "[note] notes.txt not sent — @streamer cannot read attachments."),
            ("chunk", "heard hi"),
            ("chunk", "42"),
            ("end",),
        ]

    async def test_one_that_reads_files_is_given_them(self) -> None:
        reader = _FileReader()
        runtime.registry = _Agents(reader)

        said = await _say("@reader hi", FILES)

        assert said == [("chunk", "hi"), ("end",)]
        assert reader.given == [[{"type": "text", "text": "notes.txt"}]]

    async def test_one_given_no_files_is_not_passed_the_argument(self) -> None:
        reader = _FileReader()
        runtime.registry = _Agents(reader)

        await _say("@reader hi")

        assert reader.given == [None]

    async def test_an_experimental_agent_warns_first_on_the_same_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime.registry = _Agents(_Streamer())
        monkeypatch.setattr(chat, "experimental_first_use_banner", lambda name: f"beta: {name}\n\n")

        said = await _say("@streamer hi")

        assert said[0] == ("chunk", "beta: streamer\n\n")
        assert said[-1] == ("end",)


class TestAnAgentThatAnswersOnce:
    async def test_its_answer_is_replied_as_text(self) -> None:
        runtime.registry = _Agents(_Answerer())

        assert await _say("@answerer four") == [("reply", "4"), ("end",)]

    async def test_files_are_named_as_not_sent(self) -> None:
        runtime.registry = _Agents(_Answerer())

        said = await _say("@answerer four", FILES)

        assert said == [
            ("chunk", "[note] notes.txt not sent — @answerer cannot read attachments."),
            ("reply", "4"),
            ("end",),
        ]


class TestAnAgentWithAChatMethod:
    async def test_its_answer_is_replied(self) -> None:
        runtime.registry = _Agents(_Chatter())

        assert await _say("@chatter hi", FILES) == [
            ("chunk", "[note] notes.txt not sent — @chatter cannot read attachments."),
            ("reply", "HI"),
            ("end",),
        ]

    async def test_a_failure_is_said_and_the_turn_still_ends(self) -> None:
        runtime.registry = _Agents(_Chatter(RuntimeError("no model")))

        assert await _say("@chatter hi") == [("reply", "[error] chatter: no model"), ("end",)]


class TestAnAgentReachedThroughItsMailbox:
    async def test_its_result_is_replied(self) -> None:
        agents = _Agents()
        tasked = _Tasked(agents, result={"result": "done"})
        agents._by_name["tasked"] = tasked
        runtime.registry = agents

        said = await _say("@tasked go", FILES)

        assert said == [
            ("chunk", "[note] notes.txt not sent — @tasked cannot read attachments."),
            ("reply", "done"),
            ("end",),
        ]
        (message,) = tasked.received
        assert message.sender_id == message.reply_to == "reply-slot:test"

    async def test_a_generated_agent_is_tasked_rather_than_chatted_with(self) -> None:
        agents = _Agents()
        agents._by_name["dynamic"] = _DynamicTasked(agents, result={"result": "done"})
        runtime.registry = agents

        assert await _say("@dynamic go") == [("reply", "done"), ("end",)]

    async def test_a_list_of_agents_is_said_as_the_catalog_says_it(self) -> None:
        payload = {"agents": [{"name": "weather-agent", "description": "the forecast"}]}
        agents = _Agents()
        agents._by_name["tasked"] = _Tasked(agents, result=payload)
        runtime.registry = agents

        said = await _say("@tasked list")

        assert said == [("reply", chat.format_catalog_agents_response(payload)), ("end",)]

    async def test_no_reply_in_time_is_said(self) -> None:
        agents = _Agents()
        agents._by_name["tasked"] = _Tasked(agents, failure=asyncio.TimeoutError())
        runtime.registry = agents

        assert await _say("@tasked go") == [
            ("reply", "[error] @tasked did not reply within 150s."),
            ("end",),
        ]

    async def test_a_failure_is_said(self) -> None:
        agents = _Agents()
        agents._by_name["tasked"] = _Tasked(agents, failure=RuntimeError("bad task"))
        runtime.registry = agents

        assert await _say("@tasked go") == [("reply", "[error] tasked: bad task"), ("end",)]

    async def test_a_registry_gone_mid_turn_is_said(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A shutdown under way can take the registry between finding the agent
        # and asking it.
        tasked = _Tasked(_Agents(), result={"result": "done"})
        found = chat.Destination(chat.chat_metrics.LOCAL, "tasked", "go", tasked)
        monkeypatch.setattr(chat, "destination_of", lambda _content: found)
        runtime.registry = None

        assert await _say("@tasked go") == [("reply", "[error] registry not available"), ("end",)]
        assert tasked.received == []


class TestACommand:
    async def test_files_sent_with_it_are_named_as_not_sent(self) -> None:
        runtime.registry = _Agents()

        said = await _say("/no-such-command", FILES)

        assert said == [
            ("chunk", "[note] notes.txt not sent — a command does not take attachments."),
            ("reply", "Unknown command. Type /help for available commands."),
            ("end",),
        ]

    @pytest.mark.parametrize(
        ("command", "answer"),
        [
            ("/no-such-command", "Unknown command. Type /help for available commands."),
            ("/agents", "No agents running."),
            ("/nodes", "Nodes:\n  local    online   (none)"),
            ("/clear-plans", "[System: Plan cache cleared.]"),
            ("/deploy rpi", f"[error] {chat.NO_MAIN_FOR_NODES}"),
        ],
    )
    async def test_one_answered_here_ends_the_turn_once(self, command: str, answer: str) -> None:
        # Without main or an orchestrator, this server answers every one of these itself.
        runtime.registry = _Agents()

        assert await _say(command) == [("reply", answer), ("end",)]


# ── An agent on a node ─────────────────────────────────────────────────────


class _Broker:
    """A connection on which the given messages arrive, then ``after`` is raised, if given."""

    def __init__(self, payloads: list[bytes], after: BaseException | None = None) -> None:
        self.payloads = payloads
        self.after = after
        self.subscribed: list[str] = []

    async def __aenter__(self) -> "_Broker":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, **_kwargs: Any) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> AsyncIterator[Any]:
        return self._arrive()

    async def _arrive(self) -> AsyncIterator[Any]:
        for payload in self.payloads:
            yield SimpleNamespace(payload=payload)
        if self.after is not None:
            raise self.after


@pytest.fixture(name="node")
def node_fixture(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """``@counter`` runs on the node ``edge``; whether that node takes tasks is ``node.refusal``."""
    node = SimpleNamespace(refusal=None, published=[])

    async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
        node.published.append((topic, payload))

    main = SimpleNamespace(
        _mqtt_broker="broker.invalid",
        _mqtt_port=1883,
        nodes=SimpleNamespace(refuses_tasks=lambda _node: node.refusal),
        task_refusal=lambda _address: None,
        task_topic_for=lambda address: f"agents/at/{address.node}/{address.name}/task",
        _mqtt_publish=_publish,
    )
    runtime.registry = SimpleNamespace(find_by_name=lambda _name: None)
    monkeypatch.setattr(chat, "find_main_actor", lambda _registry: main)
    monkeypatch.setattr(
        chat, "resolution_of", lambda name: Resolution(name, (Address("counter", "edge"),))
    )
    # Signed by the node unless it says it was forged.
    monkeypatch.setattr(chat, "user_properties", lambda _msg: {})
    monkeypatch.setattr(
        chat, "admit", lambda _node, _topic, payload, _props, _what: payload != b"forged"
    )
    return node


class TestAnAgentOnANode:
    async def test_a_node_that_takes_no_tasks_is_not_asked(self, node: SimpleNamespace) -> None:
        node.refusal = "edge holds no signing key"

        said = await _say("@counter four")

        assert said == [
            ("reply", "[error] @counter was not asked: edge holds no signing key."),
            ("end",),
        ]
        assert node.published == []

    async def test_the_signed_reply_is_said_and_others_are_not(
        self, node: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _Broker([b"forged", b'{"result": "4"}'])
        monkeypatch.setattr(chat, "mqtt_client", lambda *_a, **_k: broker)

        said = await _say("@counter four")

        assert said == [("reply", "4"), ("end",)]
        ((topic, payload),) = node.published
        assert topic == "agents/at/edge/counter/task"
        assert payload["text"] == payload["payload"] == "four"
        assert payload["_remote_task"] is True
        # Listened for before it was asked, on the topic the task names.
        assert broker.subscribed == [payload["_reply_topic"]]

    async def test_a_reply_that_is_not_json_is_said_as_it_came(
        self, node: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chat, "mqtt_client", lambda *_a, **_k: _Broker([b"four, plainly"]))

        assert await _say("@counter four") == [("reply", "four, plainly"), ("end",)]

    async def test_files_are_named_as_not_sent_and_the_task_still_goes(
        self, node: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chat, "mqtt_client", lambda *_a, **_k: _Broker([b'{"result": "4"}']))

        said = await _say("@counter four", FILES)

        assert said == [
            ("chunk", "[note] notes.txt not sent — @counter runs on edge."),
            ("reply", "4"),
            ("end",),
        ]
        assert len(node.published) == 1

    async def test_a_connection_that_closes_before_a_reply_is_said(
        self, node: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chat, "mqtt_client", lambda *_a, **_k: _Broker([b"forged"]))

        assert await _say("@counter four") == [
            ("reply", "[error] The connection closed before @counter on edge replied."),
            ("end",),
        ]

    async def test_no_reply_in_time_is_said(
        self, node: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broker = _Broker([b"forged"], after=asyncio.TimeoutError())
        monkeypatch.setattr(chat, "mqtt_client", lambda *_a, **_k: broker)

        assert await _say("@counter four") == [
            ("reply", "[error] @counter on edge did not reply within 150s."),
            ("end",),
        ]
