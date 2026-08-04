"""The agent API a remote node hands to generated code.

`remote_runner.py` runs on edge nodes and ``exec``s code arriving over MQTT, so
what this surface accepts and rejects is worth pinning independently of the
number. It imports on the standard library alone and needs no broker: SSH is
only how ``installer_agent`` deploys it.
"""

import asyncio
import time
from collections.abc import Iterator
from typing import Any, cast

import pytest

from wactorz import remote_runner


class _Runner:
    """Stands in for the node runner the API reads broker settings from."""

    broker = "localhost"
    port = 1883


class _Agent:
    """The minimum of ``_RemoteAgent`` that ``_RemoteAgentAPI`` touches."""

    def __init__(self, name: str = "edge-agent", node: str = "node-a") -> None:
        self.name = name
        self.node = node
        self._runner = _Runner()
        self.actor_id = "edge-0001"
        # Read by the manifest publish that subscribe() kicks off in the
        # background; absent, that task dies with an unretrieved exception.
        self._config: dict = {}


@pytest.fixture(name="api")
def api_fixture() -> Iterator[Any]:
    """A real API object, with any subscriber tasks it starts cleaned up."""
    api = remote_runner._RemoteAgentAPI(_Agent())  # pyright: ignore[reportArgumentType]
    yield api
    for task in api._subscriber_tasks:
        task.cancel()


