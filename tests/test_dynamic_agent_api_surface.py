"""The rest of what generated code is handed as `agent`.

Generated code is written by a model, often for a remote node first, so this
surface forgives the mistakes the model makes most: awaiting what is
synchronous, passing a string where a list belongs, naming a schema by one of
its common aliases. It is equally firm about the one mistake that cannot be
forgiven quietly — awaiting a window — and says what to write instead.

Discovery calls always return a list, even with no registry or bus, because
generated code iterates the result without checking it.
"""

import asyncio
import inspect
import time
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic import api as api_mod
from wactorz.agents.dynamic import streams as streams_mod
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.dynamic.api import AgentAPI, LLMInterface
from wactorz.agents.dynamic.streams import WINDOW_NOT_AWAITABLE, UnAwaitableWindow
from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.core import topic_bus
from wactorz.core.actor import ActorState
from wactorz.core.topic_bus import StreamWindow, TopicBus, TopicContract


class _Broker:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any, bool]] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, payload, retain))


class _Llm:
    async def complete(self, **_kwargs: Any) -> Any:
        raise RuntimeError("model overloaded")


class _Actor:
    def __init__(self, name: str, **attrs: Any) -> None:
        self.name = name
        self.state = ActorState.RUNNING
        for key, value in attrs.items():
            setattr(self, key, value)


class _Registry:
    def __init__(self, *actors: Any) -> None:
        self._actors = list(actors)

    def all_actors(self) -> list[Any]:
        return list(self._actors)


class _Main:
    def __init__(self, known_nodes: dict[str, Any], manifests: dict[str, Any]) -> None:
        self._known_nodes = known_nodes
        self._agent_manifests = manifests

    def list_nodes(self) -> list[dict[str, Any]]:
        return [{"node": "rpi"}]

    def list_topics(self, keyword: str = "") -> list[dict[str, Any]]:
        return [{"topic": f"match/{keyword}"}]

    def list_capabilities(self, keyword: str = "") -> list[dict[str, Any]]:
        return [{"name": f"cap-{keyword}"}]


def _api(tmp_path: Path, llm: Any = None) -> AgentAPI:
    actor = DynamicAgent(name="probe", code="", persistence_dir=str(tmp_path), llm_provider=llm)
    actor._mqtt_client = _Broker()
    return AgentAPI(actor)


def _published(api: AgentAPI) -> list[tuple[str, Any, bool]]:
    broker = api._actor._mqtt_client
    assert isinstance(broker, _Broker)
    return broker.published


@pytest.fixture(name="api")
def api_fixture(tmp_path: Path) -> AgentAPI:
    return _api(tmp_path)


@pytest.fixture(name="bus")
def bus_fixture(monkeypatch: pytest.MonkeyPatch) -> TopicBus:
    bus = TopicBus()
    monkeypatch.setattr(topic_bus, "_topic_bus", bus)
    return bus


@pytest.fixture(name="no_bus", autouse=True)
def no_bus_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topic_bus, "_topic_bus", None)


async def _stop_window(window: Any) -> None:
    inner = window._inner
    task = inner._task
    inner.stop()
    if task is not None:
        # `asyncio.wait`, not `wait_for`, which can lose a cancellation on 3.10/3.11.
        await asyncio.wait({task}, timeout=5)


