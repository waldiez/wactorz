"""Agents on the node: started there, moved home and out again, and asking the model.

The agent that is moved counts what it is sent and keeps the count, so each
answer says whether its memory came with it. Moved home, it must leave nothing
on the node: not its state file, and not a place in the node's desired state,
which the node reads again whenever it reconnects to the broker.
"""

import time

from harness import backend, broker, browser, node, waiting
from harness.probe import NODE_POLL_S
from harness.run import NODE_NAME, Run

LIVE = "● live"

#: Where the node keeps the moved agent's state.
COUNTER_STATE = f"{node.HOME}/wactorz/state/counter_state.json"

#: Long enough for a node that started an agent to report it: a heartbeat or two.
REPORTED_WITHIN_S = 25.0

#: The notice main gives of one move home, once the agent is running here.
HOME = rf"✅ System: Migration of 'counter' from '{NODE_NAME}' → local succeeded\."


def _on_the_node(app: backend.Backend) -> set[str]:
    """The agents the node says it is running."""
    for listed in app.rest.nodes():
        if (listed.get("node") or listed.get("name")) == NODE_NAME:
            return set(listed.get("agents") or [])
    return set()


def _last_seen(app: backend.Backend) -> float:
    """When main last heard from the node, as a Unix time; 0 when it has not."""
    for listed in app.rest.nodes():
        if (listed.get("node") or listed.get("name")) == NODE_NAME:
            return float(listed.get("last_seen") or 0)
    return 0.0


def _on_the_server(app: backend.Backend) -> set[str]:
    return {str(agent.get("name")) for agent in app.rest.agents()}


def test_main_starts_an_agent_on_the_node(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.say("please start a counter on the node", to="main")

    dashboard.expect_like("main", r"Starting it there\.\n<spawn>\n\{.*\}\n</spawn>")
    waiting.until(
        lambda: "counter" in _on_the_node(app),
        what="the node to be running 'counter'",
        interval=NODE_POLL_S,
    )
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
    waiting.until(
        lambda: "counter" not in _on_the_node(app),
        what="the node to have let it go",
        interval=NODE_POLL_S,
    )

    dashboard.say("two", to="counter")
    dashboard.expect("counter", "counted 2")


def test_the_node_keeps_nothing_of_it(dashboard: browser.Dashboard) -> None:
    # It kept the file while the move could still fail; once the agent runs
    # here, the delete that follows takes it.
    waiting.until(
        lambda: node.run_on(f"test -e {COUNTER_STATE}").returncode != 0,
        what="the node to delete the moved agent's state file",
    )


def test_a_node_that_reconnects_does_not_take_it_back(
    dashboard: browser.Dashboard, app: backend.Backend, run: Run
) -> None:
    # The node reads its retained desired state again when it reconnects. One
    # that still listed the agent started it there again, beside the copy here.
    broker.stop(run)
    back_at = time.time()
    broker.start(run)
    waiting.until(lambda: dashboard.connection() == LIVE, what="the header to say live again")
    waiting.until(
        lambda: app.rest.raw("GET", "/ready").status == 200, what="the server to be ready again"
    )
    waiting.until(
        lambda: _last_seen(app) > back_at,
        what="a heartbeat from the node after the broker came back",
        interval=NODE_POLL_S,
    )

    waiting.holds_for(
        lambda: "counter" not in _on_the_node(app),
        what="the node not running the agent moved home",
        window=REPORTED_WITHIN_S,
        interval=NODE_POLL_S,
    )
    assert "counter" in _on_the_server(app)


def test_moved_out_again_it_still_remembers(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.say(f"/migrate counter {NODE_NAME}", to="main")

    # Main tells of what finished since it last spoke before it answers: here,
    # that the move home succeeded, once.
    dashboard.expect_like(
        "main",
        rf"{HOME}\s+"
        rf"\[OK\] Migrating 'counter' from 'local' → '{NODE_NAME}' "
        r"\(waiting for it to confirm it started\)\.",
    )
    waiting.until(
        lambda: "counter" in _on_the_node(app),
        what="the node to be running it again",
        interval=NODE_POLL_S,
    )
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
    waiting.until(
        lambda: "asker" in _on_the_node(app),
        what="the node to be running 'asker'",
        interval=NODE_POLL_S,
    )

    dashboard.say("how is the tide today?", to="asker")

    dashboard.expect("asker", "the model said: The tide is in.")


def test_an_agent_on_the_node_that_ends_itself_leaves_the_dashboard_once(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    # The same ending as on the server, from a node: the node withdraws the
    # agent's manifest and main forgets it. Its card must go once and stay gone,
    # through the node's heartbeats that no longer list it.
    dashboard.say("please start a finisher on the node", to="main")
    dashboard.expect_like("main", r"Starting it there\.\n<spawn>\n\{.*\}\n</spawn>")
    dashboard.watch_card("far-finisher")

    waiting.until(
        lambda: "removed" in dashboard.card_comings_and_goings("far-finisher"),
        what="the far finisher's card to go once it has ended itself",
        timeout=90,
    )
    waiting.holds_for(
        lambda: dashboard.card_comings_and_goings("far-finisher")[-1] == "removed",
        what="the far finisher's card staying gone",
        window=35,
        interval=1,
    )
    seen = dashboard.card_comings_and_goings("far-finisher")
    assert seen.count("removed") == 1, seen
    assert "far-finisher" not in _on_the_node(app)
