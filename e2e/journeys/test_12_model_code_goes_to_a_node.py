"""Code the model writes goes to a node, once one is set for it.

Two agents the model wrote run on main, as they did before any node was set for
model code: one that counts, and one that reads the history without awaiting
it. The server is then restarted the way an operator would set the node, with
``WACTORZ_MODEL_CODE_NODE`` naming it. The first time the node reports, the
counter moves there and the reader stays, since only main answers its call, and
the chat says which and why. An agent the model writes after that, naming no
node, starts on the node.

It starts from what journey 11 left: the node deployed.
"""

from harness import backend, browser, guard, waiting
from harness.probe import NODE_POLL_S
from harness.run import NODE_NAME

#: Long enough for the node to report after the restart, and the move after it:
#: a heartbeat or two, and a migration's round trip.
MOVED_WITHIN_S = 120.0


def _on_the_node(app: backend.Backend) -> set[str]:
    """The agents the node says it is running."""
    for listed in app.rest.nodes():
        if (listed.get("node") or listed.get("name")) == NODE_NAME:
            return set(listed.get("agents") or [])
    return set()


def _on_the_server(app: backend.Backend) -> set[str]:
    return {str(agent.get("name")) for agent in app.rest.agents()}


def test_the_models_agents_start_on_main_while_no_node_is_set(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    for ask, name in (
        ("please start a tallier here", "tallier"),
        ("please start a trend reader here", "trend-reader"),
    ):
        dashboard.say(ask, to="main")
        dashboard.expect_like(
            "main",
            r"Starting it\.\n+"
            rf"ℹ️ Spawned '{name}' — will auto-restore on restart",
        )

    waiting.until(
        lambda: {"tallier", "trend-reader"} <= _on_the_server(app),
        what="both agents running on the server",
        interval=NODE_POLL_S,
    )
    assert not {"tallier", "trend-reader"} & _on_the_node(app)


def test_setting_the_node_moves_the_one_that_can_run_there(
    dashboard: browser.Dashboard, app: backend.Backend, unexpected: guard.Guard
) -> None:
    # While there is no server the page asks for it and is refused, which the
    # browser reports on its console.
    unexpected.allow_on_the_page(r"ERR_CONNECTION_REFUSED")

    app.restart(extra={"WACTORZ_MODEL_CODE_NODE": NODE_NAME})

    waiting.until(
        lambda: "tallier" in _on_the_node(app),
        what="the tallier running on the node",
        timeout=MOVED_WITHIN_S,
        interval=NODE_POLL_S,
    )
    assert "trend-reader" in _on_the_server(app)
    assert "trend-reader" not in _on_the_node(app)
    waiting.until(
        lambda: not dashboard.at_sign_in and dashboard.connection() == "● live",
        what="the page to be fed again",
        timeout=60.0,
    )


def test_the_chat_says_what_moved_and_what_stayed(dashboard: browser.Dashboard) -> None:
    dashboard.say("say hello", to="main")

    dashboard.expect_like(
        "main",
        rf"(?s).*Moved to node '{NODE_NAME}', where code the model writes runs: 'tallier'\..*"
        r"Kept 'trend-reader' on main: it calls query_ts\(\) without await.*"
        r"Hello from the scripted model\.",
    )


def test_the_moved_agent_answers_from_the_node(dashboard: browser.Dashboard) -> None:
    dashboard.say("one", to="tallier")

    dashboard.expect("tallier", "counted 1")


def test_a_new_agent_the_model_writes_goes_to_the_node(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.say("please start a second tallier", to="main")

    dashboard.expect_like(
        "main",
        r"Starting it\.\n+"
        rf"ℹ️ Spawned 'tallier-two' on node '{NODE_NAME}'",
    )
    waiting.until(
        lambda: "tallier-two" in _on_the_node(app),
        what="the second tallier running on the node",
        interval=NODE_POLL_S,
    )
    assert "tallier-two" not in _on_the_server(app)
