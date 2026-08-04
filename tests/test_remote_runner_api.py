"""The agent API a remote node hands to generated code.

`remote_runner.py` runs on edge nodes and ``exec``s code arriving over MQTT, so
what this surface accepts and rejects is worth pinning independently of the
number. It imports on the standard library alone and needs no broker: SSH is
only how ``installer_agent`` deploys it.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

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
