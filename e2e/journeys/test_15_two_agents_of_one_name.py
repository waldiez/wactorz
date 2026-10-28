"""Two agents of one name: one on the node, a copy on main, each reached by its address.

The page calls each ``name@node`` once the name is shared, and talks to the one
picked. A message to the bare name is answered with both addresses as buttons,
and the one pressed gets the message. An address naming the bare name opens
neither conversation, and offers both.

It starts from what journey 14 left: the node deployed.
"""

from harness import backend, browser, waiting
from harness.probe import NODE_POLL_S
from harness.run import NODE_NAME

ON_MAIN = "helper@main"
ON_NODE = f"helper@{NODE_NAME}"

#: What the helper says to "good morning", wherever it runs.
GREETING = "Good morning to you too."


def _on_the_node(app: backend.Backend) -> set[str]:
    """The agents the node says it is running."""
    for listed in app.rest.nodes():
        if (listed.get("node") or listed.get("name")) == NODE_NAME:
            return set(listed.get("agents") or [])
    return set()


def _on_the_server(app: backend.Backend) -> set[str]:
    return {str(agent.get("name")) for agent in app.rest.agents()}


def test_a_copy_on_main_is_told_apart_from_the_one_on_the_node(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.say("please start a helper on the node", to="main")
    dashboard.expect_like("main", r"(?s).*Starting it there\..*")
    waiting.until(
        lambda: "helper" in _on_the_node(app),
        what="the node to be running 'helper'",
        interval=NODE_POLL_S,
    )

    dashboard.say("/agents copy helper main", to="main")
    dashboard.expect_like("main", r"(?s).*'helper'.*")
    waiting.until(lambda: "helper" in _on_the_server(app), what="the server to be running 'helper'")

    # Each answers in its own thread, which the page names by its address.
    for address in (ON_NODE, ON_MAIN):
        dashboard.say("good morning", to=address)
        dashboard.expect("helper", GREETING)
        assert dashboard.talking_to == address
    dashboard.assert_accessible("two agents of one name, each by its address")


def test_the_bare_name_is_answered_with_both_addresses_to_pick_from(
    dashboard: browser.Dashboard,
) -> None:
    dashboard.say("@helper good morning", to="main")

    dashboard.expect_like("main", r"(?s)More than one agent is called 'helper'.*")
    assert set(dashboard.options_in_thread()) == {f"@{ON_MAIN}", f"@{ON_NODE}"}
    dashboard.assert_accessible("the addresses a name fits, offered as buttons")

    dashboard.press_in_thread(f"@{ON_NODE}")

    # The message goes again, to the one picked, and its conversation opens.
    waiting.until(
        lambda: dashboard.talking_to == ON_NODE, what="the picked agent's thread on screen"
    )
    dashboard.expect("helper", GREETING)
    dashboard.read_thread_of("main")
    assert dashboard.options_in_thread() == []


def test_an_address_naming_the_bare_name_opens_neither(dashboard: browser.Dashboard) -> None:
    dashboard.open_address("#/chat/helper")

    dashboard.follow_link(f"@{ON_MAIN}")

    waiting.until(
        lambda: dashboard.address == f"#/chat/{ON_MAIN}",
        what="the address to name the one picked",
    )
    assert dashboard.talking_to == ON_MAIN
    dashboard.show("overview")
