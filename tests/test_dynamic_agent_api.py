"""The surface generated agent code is handed as `agent`.

This is a contract two other places depend on: the planner repairs generated
code against its shape, and every catalogue recipe is written to it. Changing a
method from sync to async here breaks both silently, which is what these pin.
"""

import inspect
from pathlib import Path

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.dynamic.api import _AgentAPI
from wactorz.agents.planner.validation import SYNC_METHODS


@pytest.fixture(name="api")
def api_fixture(tmp_path: Path) -> _AgentAPI:
    actor = DynamicAgent(name="probe", code="", persistence_dir=str(tmp_path))
    return _AgentAPI(actor)


class TestTheSyncAsyncContract:
    """Which calls need `await` is the single thing generated code gets wrong most."""

    @pytest.mark.parametrize("method", SYNC_METHODS)
    def test_what_the_planner_strips_await_from_is_really_synchronous(self, method: str) -> None:
        """`validate_pipeline_code` removes `await` from each of these.

        If one ever becomes a coroutine, the planner would strip the `await`
        that makes it work and the failure would appear in generated code far
        from the change. The two lists have to agree.
        """
        fn = getattr(_AgentAPI, method, None)

        assert fn is not None, f"{method} is in SYNC_METHODS but not on the API"
        assert not inspect.iscoroutinefunction(fn), f"{method} became async"

    @pytest.mark.parametrize("method", ["publish", "log", "alert", "send_to", "mqtt_get", "chat"])
    def test_the_calls_documented_as_async_still_are(self, method: str) -> None:
        """The prompt tells the model to await these; it must stay true."""
        assert inspect.iscoroutinefunction(getattr(_AgentAPI, method))


class TestIdentity:
    def test_the_api_reports_the_actor_name(self, api: _AgentAPI) -> None:
        assert api.name == "probe"

    def test_the_api_reports_the_actor_id(self, api: _AgentAPI, tmp_path: Path) -> None:
        assert api.actor_id

    def test_node_is_a_string_even_when_unset(self, api: _AgentAPI) -> None:
        """Generated code interpolates this into topics, so it cannot be None."""
        assert isinstance(api.node, str)


class TestStateRoundTrip:
    def test_what_is_persisted_can_be_recalled(self, api: _AgentAPI) -> None:
        api.persist("last_seen", {"value": 42})

        assert api.recall("last_seen") == {"value": 42}

    def test_recalling_something_never_stored_gives_none(self, api: _AgentAPI) -> None:
        assert api.recall("never_written") is None

    def test_recall_accepts_a_default(self, api: _AgentAPI) -> None:
        assert api.recall("never_written", "fallback") == "fallback"

    def test_state_is_a_mapping_the_code_can_use(self, api: _AgentAPI) -> None:
        api.state["counter"] = 1

        assert api.state["counter"] == 1


class TestCounters:
    def test_processed_increments(self, api: _AgentAPI) -> None:
        before = api._actor.metrics.messages_processed
        api.increment_processed()

        assert api._actor.metrics.messages_processed == before + 1

    def test_errors_increment(self, api: _AgentAPI) -> None:
        before = api._actor.metrics.errors
        api.increment_errors()

        assert api._actor.metrics.errors == before + 1


class TestDiscovery:
    def test_agents_returns_a_list_without_a_registry(self, api: _AgentAPI) -> None:
        """Generated code iterates the result, so it must never be None."""
        assert isinstance(api.agents(), list)

    def test_topics_returns_a_list_without_a_bus(self, api: _AgentAPI) -> None:
        assert isinstance(api.topics(), list)

    def test_nodes_returns_a_list_without_a_registry(self, api: _AgentAPI) -> None:
        assert isinstance(api.nodes(), list)
