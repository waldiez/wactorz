"""The flags an agent publishes have to survive the trip to the browser.

A heartbeat carries `protected` and `essential`; the dashboard decides from them
which buttons a card offers. Each one is copied out by name, so a flag added to
the actor and not to this path arrives as its default -- and the interface then
offers a control the agent refuses, which reads as a broken button rather than a
rule.
"""

from typing import Any

import pytest

from wactorz.web import events, runtime
from wactorz.web.api_actors import _actor_payload


@pytest.fixture(name="clean_state", autouse=True)
def clean_state_fixture() -> Any:
    """Each case starts with no agents recorded."""
    before = runtime.state.get("agents")
    runtime.state["agents"] = {}
    yield
    runtime.state["agents"] = before if before is not None else {}


def beat(**fields: Any) -> dict[str, Any]:
    events.record_heartbeat("a1", {"name": "agent", "state": "running", **fields})
    return _actor_payload(runtime.state["agents"]["a1"] | {"agent_id": "a1"})


class TestFlagsSurviveTheHeartbeat:
    def test_essential_reaches_the_card(self) -> None:
        assert beat(protected=True, essential=True)["essential"] is True

    def test_protected_reaches_the_card(self) -> None:
        assert beat(protected=True, essential=False)["protected"] is True

    def test_an_agent_that_sends_neither_is_offered_everything(self) -> None:
        payload = beat()

        assert payload["protected"] is False
        assert payload["essential"] is False

    def test_the_two_are_independent(self) -> None:
        payload = beat(protected=True, essential=False)

        # The pairing that matters: protected without essential is the case that
        # keeps Delete away while leaving Stop available.
        assert payload["protected"] is True
        assert payload["essential"] is False
