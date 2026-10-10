"""Main answering LLM calls on behalf of agents running on other machines.

An edge node holds no API key. When an agent there needs a model it publishes a
request to `main/llm_request` with a topic to answer on, and main runs the call
with its own provider and publishes the text back. That is the whole point of
the bridge: the keys stay on one machine.

A request that goes unanswered strands the caller until its own timeout, so
every path here has to end in a reply — including the ones where there is no
provider or the call raises.
"""

import asyncio
import json
import logging
import uuid
from typing import Any

import pytest

from wactorz.agents.main import llm_bridge
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.llm_bridge import REFUSED_UNSIGNED, LLMBridge, reply_topic_for
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.nodes import NodeManager
from wactorz.core.actor import ActorState
from wactorz.core.node_signing import (
    REQUEST_SIGNATURE_FIELD,
    node_key,
    request_signed_for,
    sign_request,
)
from wactorz.node.signing import ControlGuard


class _Message:
    def __init__(self, payload: bytes) -> None:
        self.topic = "main/llm_request"
        self.payload = payload


class _Client:
    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self._messages = messages
        self._on_drained = on_drained
        self.subscribed: list[str] = []

    async def subscribe(self, topic: str, qos: int = 0, **_kwargs: Any) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for message in self._messages:
            yield message
        self._on_drained()