class TestLLMInterface:
    async def test_no_provider_means_no_llm(self, api: AgentAPI) -> None:
        assert api.llm is None
        assert await api.chat("hi") == "[No LLM configured for this agent]"

    async def test_chat_answers_and_charges_the_agent(self, tmp_path: Path) -> None:
        api = _api(tmp_path, llm=FakeProvider(script={"weather": "sunny"}))
        assert api.llm is not None

        assert await api.llm.chat("what is the weather") == "sunny"
        assert api._actor.total_cost_usd > 0
        assert any(t.endswith("/metrics") for t, _, _ in _published(api))

    async def test_a_failing_provider_is_an_error_string(self, tmp_path: Path) -> None:
        api = _api(tmp_path, llm=_Llm())
        assert api.llm is not None

        assert await api.llm.chat("hi") == "[LLM error: model overloaded]"

    async def test_complete_without_a_provider_says_so(self, tmp_path: Path) -> None:
        actor = DynamicAgent(name="probe", code="", persistence_dir=str(tmp_path))
        interface = LLMInterface(actor, {})

        assert await interface.chat("hi") == "[No LLM configured for this agent]"
        assert await interface.complete([]) == "[No LLM configured]"

    async def test_converse_keeps_the_history_in_state(self, tmp_path: Path) -> None:
        api = _api(tmp_path, llm=FakeProvider(script={"hello": "hi", "again": "hi again"}))
        assert api.llm is not None

        await api.llm.converse("hello")
        await api.llm.converse("again")

        assert [m["content"] for m in api.state["_chat_history"]] == [
            "hello",
            "hi",
            "again",
            "hi again",
        ]

    async def test_chat_on_the_api_accepts_a_bare_string_or_a_list(self, tmp_path: Path) -> None:
        api = _api(tmp_path, llm=FakeProvider(script={"ping": "pong"}))

        assert await api.chat("ping") == "pong"
        assert await api.complete([{"role": "user", "content": "ping"}]) == "pong"


