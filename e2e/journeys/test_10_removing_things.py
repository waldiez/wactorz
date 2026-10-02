"""Agents deleted one by one, and then everything wiped: what is left is a fresh install.

Last, because nothing after it has anything to start from.
"""

from harness import backend, browser, node, waiting
from harness.probe import NODE_POLL_S
from harness.run import NODE_NAME

#: What a server starts by itself, and so what a wipe leaves running.
A_FRESH_INSTALL_RUNS = {
    "main",
    "monitor",
    "installer",
    "catalog",
    "home-assistant-agent",
    "home-assistant-map-agent",
    "home-assistant-state-bridge",
}


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
        lambda: _on_the_node(app) == {"counter"},
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
    waiting.becomes_and_stays(
        lambda: {str(a.get("name")) for a in app.rest.agents()} == A_FRESH_INSTALL_RUNS,
        what="only the server's own agents to be left",
    )
    dashboard.show("overview")
    waiting.until(
        lambda: dashboard.card_names() == A_FRESH_INSTALL_RUNS,
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


def test_the_node_was_told_to_stop(app: backend.Backend) -> None:
    # A wipe stops every node that was running something for this install. The
    # machine stays deployed, and its node stays stopped until it is deployed
    # again.
    waiting.until(lambda: not node.running(), what="the node's process to stop", timeout=60.0)
    waiting.until(
        lambda: _on_the_node(app) is None,
        what="the server to list no node",
        timeout=90.0,
        interval=NODE_POLL_S,
    )
