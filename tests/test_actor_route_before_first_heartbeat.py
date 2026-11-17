# SPDX-License-Identifier: Apache-2.0
"""An agent asked for by its id is found from the moment it is registered.

The list of actors shows an agent of this process before its first heartbeat,
as starting. The route for one actor is asked for the same agent by the page
that has just listed it, a chat opening on it for one, so it answers for it
too, rather than saying there is no such agent.
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
    """Each case starts with no agents recorded, none deleted and no registry."""
    monkeypatch.setattr(runtime, "state", {**runtime.state, "agents": {}})
    monkeypatch.setattr(runtime, "registry", None)
    monkeypatch.setattr(runtime, "is_deleted", lambda _actor_id: False)
    yield


def registered(monkeypatch: pytest.MonkeyPatch, **flags: bool) -> None:
    """A registry holding one agent, ``a1``, that has reported nothing."""
    actor = SimpleNamespace(
        actor_id="a1", name="fresh", protected=False, essential=False, _published_manifest=None
    )
    for flag, value in flags.items():
        setattr(actor, flag, value)
    monkeypatch.setattr(
        runtime,
        "registry",
        SimpleNamespace(
            get=lambda actor_id: actor if actor_id == "a1" else None,
            find_by_name=lambda _name: None,
            all_actors=lambda: [actor],
        ),
    )


async def asked(actor_id: str = "a1") -> tuple[int, dict[str, Any]]:
    request = cast(web.Request, SimpleNamespace(match_info={"actor_id": actor_id}))
    response = await api_actors.actor_handler(request)
    assert isinstance(response.body, bytes)
    return response.status, json.loads(response.body)


class TestAnAgentThatHasReportedNothing:
    async def test_it_is_answered_for_as_starting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registered(monkeypatch)

        status, payload = await asked()

        assert status == 200
        assert payload["id"] == "a1"
        assert payload["name"] == "fresh"
        assert payload["state"] == events.STARTING

    async def test_its_flags_are_the_agents_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registered(monkeypatch, protected=True, essential=True)

        _status, payload = await asked()

        assert payload["protected"] is True
        assert payload["essential"] is True

    async def test_no_entry_is_opened_for_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Its first heartbeat opens the entry; asking for it does not.
        registered(monkeypatch)

        await asked()

        assert runtime.state["agents"] == {}


class TestWhatIsStillNotFound:
    async def test_an_id_nobody_has(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registered(monkeypatch)

        status, payload = await asked("nobody")

        assert status == 404
        assert payload == {"error": "actor not found"}

    async def test_an_agent_that_was_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registered(monkeypatch)
        monkeypatch.setattr(runtime, "is_deleted", lambda actor_id: actor_id == "a1")

        status, _payload = await asked()

        assert status == 404

    async def test_any_id_with_no_registry_in_this_process(self) -> None:
        status, _payload = await asked()

        assert status == 404


class TestAnAgentThatHasReported:
    async def test_what_it_reported_is_what_is_answered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registered(monkeypatch)
        events.record_heartbeat("a1", {"name": "fresh", "state": "running"})

        _status, payload = await asked()

        assert payload["state"] == "running"
