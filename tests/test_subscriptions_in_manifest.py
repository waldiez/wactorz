# SPDX-License-Identifier: Apache-2.0
"""What a program subscribes to is in its manifest, as what it publishes is.

The manifest is how main, and every other host, learns what an agent reads:
main mirrors each one into its topic registry, which the wiring view and the
planner read. So a program that only calls ``agent.subscribe()`` and declares no
contract has to have its subscriptions in the manifest: main's mirror replaces
the contract the agent registered, and for an agent on a node the manifest is
the only route its topics take.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic import streams
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.agents.dynamic.api import AgentAPI
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.core import topic_bus
from wactorz.core.actor import ActorState
from wactorz.core.manifests import AgentManifest
from wactorz.core.topic_bus import TopicBus


class _Hub:
    """Stands in for the subscription hub: connects to nothing."""

    def bind(self, _topic: str, _callback: Any) -> None:
        return None


@pytest.fixture(name="bus")
def bus_fixture(monkeypatch: pytest.MonkeyPatch) -> TopicBus:
    bus = TopicBus()
    monkeypatch.setattr(topic_bus, "_topic_bus", bus)
    return bus


@pytest.fixture(name="sent")
def sent_fixture() -> list[AgentManifest]:
    return []


@pytest.fixture(name="api")
def api_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent: list[AgentManifest]
) -> AgentAPI:
    actor = DynamicAgent(name="reader", code="", persistence_dir=str(tmp_path))

    async def _send(manifest: AgentManifest) -> None:
        sent.append(manifest)

    monkeypatch.setattr(actor, "_send_manifest", _send)
    monkeypatch.setattr(streams, "hub_for", lambda _actor: _Hub())
    return AgentAPI(actor)


async def _on(_payload: Any) -> None:
    return None


async def _other(_payload: Any) -> None:
    return None


async def _settled(api: AgentAPI) -> None:
    """Let the manifests a subscription sends in the background go out."""
    for task in [t for t in api._actor._tasks if t.get_name() == "manifest"]:
        await task


class TestTheManifest:
    async def test_it_names_what_the_program_subscribes_to(
        self, api: AgentAPI, sent: list[AgentManifest]
    ) -> None:
        api.subscribe("sensors/air", _on)
        await _settled(api)

        assert sent[-1].get("subscribes") == ["sensors/air"]

    async def test_it_is_sent_again_for_a_new_topic_only(
        self, api: AgentAPI, sent: list[AgentManifest]
    ) -> None:
        api.subscribe("sensors/air", _on)
        api.subscribe("sensors/air", _other)  # the same topic, another callback
        api.subscribe("sensors/air", _on)  # a duplicate, skipped
        await _settled(api)
        assert len(sent) == 1

        api.subscribe("sensors/water", _on)
        await _settled(api)

        assert len(sent) == 2
        assert sent[-1].get("subscribes") == ["sensors/air", "sensors/water"]

    async def test_it_keeps_what_a_contract_declares_too(
        self, api: AgentAPI, sent: list[AgentManifest], bus: TopicBus
    ) -> None:
        api.declare_contract(subscribes=["home/#"])
        api.subscribe("sensors/air", _on)
        await _settled(api)

        assert sent[-1].get("subscribes") == ["home/#", "sensors/air"]


class TestMainsMirror:
    async def test_it_keeps_the_subscription_the_agent_registered(
        self, api: AgentAPI, sent: list[AgentManifest], bus: TopicBus
    ) -> None:
        # The agent registers its subscription; then main takes its manifest,
        # which replaces that contract with its own reading of it.
        api.subscribe("sensors/air", _on)
        await _settled(api)
        ManifestRegistry()._register_contract(api.name, sent[-1], [])

        contract = bus.registry.get(api.name, api.actor_id, None)
        assert contract is not None and contract.subscribes == ["sensors/air"]


class TestARepair:
    """A repaired program starts with no subscriptions: the manifest says so."""

    async def test_the_old_programs_topics_leave_the_manifest(
        self, api: AgentAPI, sent: list[AgentManifest]
    ) -> None:
        api.subscribe("sensors/old", _on)
        await _settled(api)

        # As a repair does before the new program runs, which here subscribes to nothing.
        await api._actor._tear_down_program()

        assert sent[-1].get("subscribes") == []

    async def test_with_nothing_subscribed_nothing_is_sent(
        self, api: AgentAPI, sent: list[AgentManifest]
    ) -> None:
        await api._actor._tear_down_program()

        assert sent == []

    async def test_a_stopped_agent_sends_none(
        self, api: AgentAPI, sent: list[AgentManifest]
    ) -> None:
        # A stop withdraws the manifest; one sent after would put it back.
        api.subscribe("sensors/old", _on)
        await _settled(api)
        sent.clear()
        api._actor.state = ActorState.STOPPED

        await api._actor._tear_down_program()

        assert sent == []
