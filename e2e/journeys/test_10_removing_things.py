"""Agents deleted one by one, then everything wiped: a fresh install, its node still there.

Last, because nothing after it has anything to start from.
"""

from harness import backend, browser, node, waiting
from harness.probe import NODE_POLL_S
from harness.run import NODE_NAME

#: Long enough for the node to report what it runs: a heartbeat or two.
REPORTED_WITHIN_S = 25.0

#: Where the node keeps the state of the agent journey 06 moved there.
COUNTER_STATE = f"{node.HOME}/wactorz/state/counter_state.json"


def _on_the_node(app: backend.Backend) -> set[str] | None:
    """The agents the node says it is running, or nothing if it has not been heard from."""
    for listed in app.rest.nodes():
        if (listed.get("node") or listed.get("name")) == NODE_NAME:
            return set(listed.get("agents") or [])
    return None


def test_a_confirmed_delete_removes_the_agent(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.press("greeter", "delete")
    assert dashboard.asked_to_confirm() == (
        "Delete agent?",
        "Are you sure you want to delete greeter? This cannot be undone.",
    )

    dashboard.confirm()

    dashboard.wait_for_no_card("greeter")
    waiting.until(lambda: app.rest.agent("greeter") is None, what="the server to have no greeter")
    waiting.until(
        lambda: "greeter" not in dashboard.show("chat")._targets(),
        what="greeter to be gone from who can be talked to",
    )


def test_an_agent_on_the_node_is_deleted_from_its_card_too(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.press("asker", "delete")
    dashboard.asked_to_confirm()
    dashboard.confirm()

    dashboard.wait_for_no_card("asker")
    waiting.until(
        # Not the node's whole list: other journeys leave agents of their own there.
        lambda: "counter" in (running := _on_the_node(app) or set()) and "asker" not in running,
        what="the node to be running 'counter' and no longer 'asker'",
        interval=NODE_POLL_S,
    )


def test_main_was_told_and_says_nothing_of_it_unasked(dashboard: browser.Dashboard) -> None:
    dashboard.say("please say hello", to="main")

    dashboard.expect("main", "Hello from the scripted model.")


def test_wiping_everything_asks_first_and_says_when_it_is_done(
    dashboard: browser.Dashboard,
) -> None:
    asked = dashboard.clear("Wipe everything")

    assert asked == "Confirm wipe everything?"
    waiting.until(
        lambda: "Reset: Wipe everything cleared" in dashboard.notices(),
        what="the page to say everything was cleared",
    )


def test_what_is_left_is_what_a_fresh_install_runs(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    # What this server ran when it had just started, before any journey.
    fresh = app.started_with
    waiting.becomes_and_stays(
        lambda: {str(a.get("name")) for a in app.rest.agents()} == fresh,
        what=f"only the agents a fresh install runs to be left: {sorted(fresh)}",
    )
    dashboard.show("overview")
    waiting.until(
        lambda: dashboard.card_names() == fresh,
        what="only their cards to be left",
    )


def test_nothing_that_was_said_or_spent_is_kept(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    assert dashboard.all_said() == []
    assert dashboard.read_thread_of("main").sent() == []
    assert app.rest.cost()["spend_usd"] == 0.0

    # And it is gone from the server, not only from this page.
    dashboard.reload()
    assert dashboard.all_said() == []


def test_the_node_stays_and_runs_nothing_of_the_install(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    # A wipe clears what was built on this install. The node is the machine it
    # runs on: it stays deployed, connected and listed, with none of the agents
    # it ran and none of their state.
    def kept_and_empty() -> bool:
        return node.running() and _on_the_node(app) == set()

    waiting.becomes_and_stays(
        kept_and_empty,
        what="the node to be running, listed, and running no agent",
        timeout=90.0,
        window=REPORTED_WITHIN_S,
        interval=NODE_POLL_S,
    )
    assert node.run_on(f"test -e {COUNTER_STATE}").returncode != 0
    waiting.until(
        lambda: dashboard.node_rows().get(NODE_NAME) == [],
        what="the nodes panel to list the node with no agents",
    )
