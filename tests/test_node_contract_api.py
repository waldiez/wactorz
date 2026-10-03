"""The contract and discovery surface a node exposes to generated code.

`declare_contract` is what makes an agent visible to the planner, and it is
called from LLM-written `setup()`. Most of it exists to absorb the shapes a
model actually produces — aliased keyword names, a bare string where a list is
meant, and `await` on a call that is not a coroutine. A model writing `schema=`
instead of `produces_schema=` must not be silently dropped.

These run through a node agent because a node used to answer them with an
implementation of its own. It answers with the shared one now, and what is still
node-specific is the *scope* of the answer: this node alone, because the
cluster-wide view lives on main.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic.api import AgentAPI
from wactorz.node.agent import NodeAgent
from wactorz.node.runner import NodeRunner


@pytest.fixture(name="runner")
def runner_fixture(tmp_path: Path) -> NodeRunner:
    return NodeRunner("localhost", 1883, "rpi-kitchen", state_dir=str(tmp_path))


def _make_api(runner: NodeRunner, **config: Any) -> AgentAPI:
    """An agent registered on its node, with manifest publishing stubbed out.

    `declare_contract` fires `_publish_manifest()` as a task; on a real node
    that reaches MQTT, which these tests neither need nor have.
    """
    agent = NodeAgent({"name": "edge-sensor", "code": "", **config}, runner)
    runner.registry._actors[agent.actor_id] = agent
    agent._registry = runner.registry

    async def _no_publish(*_a: Any, **_kw: Any) -> None:
        return None

    agent._api._publish_manifest = _no_publish  # type: ignore[method-assign]
    return agent._api


@pytest.fixture(name="api")
def api_fixture(runner: NodeRunner) -> AgentAPI:
    return _make_api(runner)


def _contract(api: AgentAPI) -> Any:
    return api._actor._topic_contract


class TestDeclareContractAcceptsTheShapesModelsProduce:
    @pytest.mark.parametrize("alias", ["schema", "output_schema", "produce_schema"])
    async def test_produces_schema_aliases(self, api: AgentAPI, alias: str) -> None:
        # The alias is the point of the test, so it has to be the *keyword*.
        # Typed as dict[str, Any] so unpacking does not trip the checker on
        # parameters it cannot statically match.
        kwargs: dict[str, Any] = {alias: {"temp": "float"}}
        await api.declare_contract(**kwargs)
        assert _contract(api).produces_schema == {"temp": "float"}

    @pytest.mark.parametrize("alias", ["input_schema", "consume_schema"])
    async def test_consumes_schema_aliases(self, api: AgentAPI, alias: str) -> None:
        kwargs: dict[str, Any] = {alias: {"cmd": "str"}}
        await api.declare_contract(**kwargs)
        assert _contract(api).consumes_schema == {"cmd": "str"}

    @pytest.mark.parametrize("alias", ["topics", "publish"])
    async def test_publishes_aliases(self, api: AgentAPI, alias: str) -> None:
        kwargs: dict[str, Any] = {alias: ["sensors/temp"]}
        await api.declare_contract(**kwargs)
        assert "sensors/temp" in _contract(api).publishes

    async def test_subscribes_alias(self, api: AgentAPI) -> None:
        await api.declare_contract(subscribe=["cmd/led"])
        assert _contract(api).subscribes == ["cmd/led"]

    async def test_a_bare_string_is_treated_as_one_topic(self, api: AgentAPI) -> None:
        # Models write `publishes="sensors/temp"` as often as they write a list;
        # iterating the string would otherwise declare one topic per character.
        await api.declare_contract(publishes="sensors/temp", subscribes="cmd/led")
        assert _contract(api).publishes == ["sensors/temp"]
        assert _contract(api).subscribes == ["cmd/led"]

    async def test_the_result_can_be_awaited(self, api: AgentAPI) -> None:
        # `declare_contract` is not a coroutine, but generated code awaits it.
        assert await api.declare_contract(publishes="a/b") is None

    async def test_calling_it_twice_does_not_duplicate_a_subscription(self, api: AgentAPI) -> None:
        # setup() runs again on reconnect.
        await api.declare_contract(subscribes=["cmd/led"])
        await api.declare_contract(subscribes=["cmd/led"])
        assert _contract(api).subscribes == ["cmd/led"]

    async def test_later_calls_add_rather_than_replace(self, api: AgentAPI) -> None:
        await api.declare_contract(publishes="a/one", triggers_when={"x": 1})
        await api.declare_contract(publishes="a/two", triggers_when={"y": 2})
        assert _contract(api).publishes == ["a/one", "a/two"]
        assert _contract(api).triggers_when == {"x": 1, "y": 2}


class TestTopics:
    async def test_it_reports_published_and_subscribed_together(self, api: AgentAPI) -> None:
        await api.declare_contract(publishes="sensors/temp", subscribes="cmd/led")
        assert [t["topic"] for t in api.topics()] == ["cmd/led", "sensors/temp"]

    async def test_the_keyword_filters_case_insensitively(self, api: AgentAPI) -> None:
        await api.declare_contract(publishes=["sensors/temp", "cmd/led"])
        assert [t["topic"] for t in api.topics("SENSORS")] == ["sensors/temp"]

    async def test_each_topic_names_this_agent_and_node(self, api: AgentAPI) -> None:
        await api.declare_contract(publishes="sensors/temp")
        assert api.topics()[0]["agents"] == [{"name": "edge-sensor", "node": "rpi-kitchen"}]

    def test_nothing_declared_means_no_topics(self, api: AgentAPI) -> None:
        assert api.topics() == []


class TestCapabilities:
    def test_it_describes_this_agent_from_its_config(self, runner: NodeRunner) -> None:
        api = _make_api(runner, description="reads a probe", capabilities=["temp"])
        [entry] = api.capabilities()
        assert entry["name"] == "edge-sensor"
        assert entry["capabilities"] == ["temp"]

    def test_a_keyword_matches_the_description(self, runner: NodeRunner) -> None:
        api = _make_api(runner, description="reads a probe")
        assert api.capabilities("probe") != []

    def test_a_keyword_matches_the_agent_name(self, runner: NodeRunner) -> None:
        assert _make_api(runner).capabilities("sensor") != []

    def test_a_keyword_matching_neither_returns_nothing(self, api: AgentAPI) -> None:
        assert api.capabilities("thermostat") == []


class TestWhatANodeCannotAnswer:
    def test_it_sees_only_itself_as_a_node(self, api: AgentAPI) -> None:
        [node] = api.nodes()
        assert node["node"] == "rpi-kitchen"
        assert node["online"] is True
        assert node["agents"] == ["edge-sensor"]

    def test_wiring_opportunities_is_empty_rather_than_wrong(self, api: AgentAPI) -> None:
        # The TopicBus runs in the main process; guessing here would be worse
        # than answering nothing.
        assert not api.wiring_opportunities()

    async def test_the_answers_are_scoped_to_this_node(self, runner: NodeRunner) -> None:
        # Two agents on one node see each other and nothing beyond.
        first = _make_api(runner)
        second = _make_api(runner, name="edge-relay")
        await first.declare_contract(publishes="sensors/temp")
        await second.declare_contract(publishes="relay/state")

        assert sorted(n for n in first.nodes()[0]["agents"]) == ["edge-relay", "edge-sensor"]
        assert [t["topic"] for t in first.topics()] == ["relay/state", "sensors/temp"]
