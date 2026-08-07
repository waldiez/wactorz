"""The contract and discovery surface a remote node exposes to generated code.

`declare_contract` is what makes a remote agent visible to the planner, and it
is called from LLM-written `setup()`. Most of it exists to absorb the shapes a
model actually produces — aliased keyword names, a bare string where a list is
meant, and `await` on a call that is not a coroutine. None of that was covered,
so a model writing `schema=` instead of `produces_schema=` would have been
silently dropped rather than folded into the manifest.

The introspection helpers answer from what this node alone can see; the
cluster-wide view lives on main. Their value is the shape of the answer, which
generated code reads directly.
"""

from typing import Any

import pytest

from wactorz import remote_runner


class _FakeRunner:
    def __init__(self, agents: list[Any]) -> None:
        self._agents = {a.name: a for a in agents}


class _FakeAgent:
    """Only the attributes `_RemoteAgentAPI` reaches through."""

    def __init__(self, name: str = "edge-sensor", config: dict[str, Any] | None = None) -> None:
        self.name = name
        self.actor_id = "actor-1"
        self.node_name = "rpi-kitchen"
        self._state: dict[str, Any] = {}
        self._config: dict[str, Any] = config or {}
        self._runner = _FakeRunner([self])


@pytest.fixture(name="api")
def api_fixture(monkeypatch: pytest.MonkeyPatch) -> remote_runner._RemoteAgentAPI:
    """An API bound to a fake agent, with manifest publishing stubbed out.

    `declare_contract` fires `_publish_manifest()` as a task; on a real node
    that reaches MQTT, which these tests neither need nor have.
    """
    api = remote_runner._RemoteAgentAPI(_FakeAgent())  # pyright: ignore[reportArgumentType]

    async def _no_publish() -> None:
        return None

    monkeypatch.setattr(api, "_publish_manifest", _no_publish)
    return api


class TestDeclareContractAcceptsTheShapesModelsProduce:
    @pytest.mark.parametrize("alias", ["schema", "output_schema", "produce_schema"])
    async def test_produces_schema_aliases(
        self, api: remote_runner._RemoteAgentAPI, alias: str
    ) -> None:
        # The alias is the point of the test, so it has to be the *keyword*.
        # Typed as dict[str, Any] so unpacking does not trip the checker on
        # parameters it cannot statically match.
        kwargs: dict[str, Any] = {alias: {"temp": "float"}}
        await api.declare_contract(**kwargs)
        assert api._declared_produces_schema == {"temp": "float"}

    @pytest.mark.parametrize("alias", ["input_schema", "consume_schema"])
    async def test_consumes_schema_aliases(
        self, api: remote_runner._RemoteAgentAPI, alias: str
    ) -> None:
        kwargs: dict[str, Any] = {alias: {"cmd": "str"}}
        await api.declare_contract(**kwargs)
        assert api._declared_consumes_schema == {"cmd": "str"}

    @pytest.mark.parametrize("alias", ["topics", "publish"])
    async def test_publishes_aliases(self, api: remote_runner._RemoteAgentAPI, alias: str) -> None:
        kwargs: dict[str, Any] = {alias: ["sensors/temp"]}
        await api.declare_contract(**kwargs)
        assert "sensors/temp" in api._published_topics

    async def test_subscribes_alias(self, api: remote_runner._RemoteAgentAPI) -> None:
        await api.declare_contract(subscribe=["cmd/led"])
        assert api._declared_subscribes == ["cmd/led"]

    async def test_a_bare_string_is_treated_as_one_topic(
        self, api: remote_runner._RemoteAgentAPI
    ) -> None:
        # Models write `publishes="sensors/temp"` as often as they write a list;
        # iterating the string would otherwise declare one topic per character.
        await api.declare_contract(publishes="sensors/temp", subscribes="cmd/led")
        assert api._published_topics == {"sensors/temp"}
        assert api._declared_subscribes == ["cmd/led"]

    async def test_the_result_can_be_awaited(self, api: remote_runner._RemoteAgentAPI) -> None:
        # `declare_contract` is not a coroutine, but generated code awaits it.
        assert await api.declare_contract(publishes="a/b") is None

    async def test_calling_it_twice_does_not_duplicate_a_subscription(
        self, api: remote_runner._RemoteAgentAPI
    ) -> None:
        # setup() runs again on reconnect.
        await api.declare_contract(subscribes=["cmd/led"])
        await api.declare_contract(subscribes=["cmd/led"])
        assert api._declared_subscribes == ["cmd/led"]

    async def test_later_calls_add_rather_than_replace(
        self, api: remote_runner._RemoteAgentAPI
    ) -> None:
        await api.declare_contract(publishes="a/one", triggers_when={"x": 1})
        await api.declare_contract(publishes="a/two", triggers_when={"y": 2})
        assert api._published_topics == {"a/one", "a/two"}
        assert api._declared_triggers_when == {"x": 1, "y": 2}


class TestTopics:
    async def test_it_reports_published_and_subscribed_together(
        self, api: remote_runner._RemoteAgentAPI
    ) -> None:
        await api.declare_contract(publishes="sensors/temp", subscribes="cmd/led")
        assert [t["topic"] for t in api.topics()] == ["cmd/led", "sensors/temp"]

    async def test_the_keyword_filters_case_insensitively(
        self, api: remote_runner._RemoteAgentAPI
    ) -> None:
        await api.declare_contract(publishes=["sensors/temp", "cmd/led"])
        assert [t["topic"] for t in api.topics("SENSORS")] == ["sensors/temp"]

    async def test_each_topic_names_this_agent_and_node(
        self, api: remote_runner._RemoteAgentAPI
    ) -> None:
        await api.declare_contract(publishes="sensors/temp")
        assert api.topics()[0]["agents"] == [{"name": "edge-sensor", "node": "rpi-kitchen"}]

    def test_nothing_declared_means_no_topics(self, api: Any) -> None:
        assert api.topics() == []


class TestCapabilities:
    def test_it_describes_this_agent_from_its_config(self) -> None:
        agent = _FakeAgent(config={"description": "reads a probe", "capabilities": ["temp"]})
        api = remote_runner._RemoteAgentAPI(agent)  # pyright: ignore[reportArgumentType]
        [entry] = api.capabilities()
        assert entry["name"] == "edge-sensor"
        assert entry["capabilities"] == ["temp"]

    def test_a_keyword_matches_the_description(self) -> None:
        agent = _FakeAgent(config={"description": "reads a probe"})
        api = remote_runner._RemoteAgentAPI(agent)  # pyright: ignore[reportArgumentType]
        assert api.capabilities("probe") != []

    def test_a_keyword_matches_the_agent_name(self) -> None:
        agent = _FakeAgent(config={"description": ""})
        api = remote_runner._RemoteAgentAPI(agent)  # pyright: ignore[reportArgumentType]
        assert api.capabilities("sensor") != []

    def test_a_keyword_matching_neither_returns_nothing(
        self, api: remote_runner._RemoteAgentAPI
    ) -> None:
        assert api.capabilities("thermostat") == []


class TestWhatARemoteNodeCannotAnswer:
    def test_it_sees_only_itself_as_a_node(self, api: remote_runner._RemoteAgentAPI) -> None:
        [node] = api.nodes()
        assert node["node"] == "rpi-kitchen"
        assert node["online"] is True
        assert node["agents"] == ["edge-sensor"]

    def test_wiring_opportunities_is_empty_rather_than_wrong(
        self, api: remote_runner._RemoteAgentAPI
    ) -> None:
        # The TopicBus runs in the main process; guessing here would be worse
        # than answering nothing.
        assert not api.wiring_opportunities()
