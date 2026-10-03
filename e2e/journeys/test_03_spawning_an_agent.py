"""Asked for an agent, main starts one; it gets a card, and it answers for itself."""

from harness import backend, browser, waiting


def test_main_starts_the_agent_it_was_asked_for(
    dashboard: browser.Dashboard, app: backend.Backend
) -> None:
    dashboard.say("please start a greeter", to="main")

    # What the model wrote, the block that asks for the agent included, and
    # then main's own account of what it did with it.
    dashboard.expect_like(
        "main",
        r"I'll set that up\.\n<spawn>\n\{.*\}\n</spawn>\n\n"
        r"ℹ️ Spawned 'greeter' — will auto-restore on restart",
    )
    dashboard.wait_for_card("greeter")
    waiting.becomes_and_stays(
        lambda: app.rest.state_of("greeter") == "running",
        what="greeter to be running, and to stay so",
    )


def test_the_new_agent_answers_for_itself(dashboard: browser.Dashboard) -> None:
    dashboard.say("good morning", to="greeter")

    dashboard.expect("greeter", "Good morning to you too.")


def test_main_knows_nothing_of_that_conversation(dashboard: browser.Dashboard) -> None:
    # Each agent has a thread of its own: what was said to one is not shown as
    # said to another.
    assert all(said.sender == "greeter" for said in dashboard.read_thread_of("greeter").said())
    assert "good morning" not in dashboard.read_thread_of("main").sent()
