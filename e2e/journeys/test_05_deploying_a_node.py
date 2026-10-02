"""`/deploy` from the chat puts a node on another machine, and the node reports in.

The machine is a container with Python and an SSH server. Everything else on it
is what the deploy put there: a wheel built from this checkout, a virtualenv,
the node's settings and the process itself.
"""

from harness import backend, broker, browser, node, waiting
from harness.run import NODE_NAME, Run

import wactorz


def test_a_deploy_from_the_chat_ends_with_the_node_online(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.say(f"/deploy {NODE_NAME}", to="main")

    # One message, which main adds to as the deploy goes: what it is doing, and
    # then that the node has been heard from.
    dashboard.expect_like(
        "main",
        rf"\[deploy\] Deploying to {node.USER}@127\.0\.0\.1 as node '{NODE_NAME}'\.\.\.\n"
        r"\(This may take [^)]*\)"
        rf"\[OK\] Node '{NODE_NAME}' is live and its first heartbeat has arrived\.\n\n"
        r"Spawn agents on it:\n.*",
        timeout=240.0,
    )
    waiting.until(
        lambda: NODE_NAME in app.rest.node_names(),
        what=f"the server to list node {NODE_NAME!r}",
    )


def test_the_dashboard_shows_the_node(dashboard: browser.Dashboard) -> None:
    dashboard.show("overview")

    waiting.until(
        lambda: NODE_NAME in dashboard.node_names(),
        what=f"node {NODE_NAME!r} on the overview",
    )


def test_the_node_runs_what_the_server_runs(app: backend.Backend) -> None:
    listed = next(n for n in app.rest.nodes() if (n.get("node") or n.get("name")) == NODE_NAME)

    assert node.running()
    # The suite runs from the checkout the server runs from, and the node was
    # sent a wheel of it.
    assert listed["version"] == wactorz.__version__
    assert listed["runtime"] == "node"


def test_the_node_was_given_settings_of_its_own(run: Run) -> None:
    settings = node.settings()

    # The broker as the node reaches it, over TLS, verified with the CA it was sent.
    assert settings["WACTORZ_BROKER"] == broker.NAME_FOR_NODES
    assert settings["WACTORZ_PORT"] == "8883"
    assert settings["MQTT_TLS"] == "1"
    assert node.run_on(f"test -s {settings['MQTT_TLS_CA']}").returncode == 0
    # An account of its own, not the server's: a node that is stolen holds
    # nothing that opens the rest of the broker.
    assert settings["MQTT_USERNAME"] == NODE_NAME
    assert settings["MQTT_PASSWORD"] != run.broker_password
    assert settings["WACTORZ_NODE_KEY"]


def test_the_deploy_left_nothing_half_done_behind() -> None:
    left = node.run_on(f"ls -A {node.HOME}/wactorz").stdout.split()

    assert not [name for name in left if name.endswith(".incoming")]
