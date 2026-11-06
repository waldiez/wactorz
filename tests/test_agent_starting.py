# SPDX-License-Identifier: Apache-2.0
"""An agent the server has not heard from yet is listed as starting.

An agent's `state` on the dashboard is the last one it reported, so "running"
means ready: heard from over MQTT. A local agent is listed from the registry
before its first heartbeat, and a metric can open its entry before one too;
until it reports, its state is `starting`, never a guess.
"""

import json
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp import web

from wactorz.web import api_actors, events, runtime


@pytest.fixture(name="clean_state", autouse=True)
def clean_state_fixture(monkeypatch: pytest.MonkeyPatch) -> Generator[None, Any, None]:
    """Each case starts with no agents recorded and no registry."""
    monkeypatch.setattr(runtime, "state", {**runtime.state, "agents": {}})
    monkeypatch.setattr(runtime, "registry", None)
    yield


def state_of(agent_id: str = "a1") -> str:
    return runtime.state["agents"][agent_id]["state"]


def with_registry(monkeypatch: pytest.MonkeyPatch, *actor_ids: str) -> None:
    actors = [
        SimpleNamespace(actor_id=i, name=f"agent-{i}", protected=False, metrics=None)
        for i in actor_ids
    ]
    monkeypatch.setattr(
        runtime, "registry", SimpleNamespace(all_actors=lambda: actors, get=lambda _id: None)
    )


async def listed() -> list[dict[str, Any]]:
    request = cast(web.Request, SimpleNamespace(app=SimpleNamespace()))
    response = await api_actors.actors_handler(request)
    assert isinstance(response.body, bytes)
    return json.loads(response.body)


class TestTheEntry:
    def test_a_metric_opens_an_entry_that_is_starting(self) -> None:
        events.update_agent("a1", "metrics", {"cost_usd": 0.1})

        assert state_of() == events.STARTING

    def test_a_heartbeat_gives_the_state_it_reports(self) -> None:
        events.update_agent("a1", "metrics", {"cost_usd": 0.1})
        events.record_heartbeat("a1", {"name": "agent", "state": "running"})

        assert state_of() == "running"

    def test_a_status_gives_the_state_it_reports(self) -> None:
        events.update_agent("a1", "metrics", {"cost_usd": 0.1})
        events._on_status("a1", {"state": "stopped"})

        assert state_of() == "stopped"

    def test_the_snapshot_carries_it(self) -> None:
        events.update_agent("a1", "metrics", {})
        events.record_heartbeat("a2", {"name": "other", "state": "running"})

        by_id = {a["agent_id"]: a for a in events.snapshot(include_totals=False)["agents"]}
        assert by_id["a1"]["state"] == events.STARTING
        assert by_id["a2"]["state"] == "running"


class TestTheList:
    async def test_an_agent_not_yet_heard_from_is_starting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_registry(monkeypatch, "a1")

        (row,) = await listed()

        assert row["state"] == events.STARTING

    async def test_an_agent_heard_from_only_by_a_metric_is_starting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_registry(monkeypatch, "a1")
        events.update_agent("a1", "metrics", {})

        (row,) = await listed()

        assert row["state"] == events.STARTING

    async def test_after_a_heartbeat_it_is_what_the_agent_said(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_registry(monkeypatch, "a1")
        events.record_heartbeat("a1", {"name": "agent-a1", "state": "running"})

        (row,) = await listed()

        assert row["state"] == "running"

    async def test_without_a_registry_the_list_says_it_too(self) -> None:
        events.update_agent("a1", "metrics", {})
        events.record_heartbeat("a2", {"name": "other", "state": "running"})

        by_id = {row["id"]: row for row in await listed()}

        assert by_id["a1"]["state"] == events.STARTING
        assert by_id["a2"]["state"] == "running"
