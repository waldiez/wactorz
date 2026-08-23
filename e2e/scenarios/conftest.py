"""Fixtures shared by the scenarios, as opposed to by the harness.

The line: `harness/` is how to talk to a running system, and knows nothing about
any particular story. What is here is the story - the agents these scenarios
work with, and the phrases that bring them into being.
"""

from __future__ import annotations

import pytest
from harness import backend, waiting

#: What to ask for when a scenario needs an ordinary agent to push around.
#:
#: Names a real catalogue recipe on purpose. A vague request - "spawn a second
#: agent" - is something the fake provider's script can answer and a real model
#: cannot: asked that, it replies pleasantly and spawns nothing, and every
#: scenario downstream fails on an agent that was never going to exist. Asking
#: for a catalogue agent by name works under both, because the catalogue is what
#: actually does the spawning either way.
SPARE_REQUEST = "spawn a manual agent"


@pytest.fixture(scope="session", name="spare_agent")
def spare_agent_fixture(app: backend.Backend) -> str:
    """An ordinary spawned agent to stop, start and delete. Created once, shared.

    Returns whatever the system actually created rather than a name written
    here. The name is the model's to choose - the fake provider's script and a
    real model will not agree on it, and a scenario that hardcoded one would be
    asserting what the model said. What the scenarios downstream need is not a
    particular agent; it is *an* agent that is theirs to stop and delete.
    """
    before = {a["name"] for a in app.rest.agents()}
    app.rest.chat(SPARE_REQUEST)

    def appeared() -> str:
        new = {a["name"] for a in app.rest.agents()} - before
        return sorted(new)[0] if new else ""

    name = waiting.until(
        appeared,
        what=f"an agent to be spawned for {SPARE_REQUEST!r}",
        timeout=120.0,
        interval=0.5,
    )
    waiting.until(
        lambda: app.rest.state_of(name) == "running",
        what=f"{name!r} to reach running",
        timeout=90.0,
        interval=0.5,
    )

    # Checked rather than assumed. A protected agent refuses to be stopped or
    # deleted, so one arriving here would fail the lifecycle scenarios with a
    # refusal that reads like the product breaking rather than like this fixture
    # having handed them the wrong agent.
    entry = app.rest.agent(name) or {}
    assert not entry.get("protected"), (
        f"the spare agent {name!r} is protected, so it cannot be stopped or "
        f"deleted - the scenarios that use it need an ordinary one"
    )
    return name