class _Broker:
    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self.client = _Client(messages, on_drained)

    def __call__(self, _host: str, _port: int, **_kwargs: Any) -> "_Broker":
        return self

    async def __aenter__(self) -> _Client:
        return self.client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _LLM:
    """A provider recording what it was asked, or raising if told to."""

    def __init__(
        self,
        text: str = "the answer",
        usage: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.text = text
        self.usage = usage if usage is not None else {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, messages: list[dict[str, Any]], system: str = ""
    ) -> tuple[str, dict[str, Any]]:
        self.calls.append({"messages": messages, "system": system})
        if self.error is not None:
            raise self.error
        return self.text, self.usage


class _Run:
    """One drive of the bridge: what was published, and what the model saw."""

    def __init__(self, main: MainActor, llm: _LLM | None) -> None:
        self.main = main
        self.llm = llm
        self.published: list[tuple[str, Any]] = []
        self.persisted = 0
        self.notices: list[dict[str, Any]] = []

    @property
    def replies(self) -> list[tuple[str, Any]]:
        return self.published

    @property
    def only_reply(self) -> Any:
        assert len(self.published) == 1, f"expected one reply, got {self.published}"
        return self.published[0][1]


#: Marks a `_persist_cost` call in the published list, so one recorder serves
#: both and ordering between them stays visible.
PERSISTED = "<persisted>"


def request(*, signed: bool = True, **over: Any) -> _Message:
    """A bridge request as a remote agent publishes it.

    The reply topic is unique per call, as the runner makes it -- it mints a
    fresh uuid for every request -- and lies in the node's own reply space. Two
    requests sharing one would be a redelivery of the same request, which the
    bridge deliberately ignores. Signed with the key of the node it names, as a
    deployed node signs, unless ``signed`` is false.
    """
    body: dict[str, Any] = {
        "_reply_topic": f"nodes/rpi-kitchen/reply/{uuid.uuid4().hex[:8]}",
        "agent": "collector",
        "node": "rpi-kitchen",
        "prompt": "how warm is it?",
        **over,
    }
    if signed:
        body = sign_request(body, bytes.fromhex(node_key(str(body["node"]))))
    return _Message(json.dumps(body).encode())


async def run_bridge(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[_Message],
    *,
    llm: _LLM | None = None,
) -> _Run:
    """Drive the real bridge over `messages` until they are exhausted."""
    main = MainActor.__new__(MainActor)
    main.name = "main"
    main.manifests = ManifestRegistry(main)
    main.nodes = NodeManager(main, main.manifests)
    main.migration = Migration(main, main.nodes)
    main.llm_bridge = LLMBridge(main)
    main.state = ActorState.RUNNING
    main._mqtt_broker = "localhost"
    main._mqtt_port = 1883
    setattr(main, "llm", llm)
    main.total_input_tokens = 0
    main.total_output_tokens = 0
    main.total_cost_usd = 0.0

    run = _Run(main, llm)

    async def _publish(topic: str, payload: Any, **_kw: Any) -> None:
        run.published.append((topic, payload))

    setattr(main, "_mqtt_publish", _publish)
    setattr(main, "_persist_cost", lambda: run.published.append((PERSISTED, None)))
    setattr(main, "_queue_notification", run.notices.append)

    def _stop() -> None:
        main.state = ActorState.STOPPED

    broker = _Broker(messages, _stop)
    monkeypatch.setattr("wactorz.agents.main.llm_bridge.mqtt_client", broker)

    await asyncio.wait_for(main._llm_bridge_listener(), timeout=5)
    run.persisted = sum(1 for topic, _ in run.published if topic == PERSISTED)
    run.published = [p for p in run.published if p[0] != PERSISTED]
    return run


class TestAnsweringARequest:
    async def test_the_reply_goes_to_the_topic_the_caller_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        named = request(_reply_topic="nodes/rpi-kitchen/reply/c0ffee42")

        run = await run_bridge(monkeypatch, [named], llm=_LLM())

        assert run.replies[0][0] == "nodes/rpi-kitchen/reply/c0ffee42"

    async def test_the_reply_carries_the_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_bridge(monkeypatch, [request()], llm=_LLM(text="22 degrees"))

        assert run.only_reply == {"text": "22 degrees"}

    async def test_a_prompt_becomes_a_single_user_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = _LLM()

        await run_bridge(monkeypatch, [request(prompt="how warm?")], llm=llm)

        assert llm.calls[0]["messages"] == [{"role": "user", "content": "how warm?"}]

    async def test_a_message_list_is_passed_through_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The multi-turn path: an agent holding its own conversation sends the
        # whole thing, and rebuilding it from `prompt` would drop the history.
        llm = _LLM()
        turns = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ]

        await run_bridge(monkeypatch, [request(messages=turns)], llm=llm)

        assert llm.calls[0]["messages"] == turns

    async def test_the_default_system_prompt_names_the_agent_and_node(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = _LLM()

        await run_bridge(monkeypatch, [request()], llm=llm)

        assert "collector" in llm.calls[0]["system"]
        assert "rpi-kitchen" in llm.calls[0]["system"]

    async def test_a_supplied_system_prompt_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llm = _LLM()

        await run_bridge(monkeypatch, [request(system="be terse")], llm=llm)

        assert llm.calls[0]["system"] == "be terse"

    async def test_the_cost_is_persisted_after_a_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Totals live in memory until this runs, so a restart would lose what
        # the bridge spent.
        run = await run_bridge(monkeypatch, [request()], llm=_LLM())

        assert run.persisted == 1


class TestWhenTheCallCannotBeMade:
    """Every path still answers — a silent bridge strands the caller."""

    async def test_no_provider_still_replies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_bridge(monkeypatch, [request()], llm=None)

        assert "no provider configured" in run.only_reply["text"]

    async def test_a_failing_call_still_replies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_bridge(monkeypatch, [request()], llm=_LLM(error=RuntimeError("rate limit")))

        assert "rate limit" in run.only_reply["text"]

    async def test_a_failing_call_does_not_end_the_bridge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = _LLM(error=RuntimeError("rate limit"))

        run = await run_bridge(monkeypatch, [request(), request()], llm=llm)

        assert len(run.replies) == 2


class TestRedeliveredRequests:
    """QoS 1 is at-least-once, and answering twice spends the budget twice.

    The broker resends anything it did not see acknowledged, so a drop between
    receipt and acknowledgement replays a request whose answer was already
    published.
    """

    async def test_the_same_request_twice_is_answered_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        usage = {"input_tokens": 5, "output_tokens": 7, "cost_usd": 0.001}
        repeat = request()

        run = await run_bridge(monkeypatch, [repeat, repeat], llm=_LLM(usage=usage))

        assert len(run.replies) == 1
        assert run.main.total_cost_usd == pytest.approx(0.001), "budget spent twice"

    async def test_distinct_requests_are_both_answered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guard keys on the reply topic, so it must not swallow real traffic.
        run = await run_bridge(monkeypatch, [request(), request()], llm=_LLM())

        assert len(run.replies) == 2


class TestRequestsThatAreNotAnswered:
    async def test_one_without_a_reply_topic_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # There is nowhere to answer, so the only thing to do is nothing.
        run = await run_bridge(monkeypatch, [request(_reply_topic=None)], llm=_LLM())

        assert not run.replies

    async def test_a_malformed_payload_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_bridge(monkeypatch, [_Message(b"{not json")], llm=_LLM())

        assert not run.replies

    @pytest.mark.parametrize(
        "payload",
        [b"[]", b"[1, 2]", b"42", b'"a string"'],
        ids=["empty-list", "list", "number", "string"],
    )
    async def test_valid_json_that_is_not_a_request_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch, payload: bytes
    ) -> None:
        # It parses, so the JSON guard passes it through, and everything after
        # reads it as a mapping. Treated as unusable rather than allowed to
        # reach the fields, where it costs the connection rather than itself.
        run = await run_bridge(monkeypatch, [_Message(payload)], llm=_LLM())

        assert not run.replies

    async def test_it_does_not_cost_the_connection(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # One publisher sending the wrong shape must not make the bridge
        # reconnect, which would drop every request in flight with it.
        with caplog.at_level(logging.WARNING, logger="wactorz.agents.main.llm_bridge"):
            run = await run_bridge(monkeypatch, [_Message(b"[1, 2]"), request()], llm=_LLM())

        assert len(run.replies) == 1
        assert "Reconnecting" not in caplog.text

    async def test_a_later_good_request_is_still_answered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_bridge(monkeypatch, [_Message(b"{not json"), request()], llm=_LLM())

        assert len(run.replies) == 1


class TestCostIsAccounted:
    """Bridge calls spend main's budget, so they have to reach main's totals."""

    async def test_usage_is_added_to_the_totals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        usage = {"input_tokens": 12, "output_tokens": 30, "cost_usd": 0.004}

        run = await run_bridge(monkeypatch, [request()], llm=_LLM(usage=usage))

        assert run.main.total_input_tokens == 12
        assert run.main.total_output_tokens == 30
        assert run.main.total_cost_usd == pytest.approx(0.004)

    async def test_two_calls_accumulate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        usage = {"input_tokens": 5, "output_tokens": 7, "cost_usd": 0.001}

        run = await run_bridge(monkeypatch, [request(), request()], llm=_LLM(usage=usage))

        assert run.main.total_input_tokens == 10
        assert run.main.total_cost_usd == pytest.approx(0.002)

    async def test_usage_the_provider_omits_costs_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await run_bridge(monkeypatch, [request()], llm=_LLM(usage={}))

        assert run.main.total_input_tokens == 0
        assert run.main.total_cost_usd == 0.0


class TestWhatIsSaidAboutIt:
    async def test_a_request_is_logged_with_its_origin(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="wactorz.agents.main.llm_bridge"):
            await run_bridge(monkeypatch, [request()], llm=_LLM())

        assert "collector" in caplog.text
        assert "rpi-kitchen" in caplog.text


class TestOnlyANodeItDeployedIsAnswered:
    """Main answers with an account the broker lets write anywhere, on its own budget."""

    @pytest.mark.parametrize(
        "topic",
        [
            "agents/1234/commands",  # what a node's access list denies it
            "system/shutdown",
            "nodes/rpi-garage/reply/abcd1234",  # another node's reply space
            "nodes/rpi-kitchen/spawn",  # its own control topic, not a reply
            "nodes/rpi-kitchen/reply/not-hex",
        ],
    )
    async def test_a_reply_topic_outside_the_node_s_reply_space_gets_nothing(
        self, monkeypatch: pytest.MonkeyPatch, topic: str
    ) -> None:
        # Otherwise a node denied a topic could have main publish there for it.
        llm = _LLM()
        run = await run_bridge(monkeypatch, [request(_reply_topic=topic)], llm=llm)

        assert run.replies == []
        assert llm.calls == []

    async def test_a_signed_request_is_answered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await run_bridge(monkeypatch, [request()], llm=_LLM("the answer"))

        assert run.only_reply == {"text": "the answer"}

    async def test_an_unsigned_request_is_refused_and_told_why(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Told, so the caller fails at once rather than after its whole timeout.
        monkeypatch.setattr(llm_bridge, "NODE_SIGNING", "enforce")
        llm = _LLM()
        run = await run_bridge(monkeypatch, [request(signed=False)], llm=llm)

        assert run.only_reply == {"text": REFUSED_UNSIGNED}
        assert llm.calls == []

    async def test_a_refused_request_does_not_use_up_the_nodes_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Anything on the broker can publish an unsigned request that names a
        # reply topic. If that counted as the topic having been answered, the
        # node's real request for it would be taken for a repeat and dropped.
        monkeypatch.setattr(llm_bridge, "NODE_SIGNING", "enforce")
        topic = "nodes/rpi-kitchen/reply/abcd1234"
        llm = _LLM()

        run = await run_bridge(
            monkeypatch,
            [request(signed=False, _reply_topic=topic), request(_reply_topic=topic)],
            llm=llm,
        )

        assert [said["text"] for _topic, said in run.replies] == [REFUSED_UNSIGNED, "the answer"]
        assert len(llm.calls) == 1

    async def test_a_request_signed_by_another_node_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One node cannot spend main's budget in another's name.
        monkeypatch.setattr(llm_bridge, "NODE_SIGNING", "enforce")
        body = {
            "_reply_topic": "nodes/rpi-kitchen/reply/abcd1234",
            "agent": "collector",
            "node": "rpi-kitchen",
            "prompt": "hi",
        }
        forged = sign_request(body, bytes.fromhex(node_key("rpi-garage")))
        llm = _LLM()

        run = await run_bridge(monkeypatch, [_Message(json.dumps(forged).encode())], llm=llm)

        assert run.only_reply == {"text": REFUSED_UNSIGNED}
        assert llm.calls == []

    async def test_a_request_altered_after_signing_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llm_bridge, "NODE_SIGNING", "enforce")
        signed = json.loads(request().payload)
        signed["prompt"] = "something else entirely"
        llm = _LLM()

        run = await run_bridge(monkeypatch, [_Message(json.dumps(signed).encode())], llm=llm)

        assert run.only_reply == {"text": REFUSED_UNSIGNED}
        assert llm.calls == []

    async def test_warn_answers_it_and_says_so_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(llm_bridge, "NODE_SIGNING", "warn")
        run = await run_bridge(
            monkeypatch, [request(signed=False), request(signed=False)], llm=_LLM("ok")
        )

        assert [payload for _topic, payload in run.replies] == [{"text": "ok"}, {"text": "ok"}]
        assert len(run.notices) == 1
        assert "rpi-kitchen" in run.notices[0]["message"]

    async def test_a_replayed_signed_request_is_answered_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The signature covers the reply topic, so a replay carries the same one.
        captured = request()
        llm = _LLM()

        run = await run_bridge(monkeypatch, [captured, captured], llm=llm)

        assert len(run.replies) == 1
        assert len(llm.calls) == 1


class TestWhatTheBridgeRemembersIsBounded:
    async def test_invented_node_names_do_not_grow_it_without_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The node in a request is whatever its sender wrote.
        monkeypatch.setattr(llm_bridge, "REPORTED_MEMORY", 8)
        monkeypatch.setattr(llm_bridge, "NODE_SIGNING", "enforce")
        forged = [
            request(
                signed=False, node=f"made-up-{i}", _reply_topic=f"nodes/made-up-{i}/reply/ab{i:02x}"
            )
            for i in range(50)
        ]

        run = await run_bridge(monkeypatch, forged, llm=_LLM())

        assert len(run.main.llm_bridge._reported) == 8
        assert len(run.replies) == 50  # each still told it was refused


class TestTheNodeSignsWhatMainChecks:
    async def test_a_request_from_a_node_s_runner_is_accepted(self, tmp_path: Any) -> None:
        # Both halves share one canonical form; this is where they would drift.
        guard = ControlGuard(node_key("rpi-kitchen"), "", "enforce", str(tmp_path))
        body = {
            "_reply_topic": "nodes/rpi-kitchen/reply/abcd1234",
            "agent": "collector",
            "node": "rpi-kitchen",
            "messages": [{"role": "user", "content": "héllo"}],
            "system": "",
        }

        signed = guard.sign_request(body)
        # As main receives it: serialised by the node, parsed back on main.
        received = json.loads(json.dumps(signed))

        assert request_signed_for(received, "rpi-kitchen")

    def test_a_node_without_a_key_sends_the_request_as_it_is(self, tmp_path: Any) -> None:
        guard = ControlGuard("", "", "", str(tmp_path))
        body = {"node": "rpi-kitchen"}

        assert REQUEST_SIGNATURE_FIELD not in guard.sign_request(body)


def test_the_reply_topic_rule() -> None:
    assert reply_topic_for("nodes/rpi-kitchen/reply/abcd1234", "rpi-kitchen")
    assert not reply_topic_for("nodes/rpi-kitchen/reply/abcd1234", "rpi-garage")
    assert not reply_topic_for("nodes/rpi-kitchen/reply/abcd1234", "")
    assert not reply_topic_for("nodes/rpi-kitchen/reply/ab/cd", "rpi-kitchen")