async def _drain(api: Any) -> None:
    """Let cancelled subscriber tasks unwind before the loop closes."""
    tasks = list(api._subscriber_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    api._subscriber_tasks.clear()


async def _noop(_payload: dict[str, Any]) -> None:
    return None


class TestSubscribeRejectsBadCallbacks:
    """The caller is LLM-generated code, so the errors have to teach."""

    async def test_a_non_callable_is_refused(self, api: Any) -> None:
        with pytest.raises(TypeError) as caught:
            api.subscribe("sensors/x", "not a function")

        # The message is read by whoever is fixing the generated agent.
        assert "requires a callable callback" in str(caught.value)
        assert "sensors/x" in str(caught.value)

    async def test_none_is_refused(self, api: Any) -> None:
        with pytest.raises(TypeError):
            api.subscribe("sensors/x", None)

    async def test_a_callback_taking_no_payload_is_refused(self, api: Any) -> None:
        async def _takes_nothing() -> None:
            return None

        with pytest.raises(TypeError) as caught:
            api.subscribe("sensors/x", _takes_nothing)

        assert "must accept one argument" in str(caught.value)

    async def test_a_callback_with_defaults_only_is_refused(self, api: Any) -> None:
        async def _all_defaulted(payload: dict | None = None) -> None:
            return None

        # Every parameter is optional, so nothing receives the payload.
        with pytest.raises(TypeError):
            api.subscribe("sensors/x", _all_defaulted)


class TestSubscribeDedup:
    async def test_the_same_callback_twice_subscribes_once(self, api: Any) -> None:
        api.subscribe("sensors/x", _noop)
        api.subscribe("sensors/x", _noop)
        try:
            # setup() runs again on reconnect; two listeners would double every
            # message the agent sees.
            assert len(api._subscriber_tasks) == 1
        finally:
            await _drain(api)

    async def test_the_same_callback_on_another_topic_subscribes_again(self, api: Any) -> None:
        api.subscribe("sensors/x", _noop)
        api.subscribe("sensors/y", _noop)
        try:
            assert len(api._subscriber_tasks) == 2
        finally:
            await _drain(api)

    async def test_two_distinct_callbacks_both_subscribe(self, api: Any) -> None:
        async def _first(_payload: dict) -> None:
            return None

        async def _second(_payload: dict) -> None:
            return None

        api.subscribe("sensors/x", _first)
        api.subscribe("sensors/x", _second)
        try:
            assert len(api._subscriber_tasks) == 2
        finally:
            await _drain(api)

    async def test_the_dedup_key_keeps_its_callback_alive(self, api: Any) -> None:
        """Keying on ``id(callback)`` while holding no reference is unsound.

        ``id()`` is unique only among *live* objects. Once a callback is
        collected its address is free for the next one, which then matches the
        stale key and is silently skipped — announced as a duplicate in a debug
        line. Transient callbacks are the normal case here: generated agent code
        defines them inside ``setup()``, which runs again on every reconnect.

        Holding a reference is what makes the key mean what it says, so this
        asserts the callbacks are reachable from the dedup record rather than
        trying to provoke an address collision, which is not reproducible.
        """
        recorded = []

        async def _cb(_payload: dict) -> None:
            return None

        api.subscribe("sensors/x", _cb)
        try:
            for entry in api._subscribed_topics:
                recorded.extend(x for x in (entry if isinstance(entry, tuple) else (entry,)))
            values = list(getattr(api._subscribed_topics, "values", list)())
            assert _cb in recorded or _cb in values, (
                "the dedup record holds only an address, so the callback it names "
                "can be collected and its address reused by a different one"
            )
        finally:
            await _drain(api)


class TestMissingDeps:
    def test_it_reports_nothing_when_everything_is_installed(self) -> None:
        # aiomqtt, paho and psutil are hard dependencies of the test env.
        assert remote_runner._missing_deps() == []

    def test_it_names_the_package_not_the_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _absent(module: str) -> None:
            if module == "paho.mqtt.client":
                raise ImportError(module)

        monkeypatch.setattr(remote_runner.importlib, "import_module", _absent)

        # A node operator installs "paho-mqtt", not "paho.mqtt.client".
        assert remote_runner._missing_deps() == ["paho-mqtt"]


class TestAwaitableNone:
    """Generated code writes ``await agent.subscribe(...)`` as often as not."""

    async def test_it_can_be_awaited(self) -> None:
        assert await remote_runner._AWAITABLE_NONE is None  # pyright: ignore[reportGeneralTypeIssues]

    def test_it_is_falsey(self) -> None:
        assert not remote_runner._AWAITABLE_NONE

    def test_it_looks_like_none(self) -> None:
        assert repr(remote_runner._AWAITABLE_NONE) == "None"


class TestStreamWindow:
    """A sliding time window an agent queries synchronously.

    The background filler needs a broker; every query below reads the in-memory
    buffer, so they are driven by placing entries directly. Entries carry a
    ``_ts``, which is what the window trims on.
    """

    @staticmethod
    def _window(entries: list[dict], seconds: float = 300) -> Any:
        window = remote_runner._RemoteStreamWindow("sensors/x", "localhost", 1883, seconds=seconds)
        window._buffer.extend(entries)
        return window

    @staticmethod
    def _at(offset: float, **fields: Any) -> dict:
        """An entry stamped ``offset`` seconds ago."""
        return {"_ts": time.time() - offset, **fields}

    def test_values_and_count_ignore_entries_without_the_key(self) -> None:
        window = self._window([self._at(1, value=1), self._at(1, other=2), self._at(1, value=3)])

        assert window.values() == [1, 3]
        # count is the buffer, not the matches — a distinction worth pinning.
        assert window.count() == 3

    def test_latest_is_the_newest_entry_carrying_the_key(self) -> None:
        window = self._window([self._at(3, value=1), self._at(2, value=2), self._at(1, other=9)])

        assert window.latest() == 2

    def test_latest_is_none_when_nothing_carries_the_key(self) -> None:
        assert self._window([self._at(1, other=1)]).latest() is None

    def test_the_aggregates_are_none_on_an_empty_window(self) -> None:
        window = self._window([])

        # None rather than 0: an agent must be able to tell "no readings" from
        # "readings that average zero".
        assert window.mean() is None
        assert window.min() is None
        assert window.max() is None

    def test_the_aggregates_read_the_buffer(self) -> None:
        window = self._window([self._at(1, value=2), self._at(1, value=4), self._at(1, value=9)])

        assert window.mean() == 5
        assert window.min() == 2
        assert window.max() == 9

    def test_rising_and_falling_compare_the_ends_not_the_extremes(self) -> None:
        dipping = self._window([self._at(3, value=1), self._at(2, value=99), self._at(1, value=5)])

        # First to last, so a spike in the middle does not decide it.
        assert dipping.rising() is True
        assert dipping.falling() is False

    def test_a_single_reading_is_neither_rising_nor_falling(self) -> None:
        window = self._window([self._at(1, value=1)])

        assert window.rising() is False
        assert window.falling() is False

    def test_the_threshold_is_a_minimum_change(self) -> None:
        window = self._window([self._at(2, value=10), self._at(1, value=12)])

        assert window.rising(threshold=1) is True
        assert window.rising(threshold=5) is False

    def test_stable_is_the_spread_against_a_tolerance(self) -> None:
        window = self._window([self._at(2, value=10), self._at(1, value=11)])

        assert window.stable(tolerance=2) is True
        assert window.stable(tolerance=0.5) is False

    def test_an_empty_window_is_not_stable(self) -> None:
        # Nothing was measured, so "unchanged" would be a claim about no data.
        assert self._window([]).stable(tolerance=100) is False

    def test_absent_for_is_true_when_nothing_has_arrived(self) -> None:
        assert self._window([]).absent_for(1) is True

    def test_absent_for_measures_from_the_newest_entry(self) -> None:
        window = self._window([self._at(10, value=1)])

        assert window.absent_for(5) is True
        assert window.absent_for(60) is False

    def test_entries_older_than_the_window_are_dropped(self) -> None:
        window = self._window([self._at(600, value=1), self._at(1, value=2)], seconds=300)

        assert window.values() == [2]
        assert window.count() == 1

    def test_the_buffer_is_bounded(self) -> None:
        window = remote_runner._RemoteStreamWindow("sensors/x", "localhost", 1883, max_size=3)
        for n in range(10):
            window._buffer.append(self._at(1, value=n))

        # A node runs for weeks; an unbounded window would be a slow leak.
        assert window.count() == 3
        assert window.values() == [7, 8, 9]

    def test_event_count_counts_everything_by_default(self) -> None:
        window = self._window([self._at(1, value=1), self._at(1, other=2)])

        assert window.event_count() == 2

    def test_event_count_filters_by_key_then_value(self) -> None:
        window = self._window(
            [self._at(1, state="on"), self._at(1, state="off"), self._at(1, other=1)]
        )

        assert window.event_count(key="state") == 2
        assert window.event_count(key="state", value="on") == 1

    def test_event_count_can_look_at_less_than_the_whole_window(self) -> None:
        window = self._window([self._at(200, value=1), self._at(1, value=2)], seconds=300)

        assert window.event_count() == 2
        assert window.event_count(seconds=60) == 1

    def test_stop_is_safe_before_start(self) -> None:
        # Agent code calls stop() in teardown whether or not it ever started.
        remote_runner._RemoteStreamWindow("sensors/x", "localhost", 1883).stop()


class _FakeAPI:
    """Records what the LLM bridge forwards, and what it forwards it to."""

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.state: dict = {}
        self.ask_llm_calls: list[tuple] = []
        self.chat_calls: list[tuple] = []

    async def ask_llm(self, prompt: str, system: str = "", timeout: float = 60.0) -> str:
        self.ask_llm_calls.append((prompt, system, timeout))
        return self.reply

    async def chat(self, messages: Any, system: str = "", timeout: float = 60.0) -> str:
        # Snapshotted: converse() hands over its live history list and appends
        # the reply to it afterwards, so a stored reference shows later turns.
        recorded = list(messages) if isinstance(messages, list) else messages
        self.chat_calls.append((recorded, system, timeout))
        return self.reply


def _bridge(api: _FakeAPI) -> Any:
    """The bridge over a stand-in API, cast where the real type is demanded."""
    return remote_runner._RemoteLLMInterface(cast(remote_runner._RemoteAgentAPI, api))


class TestLLMBridge:
    """agent.llm on a node forwards to main; the shapes must match the local API."""

    async def test_chat_forwards_the_prompt(self) -> None:
        api = _FakeAPI()
        bridge = _bridge(api)

        assert await bridge.chat("hello", system="be brief", timeout=5) == "ok"
        assert api.ask_llm_calls == [("hello", "be brief", 5)]

    async def test_complete_forwards_the_message_list(self) -> None:
        api = _FakeAPI()
        bridge = _bridge(api)
        messages = [{"role": "user", "content": "hi"}]

        await bridge.complete(messages)

        # complete() routes through the API's chat(), which takes a list here
        # rather than a prompt — a name collision the source calls out.
        assert api.chat_calls[0][0] == messages
        assert api.ask_llm_calls == []

    async def test_converse_keeps_both_sides_of_the_exchange(self) -> None:
        api = _FakeAPI(reply="the answer")
        bridge = _bridge(api)

        assert await bridge.converse("first") == "the answer"

        assert api.state["_chat_history"] == [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "the answer"},
        ]

    async def test_converse_sends_the_history_it_has_accumulated(self) -> None:
        api = _FakeAPI(reply="r")
        bridge = _bridge(api)

        await bridge.converse("first")
        await bridge.converse("second")

        # The second call carries the first exchange, which is what makes it
        # stateful rather than three independent questions.
        sent = api.chat_calls[1][0]
        assert [m["content"] for m in sent] == ["first", "r", "second"]

    async def test_converse_hands_over_its_live_history(self) -> None:
        """The list passed to the LLM is the same object converse keeps."""
        seen: list = []
        api = _FakeAPI()

        async def _capture(messages: Any, system: str = "", timeout: float = 60.0) -> str:
            seen.append(messages)
            return "r"

        api.chat = _capture  # type: ignore[method-assign]
        bridge = _bridge(api)

        await bridge.converse("first")

        # Anything holding it past the call sees turns that had not happened
        # when it was handed over. Harmless while the payload is serialised
        # immediately; a hazard for any caller that keeps it.
        assert seen[0] is api.state["_chat_history"]

    async def test_converse_shares_the_agent_state_dict(self) -> None:
        api = _FakeAPI()
        api.state["_chat_history"] = [{"role": "user", "content": "earlier"}]
        bridge = _bridge(api)

        await bridge.converse("now")

        # History lives in agent.state, so it survives alongside the rest of the
        # agent's data rather than in a private buffer.
        assert api.state["_chat_history"][0]["content"] == "earlier"
        assert len(api.state["_chat_history"]) == 3
