"""Agents on the node: started there, moved home and out again, and asking the model.

The agent that is moved counts what it is sent and keeps the count, so each
answer says whether its memory came with it.
"""

from harness import backend, browser, waiting
from harness.run import NODE_NAME


def _on_the_node(app: backend.Backend) -> set[str]:
    """The agents the node says it is running."""
    for listed in app.rest.nodes():
        if (listed.get("node") or listed.get("name")) == NODE_NAME:
            return set(listed.get("agents") or [])
    return set()


def _on_the_server(app: backend.Backend) -> set[str]:
    return {str(agent.get("name")) for agent in app.rest.agents()}


def test_main_starts_an_agent_on_the_node(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.say("please start a counter on the node", to="main")

    dashboard.expect_like("main", r"Starting it there\.\n<spawn>\n\{.*\}\n</spawn>")
    waiting.until(lambda: "counter" in _on_the_node(app), what="the node to be running 'counter'")
    assert "counter" not in _on_the_server(app)


def test_it_answers_from_there(dashboard: browser.Dashboard) -> None:
    dashboard.say("one", to="counter")

    dashboard.expect("counter", "counted 1")


def test_moved_home_it_remembers(dashboard: browser.Dashboard, app: backend.Backend) -> None:
    dashboard.say("/migrate counter local", to="main")

    dashboard.expect(
        "main",
        f"[OK] Migration of 'counter' from '{NODE_NAME}' → local initiated "
        "(waiting for state from remote node).",
    )
    waiting.until(lambda: "counter" in _on_the_server(app), what="the server to be running it")
    waiting.until(lambda: "counter" not in _on_the_node(app), what="the node to have let it go")

    dashboard.say("two", to="counter")
    dashboard.expect("counter", "counted 2")


def test_moved_out_again_it_still_remembers(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.say(f"/migrate counter {NODE_NAME}", to="main")

    # Main tells of what finished since it last spoke before it answers: here,
    # that the move home succeeded. It says so twice, in two wordings.
    dashboard.expect_like(
        "main",
        rf"✅ System: Migration of 'counter' from '{NODE_NAME}' → local succeeded\.\s+"
        r"✅ System: Migration of 'counter' to 'local' succeeded\.\s+"
        rf"\[OK\] Migrating 'counter' from 'local' → '{NODE_NAME}' "
        r"\(waiting for it to confirm it started\)\.",
    )
    waiting.until(lambda: "counter" in _on_the_node(app), what="the node to be running it again")
    waiting.until(lambda: "counter" not in _on_the_server(app), what="the server to have let it go")

    dashboard.say("three", to="counter")
    dashboard.expect("counter", "counted 3")


def test_an_agent_on_the_node_asks_the_model_through_the_server(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    # The node holds no key for a model: its agent's question goes to the
    # server, signed, and the answer comes back to it.
    dashboard.say("please start an asker on the node", to="main")
    dashboard.expect_like(
        "main",
        rf"✅ System: Migration of 'counter' from 'local' → '{NODE_NAME}' complete\.\s+"
        r"Starting it there\.\n<spawn>\n\{.*\}\n</spawn>",
    )
    waiting.until(lambda: "asker" in _on_the_node(app), what="the node to be running 'asker'")

    dashboard.say("how is the tide today?", to="asker")

    dashboard.expect("asker", "the model said: The tide is in.")
