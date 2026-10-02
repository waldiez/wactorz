"""The broker stops and comes back: the page says so, and so does an agent it cannot reach."""

from harness import backend, broker, browser, guard, waiting
from harness.run import NODE_NAME, Run

LIVE = "● live"


def test_the_page_says_when_the_broker_is_gone(dashboard: browser.Dashboard, run: Run) -> None:
    assert dashboard.connection() == LIVE

    broker.stop(run)

    waiting.until(lambda: dashboard.connection() != LIVE, what="the header to stop saying live")
    assert dashboard.connection() == "◎ Demo fallback"


def test_main_still_answers_without_it(dashboard: browser.Dashboard) -> None:
    # Main and the page talk over the server's own socket, not over the broker.
    dashboard.say("please say hello", to="main")

    dashboard.expect("main", "Hello from the scripted model.")


def test_an_agent_on_the_node_cannot_be_reached_and_the_user_is_told(
    dashboard: browser.Dashboard, unexpected: guard.Guard
) -> None:
    # The agent is on the node, which only the broker reaches. The answer is a
    # refusal that says so, at once, and not a wait that ends in nothing.
    # The server logs the failure it met, which is this journey's doing.
    unexpected.allow(r"\[io-gateway\] Remote @counter routing failed")
    dashboard.say("four", to="counter")

    dashboard.expect_like("counter", rf"\[error\] Could not reach @counter on {NODE_NAME}: .+")


def test_it_comes_back_and_the_page_says_so(
    dashboard: browser.Dashboard, app: backend.Backend, run: Run
) -> None:
    broker.start(run)

    waiting.until(lambda: dashboard.connection() == LIVE, what="the header to say live again")
    waiting.until(
        lambda: app.rest.raw("GET", "/ready").status == 200, what="the server to be ready again"
    )


def test_the_node_is_reached_again_and_took_nothing_meanwhile(
    dashboard: browser.Dashboard,
) -> None:
    # The message that was refused was told so, and is not delivered late: the
    # count goes on from where it was.
    dashboard.say("four", to="counter")

    dashboard.expect("counter", "counted 4", timeout=120.0)
