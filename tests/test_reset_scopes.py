# SPDX-License-Identifier: Apache-2.0
"""What a reset clears, and for whom: the corners the scope tests leave out.

A kept agent goes back to how a fresh boot starts it; a reset of an agent on a
node leaves this machine's files alone; the cost a metrics reset forgets is the
one banked under the agent's own id.
"""

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import wactorz.reset as reset_module
from wactorz.core.addresses import Address
from wactorz.web import api_reset, runtime


@pytest.fixture(autouse=True)
def _restore_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(runtime, "registry", None)
    monkeypatch.setattr(
        runtime,
        "state",
        {
            "agents": {
                "id-counter": {"name": "counter", "cost_usd": 2.0, "messages_processed": 4},
                "id-edge": {
                    "name": "counter",
                    "node": "edge",
                    "cost_usd": 0.7,
                    "messages_processed": 1,
                },
                "id-weather": {"name": "weather", "cost_usd": 1.0, "messages_processed": 2},
            },
            "nodes": {},
            "alerts": ["an alert"],
            "log_feed": ["a line"],
        },
    )
    yield


class TestAKeptAgentAfterAFactoryReset:
    def test_it_has_no_counters_facts_or_rules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        forgotten: list[str] = []
        for name in ("reset_actor_cost", "forget_conversation", "forget_legacy_state"):
            monkeypatch.setattr(api_reset, name, lambda _actor, name=name: forgotten.append(name))
        actor = SimpleNamespace(
            metrics=SimpleNamespace(
                messages_processed=5, errors=2, tasks_completed=3, tasks_failed=1
            ),
            _user_facts={"likes": "tea"},
            _pipeline_rules=["when hot, open the window"],
        )

        api_reset._to_factory_state(actor)

        assert vars(actor.metrics) == {
            "messages_processed": 0,
            "errors": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
        }
        assert actor._user_facts == {}
        assert actor._pipeline_rules == []
        # The pickle behind the live copies goes last, after them.
        assert forgotten == ["reset_actor_cost", "forget_conversation", "forget_legacy_state"]

    def test_one_without_facts_or_rules_is_given_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("reset_actor_cost", "forget_conversation", "forget_legacy_state"):
            monkeypatch.setattr(api_reset, name, lambda _actor: None)
        actor = SimpleNamespace(
            metrics=SimpleNamespace(
                messages_processed=5, errors=2, tasks_completed=3, tasks_failed=1
            )
        )

        api_reset._to_factory_state(actor)

        assert not hasattr(actor, "_user_facts")
        assert not hasattr(actor, "_pipeline_rules")


class TestAStateResetOfAnAgentOnANode:
    async def test_nothing_here_is_restarted_or_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        supervisor = SimpleNamespace(restart_afresh=AsyncMock())
        monkeypatch.setattr(
            api_reset,
            "_restartable",
            lambda _agent: (supervisor, [SimpleNamespace(name="counter")]),
        )
        removed = MagicMock()
        monkeypatch.setattr(reset_module, "reset_agent_state", removed)
        monkeypatch.setattr(reset_module, "_reset_all_pickles", removed)

        await api_reset._clear_state(
            api_reset._Target("counter", Address("counter", "edge"), "id-edge")
        )

        supervisor.restart_afresh.assert_not_awaited()
        removed.assert_not_called()


class TestTheCostAMetricsResetForgets:
    def test_the_agents_own_id_comes_first(self) -> None:
        target = api_reset._Target("counter", Address("counter", "edge"), "id-edge")

        assert api_reset._cost_id(target) == "id-edge"

    def test_a_name_is_found_among_the_agents_here(self) -> None:
        runtime.registry = SimpleNamespace(
            all_actors=lambda: [SimpleNamespace(name="counter", actor_id="id-live")]
        )

        assert api_reset._cost_id(api_reset._Target("counter")) == "id-live"

    def test_then_among_the_dashboards(self) -> None:
        assert api_reset._cost_id(api_reset._Target("weather")) == "id-weather"

    def test_a_name_nothing_knows_has_none(self) -> None:
        assert api_reset._cost_id(api_reset._Target("ghost")) is None


class TestTheDashboardAfterAMetricsReset:
    def test_one_agent_loses_its_counts_and_nothing_else_does(self) -> None:
        api_reset._clear_dashboard_metrics(
            api_reset._Target("counter", Address("counter", "edge"), "id-edge")
        )

        agents = runtime.state["agents"]
        assert "cost_usd" not in agents["id-edge"]
        assert "messages_processed" not in agents["id-edge"]
        assert agents["id-counter"]["cost_usd"] == 2.0
        assert runtime.state["alerts"] == ["an alert"]

    def test_everyone_loses_them_and_the_alerts_and_feed_go(self) -> None:
        api_reset._clear_dashboard_metrics(api_reset._Target())

        assert all(
            "cost_usd" not in a and "messages_processed" not in a
            for a in runtime.state["agents"].values()
        )
        assert runtime.state["alerts"] == []
        assert runtime.state["log_feed"] == []

    def test_a_name_nothing_knows_changes_nothing(self) -> None:
        before = {aid: dict(a) for aid, a in runtime.state["agents"].items()}

        api_reset._clear_dashboard_metrics(api_reset._Target("ghost"))

        assert runtime.state["agents"] == before
