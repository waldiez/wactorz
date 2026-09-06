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
from typing import Any

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.llm_bridge import LLMBridge
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.migration import Migration
from wactorz.agents.main.nodes import NodeManager
from wactorz.core.actor import ActorState


class _Message:
    def __init__(self, payload: bytes) -> None:
        self.topic = "main/llm_request"
        self.payload = payload


class _Client:
    def __init__(self, messages: list[_Message], on_drained: Any) -> None:
        self._messages = messages
        self._on_drained = on_drained
        self.subscribed: list[str] = []

    async def subscribe(self, topic: str) -> None:
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


def request(**over: Any) -> _Message:
    """A bridge request as a remote agent publishes it."""
    body: dict[str, Any] = {
        "_reply_topic": "nodes/rpi/llm_reply/1",
        "agent": "collector",
        "node": "rpi-kitchen",
        "prompt": "how warm is it?",
        **over,
    }
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
        run = await run_bridge(monkeypatch, [request()], llm=_LLM())

        assert run.replies[0][0] == "nodes/rpi/llm_reply/1"

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
