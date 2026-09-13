"""Stop, start, delete - and `main` refusing to be stopped.

One test per row. Each of these is a case where the system can report success
while having done nothing at all: an accepted command that changes no state reads
exactly like a working one from the caller's side. Split so a failure names the
claim that broke rather than "lifecycle".

The order matters and is the order a person would use: stop before start,
delete last. They share the shared backend, so the agent this operates on is the
one `a04` spawned.
"""

from harness import backend, waiting

AGENT = "weather-agent"


def _state_becomes(app: backend.Backend, agent: str, wanted: str, *, timeout: float = 60.0) -> None:
    waiting.until(
        lambda: app.rest.state_of(agent) == wanted,
        what=f"{agent!r} to report {wanted!r} (it reports {app.rest.state_of(agent)!r})",
        timeout=timeout,
        interval=0.25,
    )


def test_an_agent_can_be_stopped(app: backend.Backend) -> None:
    response = app.rest.command(AGENT, "stop")
    assert response.ok, f"stopping {AGENT!r} was refused: {response.status} {response.body[:200]}"
    _state_becomes(app, AGENT, "stopped")


def test_a_stopped_agent_can_be_started_and_stays_supervised(app: backend.Backend) -> None:
    """Started, and still there a while later.

    The window is the claim. An agent that starts and immediately dies is
    restarted by its supervisor, so a single check after `start` catches it
    alive and reports success for a system in a crash loop.
    """
    response = app.rest.command(AGENT, "start")
    assert response.ok, f"starting {AGENT!r} was refused: {response.status} {response.body[:200]}"
    waiting.becomes_and_stays(
        lambda: app.rest.state_of(AGENT) == "running",
        what=f"{AGENT!r} to start and stay running",
        timeout=90.0,
        window=10.0,
        interval=0.5,
    )


def test_main_refuses_to_be_stopped(app: backend.Backend) -> None:
    """The one agent everything else routes through will not be turned off.

    A refusal, and then evidence that the refusal meant it: an endpoint can
    return an error and stop the agent anyway, and that failure is invisible
    from the status code alone. Refused as a rule about the agent, so the status
    is 403 - a 404 or 405 would also be "not ok" while testing nothing.
    """
    response = app.rest.command("main", "stop")
    assert response.status == 403, (
        f"stopping main answered {response.status}; it is essential and must be refused"
    )
    assert app.rest.state_of("main") == "running", (
        f"main was refused and stopped anyway - it reports {app.rest.state_of('main')!r}"
    )


def test_an_agent_can_be_deleted(app: backend.Backend) -> None:
    """Deleted, and gone from what the dashboard is told.

    Last, because everything above needs the agent to exist.
    """
    response = app.rest.delete(AGENT)
    assert response.ok, f"deleting {AGENT!r} was refused: {response.status} {response.body[:200]}"
    waiting.until(
        lambda: app.rest.agent(AGENT) is None,
        what=f"{AGENT!r} to disappear from the agent list",
        timeout=60.0,
        interval=0.25,
    )
