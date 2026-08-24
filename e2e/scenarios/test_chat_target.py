"""The agent you chose survives a reload, and a stopped one is refused rather
than silently switched.

Two halves of one rule: the chat target is the user's choice and nothing else's.
The bugs on either side of it are both quiet. On one side, a reload that resets
to `main` loses a conversation without saying anything. On the other, a target
that has gone away and is silently replaced sends the next message to somebody
else - the worst kind of failure, because it looks like it worked.

A stopped agent is not a gone agent, and the distinction is the point: gone means
move and say so, stopped means stay and refuse.
"""

from harness import backend, browser, waiting


def test_the_chosen_agent_survives_a_reload(
    dashboard: browser.Dashboard, app: backend.Backend, spare_agent: str
) -> None:
    waiting.until(
        lambda: app.rest.agent(spare_agent) is not None,
        what=f"{spare_agent!r} to exist before it can be chosen",
        timeout=90.0,
        interval=0.5,
    )

    dashboard.choose_target(spare_agent, dwell="beat")
    assert dashboard.target() == spare_agent, "the composer did not take the chosen target"

    dashboard.reload()
    dashboard.show("chat", dwell="readable")

    waiting.until(
        lambda: dashboard.target() == spare_agent,
        what=f"the composer to reopen on {spare_agent!r} rather than the default",
        timeout=30.0,
        interval=0.25,
    )


def test_a_stopped_agent_is_kept_and_refused_not_switched(
    dashboard: browser.Dashboard, app: backend.Backend, spare_agent: str
) -> None:
    """Stopping the agent you are talking to leaves you talking to it.

    The assertion is about the target, not about an error message: what must not
    happen is the message going somewhere else. A refusal the user can see is the
    good outcome; a silent redirect to `main` is the bug.
    """
    dashboard.choose_target(spare_agent)
    assert app.rest.command(spare_agent, "stop").ok, f"stopping {spare_agent!r} was refused"
    waiting.until(
        lambda: app.rest.state_of(spare_agent) == "stopped",
        what=f"{spare_agent!r} to stop",
        timeout=60.0,
        interval=0.25,
    )

    dashboard.show("chat", dwell="beat")
    assert dashboard.target() == spare_agent, (
        f"the composer moved to {dashboard.target()!r} because the agent was stopped; "
        f"stopped is not gone, and the target is the user's choice"
    )

    app.rest.command(spare_agent, "start")


def test_a_deleted_agent_moves_the_target_instead_of_stranding_it(
    dashboard: browser.Dashboard, app: backend.Backend, spare_agent: str
) -> None:
    """Gone is the other case, and it must move rather than sit on a dead name.

    The pair to the test above. Keeping the target on an agent that no longer
    exists would leave the composer addressed to nothing, which is the failure
    the "stopped is not gone" rule must not cause on its way to fixing the other.
    """
    waiting.until(
        lambda: app.rest.state_of(spare_agent) == "running",
        what=f"{spare_agent!r} to be running again",
        timeout=90.0,
        interval=0.25,
    )
    dashboard.choose_target(spare_agent)

    assert app.rest.delete(spare_agent).ok, f"deleting {spare_agent!r} was refused"
    waiting.until(
        lambda: app.rest.agent(spare_agent) is None,
        what=f"{spare_agent!r} to be gone",
        timeout=60.0,
        interval=0.25,
    )

    dashboard.show("chat")
    waiting.until(
        lambda: dashboard.target() != spare_agent and dashboard.target() != "",
        what="the composer to move off the deleted agent",
        timeout=60.0,
        interval=0.25,
    )