class TestIdentityAndLifecycle:
    def test_a_node_is_reported_when_the_agent_has_one(self, api: AgentAPI) -> None:
        api._actor._node = "rpi-kitchen"  # pyright: ignore[reportAttributeAccessIssue]

        assert api.node == "rpi-kitchen"

    async def test_stop_ends_the_agent_for_good(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ended: list[bool] = []

        async def _end() -> None:
            ended.append(True)

        monkeypatch.setattr(api._actor, "end_self", _end)

        await api.stop()

        assert ended == [True]

    async def test_the_logger_shim_logs_at_each_level(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        levels: list[tuple[str, str]] = []

        async def _log(message: str, level: str = "info") -> None:
            levels.append((level, message))

        monkeypatch.setattr(api, "log", _log)

        api.logger.info("a")
        api.logger.warning("b")
        api.logger.error("c")
        api.logger.debug("d")
        await asyncio.sleep(0)

        assert levels == [("info", "a"), ("warning", "b"), ("error", "c"), ("debug", "d")]

    async def test_background_work_is_tracked_on_the_actor(self, api: AgentAPI) -> None:
        async def _work() -> str:
            return "done"

        task = api.run_in_background(_work())

        assert task in api._actor._tasks
        assert await task == "done"

    async def test_background_work_still_runs_when_it_cannot_be_tracked(
        self, api: AgentAPI
    ) -> None:
        api._actor._tasks = None  # pyright: ignore[reportAttributeAccessIssue]

        async def _work() -> str:
            return "done"

        assert await api.run_in_background(_work()) == "done"

    def test_persist_can_be_awaited_by_mistake(self, api: AgentAPI) -> None:
        assert inspect.isawaitable(api.persist("k", 1))


class TestDiscovery:
    def test_local_and_live_remote_agents_are_listed(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api._actor._registry = _Registry(  # pyright: ignore[reportAttributeAccessIssue]
            _Actor("news", description="fetches news"),
            _Actor("chat", system_prompt="You are a helpful assistant."),
            _Actor("bare", state="custom"),
        )
        main = _Main(
            known_nodes={
                "rpi": {"last_seen": time.time(), "agents": ["camera", "news"]},
                "gone": {"last_seen": 0, "agents": ["old"]},
            },
            manifests={"camera": {"description": "takes photos"}},
        )
        monkeypatch.setattr(api_mod, "find_main_actor", lambda _registry: main)

        agents = {a["name"]: a for a in api.agents()}

        assert set(agents) == {"news", "chat", "bare", "camera"}
        assert agents["news"]["state"] == "RUNNING"
        assert agents["chat"]["description"] == "You are a helpful assistant."
        assert agents["bare"]["state"] == "custom"
        assert agents["camera"] == {
            "name": "camera",
            "type": "RemoteAgent",
            "description": "takes photos",
            "state": "running",
            "remote": True,
            "node": "rpi",
        }

    def test_nodes_topics_and_capabilities_come_from_main(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api_mod, "find_main_actor", lambda _registry: _Main({}, {}))

        assert api.nodes() == [{"node": "rpi"}]
        assert api.topics("temp") == [{"topic": "match/temp"}]
        assert api.capabilities("weather") == [{"name": "cap-weather"}]
        assert api.agents() == []

    def test_without_main_the_capabilities_are_empty(self, api: AgentAPI) -> None:
        assert api.capabilities() == []


class TestSubscribeBookkeeping:
    def test_the_same_callback_on_the_same_topic_is_bound_once(self, api: AgentAPI) -> None:
        async def on_msg(payload: dict[str, Any]) -> None:
            return None

        api._actor._subscribed_topics[("sensors/t", id(on_msg))] = on_msg

        result = api.subscribe("sensors/t", on_msg)

        assert inspect.isawaitable(result)
        assert getattr(api._actor, "_sub_hub", None) is None, "no second listener was bound"

    def test_a_callback_that_cannot_be_inspected_is_allowed_through(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bound: list[str] = []

        class _Hub:
            def bind(self, topic: str, callback: Any) -> None:
                bound.append(topic)

        def _uninspectable(_callback: Any) -> Any:
            raise ValueError("no signature")

        monkeypatch.setattr(streams_mod.inspect, "signature", _uninspectable)
        monkeypatch.setattr(streams_mod, "hub_for", lambda _actor: _Hub())

        api.subscribe("sensors/t", print)

        assert bound == ["sensors/t"]

    def test_a_first_subscription_creates_a_consumer_contract(
        self, api: AgentAPI, bus: TopicBus
    ) -> None:
        streams_mod._register_with_topic_bus(api, api._actor, "sensors/t")

        contract = bus.registry.get("probe")
        assert contract is not None
        assert contract.subscribes == ["sensors/t"]

    def test_later_subscriptions_extend_the_contract_once(
        self, api: AgentAPI, bus: TopicBus
    ) -> None:
        bus.register_contract(TopicContract(name="probe", subscribes=["a"]))

        streams_mod._register_with_topic_bus(api, api._actor, "b")
        streams_mod._register_with_topic_bus(api, api._actor, "b")

        contract = bus.registry.get("probe")
        assert contract is not None
        assert contract.subscribes == ["a", "b"]

    def test_a_broken_bus_is_not_the_subscribers_problem(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken() -> TopicBus:
            raise RuntimeError("bus down")

        monkeypatch.setattr(topic_bus, "get_topic_bus", _broken)

        streams_mod._register_with_topic_bus(api, api._actor, "sensors/t")


class _Message:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class _Client:
    def __init__(self, payloads: list[bytes], hang: bool = False) -> None:
        self._payloads = payloads
        self._hang = hang

    def __call__(self, _host: str, _port: int, **_kwargs: Any) -> "_Client":
        return self

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, **_kwargs: Any) -> None:
        return None

    @property
    def messages(self) -> Any:
        return self._stream()

    async def _stream(self) -> Any:
        if self._hang:
            await asyncio.Event().wait()
        for payload in self._payloads:
            yield _Message(payload)


class TestMqttGet:
    @pytest.mark.parametrize(
        ("payload", "expected"), [(b'{"cpu": 12}', {"cpu": 12}), (b"plain text", "plain text")]
    )
    async def test_one_message_is_read_and_parsed_where_possible(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch, payload: bytes, expected: Any
    ) -> None:
        monkeypatch.setattr(streams_mod, "mqtt_client", _Client([payload]))

        assert await api.mqtt_get("rpi/cpu") == expected

    async def test_a_broker_that_refuses_gives_none(self, api: AgentAPI) -> None:
        assert await api.mqtt_get("rpi/cpu") is None

    async def test_a_silent_topic_times_out_to_none(
        self, api: AgentAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(streams_mod, "mqtt_client", _Client([], hang=True))

        assert await api.mqtt_get("rpi/cpu", timeout=0.01) is None


class TestWindow:
    async def test_without_a_bus_a_local_window_is_started(self, api: AgentAPI) -> None:
        window = api.window("sensors/t", seconds=60, max_size=10)

        assert isinstance(window, UnAwaitableWindow)
        assert isinstance(window._inner, StreamWindow)
        assert window.seconds == 60
        assert repr(window) == "StreamWindow(topic=sensors/t, seconds=60)"
        await _stop_window(window)

    async def test_with_a_bus_the_bus_makes_the_window(
        self, api: AgentAPI, bus: TopicBus, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made: list[str] = []
        real_make = bus.make_window

        def _make(topic: str, seconds: float = 300, max_size: int = 1000) -> StreamWindow:
            made.append(topic)
            return real_make(topic, seconds=seconds, max_size=max_size)

        monkeypatch.setattr(bus, "make_window", _make)

        window = api.window("sensors/t")

        assert made == ["sensors/t"]
        await _stop_window(window)

    async def test_a_failing_bus_still_yields_a_local_window(
        self, api: AgentAPI, bus: TopicBus, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken(*_args: Any, **_kwargs: Any) -> StreamWindow:
            raise RuntimeError("bus full")

        monkeypatch.setattr(bus, "make_window", _broken)

        window = api.window("sensors/t")

        assert isinstance(window._inner, StreamWindow)
        await _stop_window(window)

    async def test_a_window_that_cannot_start_is_still_returned(
        self, api: AgentAPI, bus: TopicBus, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken(*_args: Any, **_kwargs: Any) -> StreamWindow:
            raise RuntimeError("bus full")

        def _no_start(self: StreamWindow, *_args: Any) -> StreamWindow:
            raise RuntimeError("no loop")

        monkeypatch.setattr(bus, "make_window", _broken)
        monkeypatch.setattr(StreamWindow, "start", _no_start)

        window = api.window("sensors/t")

        assert window.count() == 0

    async def test_awaiting_a_window_says_what_to_write_instead(self) -> None:
        window = UnAwaitableWindow(StreamWindow("t"))

        with pytest.raises(TypeError) as refused:
            await window

        assert str(refused.value) == WINDOW_NOT_AWAITABLE


class TestDeclareContract:
    async def test_aliases_and_bare_strings_are_accepted(
        self, api: AgentAPI, bus: TopicBus
    ) -> None:
        result = api.declare_contract(
            topics="out/t",
            subscribe="in/t",
            schema={"temp": "float"},
            input_schema={"cmd": "str"},
        )
        await asyncio.sleep(0)

        assert inspect.isawaitable(result)
        contract = bus.registry.get("probe")
        assert contract is not None
        assert contract.publishes == ["out/t"]
        assert contract.subscribes == ["in/t"]
        assert contract.produces_schema == {"temp": "float"}
        assert contract.consumes_schema == {"cmd": "str"}
        ((topic, manifest, retain),) = _published(api)
        assert topic == f"agents/{api.actor_id}/manifest"
        assert retain is True
        assert '"subscribes": ["in/t"]' in manifest

    async def test_without_publishes_the_topics_already_used_are_declared(
        self, api: AgentAPI
    ) -> None:
        api._published_topics.add("seen/t")

        api.declare_contract(subscribes=["in/t"], triggers_when={"on": True})
        await asyncio.sleep(0)

        contract = api._actor._topic_contract  # pyright: ignore[reportAttributeAccessIssue]
        assert contract.publishes == ["seen/t"]
        assert contract.triggers_when == {"on": True}

    def test_wiring_opportunities_involving_this_agent(self, api: AgentAPI, bus: TopicBus) -> None:
        bus.register_contract(TopicContract(name="probe", publishes=["t/1"]))
        bus.register_contract(TopicContract(name="consumer", subscribes=["t/#"]))
        bus.register_contract(TopicContract(name="x", publishes=["x/1"]))
        bus.register_contract(TopicContract(name="y", subscribes=["x/1"]))

        assert api.wiring_opportunities() == [
            {"producer": "probe", "consumer": "consumer", "topic": "t/1"}
        ]

    def test_without_a_bus_there_are_no_opportunities(self, api: AgentAPI) -> None:
        assert api.wiring_opportunities() == []
