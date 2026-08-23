"""A message reaches the agent, and its reply reaches the page.

Asserted in the browser rather than through the API, because the API answers
`sent` and then says nothing more - the reply travels over the socket, and the
whole seam under test is the one between that socket and the thread on screen.

What is asserted is that an answer arrived and that spend moved, never what the
answer said. Wording is the one thing that cannot hold under both the fake
provider and a real model, and an assertion that only holds under one of them
turns the demo profile into a second, weaker suite.
"""

from harness import backend, browser, waiting


def test_a_reply_reaches_the_thread(dashboard: browser.Dashboard) -> None:
    dashboard.show("chat")
    before = len(dashboard.replies())

    dashboard.chat("hello", to="main", dwell="beat")
    reply = dashboard.wait_for_reply(after=before, dwell="readable")

    assert reply.strip(), "an agent message appeared in the thread with nothing in it"


def test_the_message_that_was_sent_is_shown_as_sent(dashboard: browser.Dashboard) -> None:
    """The user's own turn is in the thread.

    Trivial to break and easy to miss: a composer that clears on send and never
    renders the turn looks like it worked until the reply lands somewhere with no
    question above it.
    """
    dashboard.show("chat")
    dashboard.chat("did that arrive", to="main", dwell="beat")
    waiting.until(
        lambda: any("did that arrive" in m for m in dashboard.messages()),
        what="the sent message to appear in the thread",
        timeout=30.0,
        interval=0.25,
    )


def test_answering_costs_something(dashboard: browser.Dashboard, app: backend.Backend) -> None:
    """Spend moves while an agent answers.

    The counter that a regression once froze while every unit test stayed green,
    which is why this asserts movement against a captured value rather than
    against a number. The fake provider reports a cost that is tiny and
    deliberately not zero, so this holds under both providers.
    """
    before = app.rest.capture("main", "cost_usd")
    dashboard.show("chat")
    replies = len(dashboard.replies())
    dashboard.chat("hello again", to="main")
    dashboard.wait_for_reply(after=replies)

    waiting.until(
        lambda: app.rest.capture("main", "cost_usd")["cost_usd"] > before["cost_usd"],
        what=f"spend to move above {before['cost_usd']}",
        timeout=60.0,
        interval=0.5,
    )
