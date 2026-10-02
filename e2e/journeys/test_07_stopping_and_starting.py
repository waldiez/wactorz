"""An agent stopped from its card stays stopped, and is started again from there."""

from harness import backend, browser, waiting


def test_the_servers_own_agents_cannot_be_stopped_or_deleted(
    dashboard: browser.Dashboard,
) -> None:
    # Stopping main removes the way back, and it cannot be recreated: its card
    # offers neither.
    assert dashboard.card_actions("main") == ["Chat"]


def test_an_agent_is_stopped_from_its_card(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.press("greeter", "stop")

    waiting.until(
        lambda: dashboard.card_state("greeter") == "stopped", what="the card to say stopped"
    )
    waiting.holds_for(
        lambda: app.rest.state_of("greeter") == "stopped",
        what="greeter staying stopped, with nothing starting it again",
    )
    # Starting it is the way back, and it is offered; talking to it is not.
    assert dashboard.card_actions("greeter") == ["Start", "Delete"]


def test_it_is_started_again_and_answers(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.press("greeter", "start")

    waiting.until(
        lambda: dashboard.card_state("greeter") == "running", what="the card to say running"
    )
    waiting.becomes_and_stays(
        lambda: app.rest.state_of("greeter") == "running", what="greeter to run, and stay so"
    )
    dashboard.say("good morning again", to="greeter")
    dashboard.expect("greeter", "Good morning to you too.")


def test_deleting_asks_first_and_cancel_keeps_the_agent(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.press("greeter", "delete")
    asked = dashboard.asked_to_confirm()

    dashboard.cancel()

    assert asked == (
        "Delete agent?",
        "Are you sure you want to delete greeter? This cannot be undone.",
    )
    waiting.holds_for(
        lambda: app.rest.state_of("greeter") == "running", what="greeter still running"
    )
