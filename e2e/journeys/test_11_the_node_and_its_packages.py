"""Checking on the node, a command for it, and packages nobody has allowed.

Main's model reaches a node only by named operations that read; a command for
the node is the operator's to type, as `/nodes run`. A package an agent asks
for that is not on the list of packages installed without asking is put to the
operator as a notice that stays until it is answered, and the installer, which
cannot be messaged, opens in the chat while it asks.

It starts from what journey 10 left: the node deployed, running nothing.
"""

from harness import backend, browser, guard, node, waiting
from harness.probe import NODE_POLL_S
from harness.run import NODE_NAME
from model import ON_THE_NODE_ALREADY, UNLISTED_PACKAGE

#: How long the installer's question may take to reach the page: the spawn, a
#: delegation to the installer, and a frame over the broker.
ASKED_WITHIN_S = 60.0


def _on_the_node(app: backend.Backend) -> set[str]:
    """The agents the node says it is running."""
    for listed in app.rest.nodes():
        if (listed.get("node") or listed.get("name")) == NODE_NAME:
            return set(listed.get("agents") or [])
    return set()


def _asked(dashboard: browser.Dashboard) -> str:
    return waiting.until(
        lambda: dashboard.question_from("installer"),
        what="the installer's question on screen",
        timeout=ASKED_WITHIN_S,
    )


def test_the_model_checks_the_nodes_disk(dashboard: browser.Dashboard) -> None:
    dashboard.say("how full is the disk on the node", to="main")

    # `df -h`, run on the node by name: its header, and the root filesystem.
    dashboard.expect_like(
        "main",
        r"Checking\.\n+"
        r"✅ installer completed: output=Filesystem\s+Size\s+Used\s+Avail.*\s/\n.*",
    )


def test_a_command_the_model_writes_is_refused(dashboard: browser.Dashboard) -> None:
    dashboard.say("please reboot the node", to="main")

    dashboard.expect_like(
        "main",
        r"Rebooting it\.\n+"
        r"\[installer: 'node_run' is not something the assistant may ask for\..*/nodes run.*\]",
    )
    assert node.running()


def test_a_command_the_operator_types_runs_there(dashboard: browser.Dashboard) -> None:
    dashboard.say(f"/nodes run {NODE_NAME} uptime", to="main")

    dashboard.expect_like("main", rf"{NODE_NAME} \$ uptime\s+\[exit 0\]\n.*load average.*")


def test_an_unlisted_package_waits_for_a_yes_and_a_no_starts_nothing(
    dashboard: browser.Dashboard, app: backend.Backend, unexpected: guard.Guard
) -> None:
    # Refused on purpose: main says why it did not start the agent.
    unexpected.allow(r"Not spawning 'prober'")
    dashboard.say("please start an agent that needs an unknown package", to="main")

    asked = _asked(dashboard)
    assert UNLISTED_PACKAGE in asked
    assert "'prober'" in asked
    dashboard.assert_accessible("a question on a notice")
    # The installer cannot be messaged, and opens while it asks: its thread
    # holds the same question, with the same answers.
    assert dashboard.thread_opens("installer")
    dashboard.open_from_list("installer")
    assert dashboard.options_in_thread() == ["Install", "Refuse"]
    dashboard.assert_accessible("a question in the thread")

    dashboard.answer_notice("installer", "Refuse")

    waiting.until(
        lambda: "Not spawning 'prober'" in app.console(),
        what="main to say it did not start the agent",
    )
    waiting.until(
        lambda: not dashboard.thread_opens("installer"),
        what="the installer's row to lock again once answered",
    )
    assert app.rest.agent("prober") is None
    dashboard.read_thread_of("main")
    dashboard.expect_like("main", r"Starting it\..*")


def test_a_package_allowed_goes_to_the_node_and_stays_allowed(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.say("please start a sampler on the node", to="main")

    # The node installs what the spawn lists, so nothing is sent before the yes.
    assert ON_THE_NODE_ALREADY in _asked(dashboard)
    assert "sampler" not in _on_the_node(app)
    dashboard.answer_notice("installer", "Install")

    waiting.until(
        lambda: "sampler" in _on_the_node(app),
        what="the node to be running 'sampler'",
        interval=NODE_POLL_S,
    )
    dashboard.expect_like(
        "main",
        r"Starting it there\.\n+"
        rf"ℹ️ Spawned 'sampler' on node '{NODE_NAME}'",
    )

    dashboard.say("/packages", to="main")
    dashboard.expect_like(
        "main", rf"Packages installed without asking:\n  allowed by you: {ON_THE_NODE_ALREADY}\n.*"
    )


def test_forgetting_it_takes_it_off_the_list(dashboard: browser.Dashboard) -> None:
    dashboard.say(f"/packages forget {ON_THE_NODE_ALREADY}", to="main")

    dashboard.expect(
        "main",
        f"Forgotten: {ON_THE_NODE_ALREADY}. Agents already saved keep what they were placed "
        "with; this asks again for agents placed from now on.\nAllowed by you: none.",
    )
