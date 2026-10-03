"""A program a node repaired reaches main's spawn registry, and only that way.

An agent can be repaired by the LLM while it runs. On a node that repair lived
on the node, so a reboot was sent the original break and paid to fix it again.

The obvious route — the node publishes its new code and main files it — is the
one this must not take: main executes what a node sends, so an unsolicited write
lets one compromised node put code into the registry for any agent it hosts, and
that code runs wherever the agent goes next. So the node volunteers a notice and
never the program, and main acts on code only for a token it minted itself.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.agents.main import code_refresh as code_refresh_mod
from wactorz.agents.main.code_refresh import ASK_INTERVAL_S, TOKEN_TTL_S, CodeRefresh
from wactorz.core.node_signing import CONTROL_LEAVES

BROKEN = "async def process(agent):\n    boom()\n"
REPAIRED = "async def process(agent):\n    pass  # repaired\n"


class _Main:
    """The reach `CodeRefreshHost` declares, and nothing else."""

    name = "main"
    _mqtt_broker = "localhost"
    _mqtt_port = 1883

    def __init__(self) -> None:
        self.state = SimpleNamespace(value="running")
        self.registry: dict[str, dict[str, Any]] = {
            "collector": {"name": "collector", "node": "rpi", "code": BROKEN}
        }
        self.published: list[tuple[str, Any]] = []

    def _get_spawn_registry(self) -> dict[str, dict[str, Any]]:
        return self.registry

    def _save_to_spawn_registry(self, config: dict[str, Any]) -> None:
        self.registry[config["name"]] = config

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload))


@pytest.fixture(name="main")
def main_fixture() -> _Main:
    return _Main()


@pytest.fixture(name="refresh")
def refresh_fixture(main: _Main) -> CodeRefresh:
    return CodeRefresh(main)  # pyright: ignore[reportArgumentType]  # the protocol's reach, no more


def _answer(refresh: CodeRefresh, main: _Main, **overrides: Any) -> bytes:
    """What a node would send back, quoting the token main just minted."""
    (token,) = refresh.pending
    payload = {"agent": "collector", "token": token, "code": REPAIRED, "node": "rpi"}
    return json.dumps({**payload, **overrides}).encode()


class TestMainAsksBeforeItBelieves:
    async def test_a_notice_makes_main_ask(self, refresh: CodeRefresh, main: _Main) -> None:
        await refresh.note_change("rpi", "collector")

        (topic, payload) = main.published[-1]
        assert topic == "nodes/rpi/code_request"
        assert payload["agent"] == "collector"
        assert payload["token"]

    def test_the_request_is_a_signed_control_topic(self) -> None:
        # It commands nothing, but what makes the answer usable is that main
        # asked — so the asking is worth being sure of.
        assert "code_request" in CONTROL_LEAVES

    async def test_it_files_the_answer_it_asked_for(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        await refresh.note_change("rpi", "collector")

        await refresh.receive_code_return("nodes/rpi/code_return", _answer(refresh, main))

        assert main.registry["collector"]["code"] == REPAIRED

    async def test_a_notice_for_an_agent_main_never_placed_is_ignored(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        await refresh.note_change("rpi", "not-an-agent-of-ours")

        assert main.published == []
        assert refresh.pending == {}

    async def test_a_notice_from_a_node_that_does_not_host_it_is_ignored(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        # A node may speak for the agents main put on it, and for no others.
        await refresh.note_change("some-other-node", "collector")

        assert main.published == []


class TestWhatIsRefused:
    async def test_code_nobody_asked_for(self, refresh: CodeRefresh, main: _Main) -> None:
        forged = json.dumps({"agent": "collector", "token": "deadbeef", "code": "evil()"})

        await refresh.receive_code_return("nodes/rpi/code_return", forged.encode())

        assert main.registry["collector"]["code"] == BROKEN

    async def test_the_same_answer_twice(self, refresh: CodeRefresh, main: _Main) -> None:
        # Spent on use, so a captured reply cannot be played back later.
        await refresh.note_change("rpi", "collector")
        answer = _answer(refresh, main)
        await refresh.receive_code_return("nodes/rpi/code_return", answer)
        main.registry["collector"]["code"] = BROKEN

        await refresh.receive_code_return("nodes/rpi/code_return", answer)

        assert main.registry["collector"]["code"] == BROKEN

    async def test_an_answer_about_a_different_agent(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        # A token is for one agent on one node. Answering with another agent's
        # name would file this code against whatever the token was for.
        main.registry["other"] = {"name": "other", "node": "rpi", "code": BROKEN}
        await refresh.note_change("rpi", "collector")

        await refresh.receive_code_return(
            "nodes/rpi/code_return", _answer(refresh, main, agent="other")
        )

        assert main.registry["collector"]["code"] == BROKEN
        assert main.registry["other"]["code"] == BROKEN

    async def test_an_answer_from_a_different_node(self, refresh: CodeRefresh, main: _Main) -> None:
        await refresh.note_change("rpi", "collector")
        (token,) = refresh.pending

        await refresh.receive_code_return("nodes/elsewhere/code_return", _answer(refresh, main))

        assert main.registry["collector"]["code"] == BROKEN
        # And the question stands, as it does for a wrong agent name. Both are
        # the same refusal today; asserting only one of them would let a later
        # split of that check quietly spend the token on this half, which is
        # how a stranger cancels the exchange.
        assert token in refresh.pending

    async def test_an_answer_that_came_too_late(self, refresh: CodeRefresh, main: _Main) -> None:
        await refresh.note_change("rpi", "collector")
        answer = _answer(refresh, main)
        for asked in refresh.pending.values():
            asked["asked_at"] = time.time() - TOKEN_TTL_S - 1

        await refresh.receive_code_return("nodes/rpi/code_return", answer)

        assert main.registry["collector"]["code"] == BROKEN

    @pytest.mark.parametrize("code", ["", "   ", None, 42])
    async def test_an_answer_carrying_no_program(
        self, refresh: CodeRefresh, main: _Main, code: Any
    ) -> None:
        await refresh.note_change("rpi", "collector")

        await refresh.receive_code_return(
            "nodes/rpi/code_return", _answer(refresh, main, code=code)
        )

        assert main.registry["collector"]["code"] == BROKEN

    async def test_something_that_is_not_a_message(self, refresh: CodeRefresh, main: _Main) -> None:
        await refresh.receive_code_return("nodes/rpi/code_return", b"{not json")
        await refresh.receive_code_return("nodes/rpi/code_return", b"[]")
        await refresh.receive_code_return("nodes/rpi/code_return", None)

        assert main.registry["collector"]["code"] == BROKEN


class TestOneQuestionAtATime:
    """A notice is unauthenticated, and every question main asks costs it.

    Each is a signed message, and signing records a sequence number to disk
    before sending — on the stated assumption that control messages come at an
    operator's pace. A notice is the one thing that would let a node, or anyone
    on the broker, set that pace instead.
    """

    async def test_a_second_notice_while_a_question_is_open_asks_once(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        await refresh.note_change("rpi", "collector")
        await refresh.note_change("rpi", "collector")
        await refresh.note_change("rpi", "collector")

        assert len(main.published) == 1
        assert len(refresh.pending) == 1

    async def test_a_flood_of_notices_does_not_grow_the_pending_map(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        for _ in range(500):
            await refresh._on_notice("nodes/rpi/code_changed", b'{"agent": "collector"}')

        assert len(refresh.pending) == 1
        assert len(main.published) == 1

    async def test_asking_again_waits_out_the_interval(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        await refresh.note_change("rpi", "collector")
        await refresh.receive_code_return("nodes/rpi/code_return", _answer(refresh, main))
        assert refresh.pending == {}, "the question was answered, so none is open"

        await refresh.note_change("rpi", "collector")

        # Answered or not, the rate is what is capped.
        assert len(main.published) == 1

    async def test_a_real_repair_later_is_still_asked_about(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        # The floor sits below any real repair, which costs an LLM round trip
        # first — so a genuine second repair is never the one turned away.
        await refresh.note_change("rpi", "collector")
        await refresh.receive_code_return("nodes/rpi/code_return", _answer(refresh, main))
        refresh._last_asked["rpi", "collector"] = time.time() - ASK_INTERVAL_S - 1

        await refresh.note_change("rpi", "collector")

        assert len(main.published) == 2

    async def test_different_agents_do_not_wait_for_each_other(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        main.registry["other"] = {"name": "other", "node": "rpi", "code": BROKEN}

        await refresh.note_change("rpi", "collector")
        await refresh.note_change("rpi", "other")

        assert len(main.published) == 2


class TestAMisaddressedAnswerCannotCancelTheQuestion:
    """The token is in the request, in the clear.

    Anyone who can read the node's topics can quote it back. If a misaddressed
    answer spent the question, they could cancel the exchange — main would have
    nothing pending and would refuse the node's real answer a moment later.
    """

    async def test_the_question_survives_one(self, refresh: CodeRefresh, main: _Main) -> None:
        main.registry["other"] = {"name": "other", "node": "rpi", "code": BROKEN}
        await refresh.note_change("rpi", "collector")
        (token,) = refresh.pending

        await refresh.receive_code_return(
            "nodes/rpi/code_return", _answer(refresh, main, agent="other", code="evil()")
        )

        assert token in refresh.pending, "the question was cancelled by a stranger"

    async def test_and_the_real_answer_still_lands(self, refresh: CodeRefresh, main: _Main) -> None:
        await refresh.note_change("rpi", "collector")
        stolen = _answer(refresh, main, agent="someone-else")

        await refresh.receive_code_return("nodes/rpi/code_return", stolen)
        await refresh.receive_code_return("nodes/rpi/code_return", _answer(refresh, main))

        assert main.registry["collector"]["code"] == REPAIRED

    async def test_an_answer_that_is_addressed_right_is_spent(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        # Whether or not it carried a usable program: the node said what it is
        # running, and there is nothing to ask again.
        await refresh.note_change("rpi", "collector")

        await refresh.receive_code_return("nodes/rpi/code_return", _answer(refresh, main, code=""))

        assert refresh.pending == {}


class TestTheNotice:
    async def test_a_forged_notice_costs_one_question(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        """Anything on the broker can send one; it carries no code.

        All it does is make main ask the node, and the node answers with what
        it is really running — so the worst a forgery achieves is a round trip
        that files the truth.
        """
        await refresh._on_notice("nodes/rpi/code_changed", b'{"agent": "collector"}')

        (topic, _payload) = main.published[-1]
        assert topic == "nodes/rpi/code_request"
        assert main.registry["collector"]["code"] == BROKEN, "nothing was filed from the notice"

    async def test_a_notice_that_is_not_a_message_is_dropped(
        self, refresh: CodeRefresh, main: _Main
    ) -> None:
        await refresh._on_notice("nodes/rpi/code_changed", b"{not json")
        await refresh._on_notice("nodes/rpi/code_changed", None)

        assert main.published == []


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _Client:
    """A broker handing over a fixed sequence, then holding the line open."""

    def __init__(self, messages: list[_Message], drained: Any) -> None:
        self._messages = messages
        self._drained = drained
        self.subscribed: list[str] = []

    async def subscribe(self, topic: str, **_kw: Any) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        return self._stream()

    async def _stream(self) -> Any:
        for message in self._messages:
            yield message
        self._drained.set()
        # Held open, as a real broker would: a stream that ends is a reconnect,
        # and the test would race the retry instead of seeing the messages.
        await asyncio.Event().wait()


class _Broker:
    def __init__(self, messages: list[_Message]) -> None:
        self.drained = asyncio.Event()
        self.client = _Client(messages, self.drained)

    def __call__(self, host: str, port: int, **_kw: Any) -> "_Broker":
        return self

    async def __aenter__(self) -> _Client:
        return self.client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class TestTheListener:
    """The seam that makes the exchange run at all: what arrives, where it goes."""

    async def test_it_follows_both_halves_and_routes_each(
        self, refresh: CodeRefresh, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A notice, then the answer to the question it provokes.
        notice = _Message("nodes/rpi/code_changed", b'{"agent": "collector"}')
        broker = _Broker([notice])
        monkeypatch.setattr(code_refresh_mod, "mqtt_client", broker)

        task = asyncio.create_task(refresh.listener())
        try:
            await asyncio.wait_for(broker.drained.wait(), timeout=2)
            await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert set(broker.client.subscribed) == {
            "nodes/+/code_changed",
            "nodes/+/code_return",
        }
        # The notice was routed, so main asked.
        (topic, _payload) = main.published[-1]
        assert topic == "nodes/rpi/code_request"

    async def test_an_answer_arriving_on_the_wire_is_filed(
        self, refresh: CodeRefresh, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await refresh.note_change("rpi", "collector")
        answer = _Message("nodes/rpi/code_return", _answer(refresh, main))
        broker = _Broker([answer])
        monkeypatch.setattr(code_refresh_mod, "mqtt_client", broker)

        task = asyncio.create_task(refresh.listener())
        try:
            await asyncio.wait_for(broker.drained.wait(), timeout=2)
            await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert main.registry["collector"]["code"] == REPAIRED

    async def test_it_stops_when_the_actor_does(
        self, refresh: CodeRefresh, main: _Main, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The state is read each pass rather than captured, so the listener
        # goes down with the actor instead of holding a connection open.
        main.state = SimpleNamespace(value="stopped")
        monkeypatch.setattr(code_refresh_mod, "mqtt_client", _Broker([]))

        await asyncio.wait_for(refresh.listener(), timeout=2)
