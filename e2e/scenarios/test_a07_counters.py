"""After two stop/start cycles, message count and cost are unchanged.

Restarting an agent must not look like work. The failure this exists for is a
counter that is re-derived rather than restored on start - which makes a restart
inflate the message count, or a cost total, by whatever the agent did before it.
Nobody notices until a bill or a graph is wrong, because every individual number
looks plausible.

Two cycles rather than one: a counter that doubles is visible after one, and a
counter that resets to a stale snapshot is only visible after two.

Compared against captured values, never against numbers written here. What the
count is depends on how much the earlier scenarios talked; what it must not do
is move because an agent was restarted.
"""

import pytest
from harness import backend, browser, waiting


@pytest.fixture(name="agent")
def agent_fixture(app: backend.Backend, spare_agent: str) -> str:
    """The shared spare agent, spoken to once before it is cycled.

    Spoken to, because "unchanged" is trivially true of counters that were never
    anything but zero - the scenario has to have given them something to lose.
    """
    app.rest.chat("hello, are you there", target=spare_agent)
    waiting.until(
        lambda: app.rest.capture(spare_agent, "message_count")["message_count"] is not None,
        what=f"{spare_agent!r} to be reporting a message count",
        timeout=60.0,
        interval=0.25,
    )
    return spare_agent


def _cycle(app: backend.Backend, agent: str) -> None:
    """One stop and one start, each waited out before the next begins."""
    assert app.rest.command(agent, "stop").ok, f"stopping {agent!r} was refused"
    waiting.until(
        lambda: app.rest.state_of(agent) == "stopped",
        what=f"{agent!r} to stop",
        timeout=60.0,
        interval=0.25,
    )
    assert app.rest.command(agent, "start").ok, f"starting {agent!r} was refused"
    waiting.until(
        lambda: app.rest.state_of(agent) == "running",
        what=f"{agent!r} to start again",
        timeout=90.0,
        interval=0.25,
    )


def test_two_cycles_leave_the_counters_where_they_were(app: backend.Backend, agent: str) -> None:
    before = app.rest.capture(agent, "message_count", "cost_usd")

    _cycle(app, agent)
    _cycle(app, agent)

    after = app.rest.capture(agent, "message_count", "cost_usd")
    assert after == before, (
        f"restarting {agent!r} twice changed its counters: {before} became {after}"
    )


def test_the_agent_is_still_on_the_dashboard_afterwards(
    dashboard: browser.Dashboard, agent: str
) -> None:
    """It survived the cycling as something a person can still see and use.

    An agent whose counters are intact and whose card has vanished is not a
    working restart, and the registry cannot tell the difference.
    """
    dashboard.show("overview", dwell="beat")
    dashboard.wait_for_card(agent, dwell="readable")
