"""The server is stopped and started again: everything it was running comes back.

The same state directory, the same broker, the same node, and the same browser
tab left open across it.
"""

from harness import backend, browser, guard, waiting
from harness.browser import Said
from harness.run import NODE_NAME


def _running(app: backend.Backend) -> set[str]:
    return {str(a.get("name")) for a in app.rest.agents() if a.get("state") == "running"}


def test_what_was_running_is_running_again(app: backend.Backend, unexpected: guard.Guard) -> None:
    # While there is no server the page asks for it and is refused, which the
    # browser reports on its console.
    unexpected.allow_on_the_page(r"ERR_CONNECTION_REFUSED")
    before = _running(app)
    spent = app.rest.capture("main", "cost_usd")["cost_usd"]
    assert {"main", "greeter", "timeseries-collector"} <= before

    app.restart()

    waiting.becomes_and_stays(
        lambda: _running(app) == before, what=f"the same agents to be running: {sorted(before)}"
    )
    # What had been spent is not forgotten with the process that spent it.
    assert app.rest.capture("main", "cost_usd")["cost_usd"] >= spent


def test_the_open_tab_carries_on_without_signing_in_again(dashboard: browser.Dashboard) -> None:
    waiting.until(
        lambda: not dashboard.at_sign_in and dashboard.connection() == "● live",
        what="the page to be fed again, still signed in",
        timeout=60.0,
    )
    dashboard.show("overview")
    waiting.until(
        lambda: {"greeter", "counter", "asker"} <= dashboard.card_names(),
        what="cards for the agents here and on the node",
    )
    assert NODE_NAME in dashboard.node_names()


def test_the_conversations_are_what_they_were(dashboard: browser.Dashboard) -> None:
    # Read again from the server, which is a new process: a reload drops what
    # the page was holding.
    before: dict[str, list[Said]] = {
        agent: dashboard.read_thread_of(agent).said() for agent in ("main", "greeter", "counter")
    }

    dashboard.reload()

    for agent, said in before.items():
        waiting.until(
            lambda agent=agent, said=said: dashboard.read_thread_of(agent).said() == said,
            what=f"the thread with {agent} to show what it showed",
        )


def test_an_agent_here_answers(dashboard: browser.Dashboard) -> None:
    dashboard.say("good morning once more", to="greeter")

    dashboard.expect("greeter", "Good morning to you too.")


def test_the_agent_on_the_node_went_on_counting(dashboard: browser.Dashboard) -> None:
    # The node was never stopped. The server finds it again, and its agent has
    # the count it had.
    dashboard.say("five", to="counter")

    dashboard.expect("counter", "counted 5", timeout=120.0)
