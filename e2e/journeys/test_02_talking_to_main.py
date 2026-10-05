"""A message goes to the server's own agent, and its answer comes back to the page."""

from harness import backend, browser, waiting


def test_main_answers_what_it_was_asked(dashboard: browser.Dashboard) -> None:
    dashboard.say("please say hello", to="main")

    dashboard.expect("main", "Hello from the scripted model.")


def test_answering_was_paid_for(dashboard: browser.Dashboard, app: backend.Backend) -> None:
    # The total that a change once froze while every other test stayed green:
    # read before and after, so it is the movement that is asserted.
    before = app.rest.capture("main", "cost_usd")["cost_usd"]

    dashboard.say("say hello again", to="main")
    dashboard.expect("main", "Hello from the scripted model.")

    waiting.until(
        lambda: app.rest.capture("main", "cost_usd")["cost_usd"] > before,
        what=f"main's spend to move above {before}",
    )


def test_the_conversation_is_still_there_after_a_reload(dashboard: browser.Dashboard) -> None:
    before = dashboard.said()

    dashboard.reload().show("chat")

    waiting.until(lambda: dashboard.said() == before, what="the thread to show what it showed")
