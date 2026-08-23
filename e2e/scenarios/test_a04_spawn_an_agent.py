"""A catalogue agent spawns from a conversation and reaches `running`.

Spawning is what the product is for, and it is the longest path in it: a message
reaches main, the model answers with a spawn block, the block is parsed, the
agent is registered, supervised, and reported to the dashboard. Nothing short of
a real run crosses all of that.

`becomes_and_stays` rather than a single check, because a supervisor restarting a
crashing agent puts it in `running` several times a second - which satisfies "it
reached running" and is the opposite of what the claim means.
"""

from harness import backend, waiting

#: Safe to name, unlike an invented agent. `weather-agent` is a maintained
#: catalogue recipe, and asked for something the catalogue already provides the
#: system spawns the recipe rather than whatever the model just wrote - so the
#: name is the product's, not the model's, and holds under both providers.
AGENT = "weather-agent"


def test_a_catalogue_agent_spawns_and_reaches_running(app: backend.Backend) -> None:
    app.rest.chat("spawn a weather agent")

    waiting.until(
        lambda: app.rest.agent(AGENT) is not None,
        what=f"{AGENT!r} to be registered",
        timeout=90.0,
        interval=0.5,
    )
    waiting.becomes_and_stays(
        lambda: app.rest.state_of(AGENT) == "running",
        what=f"{AGENT!r} to be running and stay running",
        timeout=90.0,
        window=3.0,
        interval=0.25,
    )


def test_the_spawned_agent_is_offered_to_the_dashboard(app: backend.Backend) -> None:
    """It exists as far as the browser is concerned, not only as far as the registry is.

    A separate claim from the one above: an agent can be in the registry and
    missing from the payload the dashboard is built from, and that is a bug a
    user sees and the registry never does.
    """
    names = {a["name"] for a in app.rest.agents()}
    assert AGENT in names, f"{AGENT!r} is not in the agent list the dashboard is given: {names}"
