"""A question an agent asked while the page was away is there when the page opens.

The page hears of a question live, while it is open. The server keeps the book
of questions every agent publishes, so a page opened later shows each one still
waiting: as a notice with its answers, and in the agent's thread with its
buttons. Answering it reaches the agent, and closes both.

It starts from what journey 13 left: the node deployed, and model code going to
it, so the agent that asks runs on the node.
"""

import re

from harness import browser, waiting
from harness.run import NODE_NAME
from model import QUESTION

#: How long the question may take to reach the page: the spawn on the node, the
#: agent's first round, and its book over the broker.
ASKED_WITHIN_S = 60.0

#: The question as its thread shows it: the words, then the buttons' labels.
AS_SHOWN = rf"{re.escape(QUESTION)}\s*Yes\s*No"


def _asked(dashboard: browser.Dashboard, what: str) -> str:
    return waiting.until(
        lambda: dashboard.question_from("asker"), what=what, timeout=ASKED_WITHIN_S
    )


def test_a_question_waiting_when_the_page_opens_is_shown_with_its_answers(
    dashboard: browser.Dashboard,
) -> None:
    dashboard.say("please start an asker", to="main")
    dashboard.expect_like("main", rf"Starting it\.\n+ℹ️ Spawned 'asker' on node '{NODE_NAME}'")
    assert QUESTION in _asked(dashboard, "the asker's question on screen")

    # Opened again while the question waits: the server's ledger has it.
    dashboard.reload()

    assert QUESTION in _asked(dashboard, "the question shown again once the page is back")
    dashboard.open_from_list("asker")
    assert dashboard.options_in_thread() == ["Yes", "No"]
    dashboard.expect_like("asker", AS_SHOWN)
    dashboard.assert_accessible("a question shown again after a reload")

    dashboard.answer_notice("asker", "Yes")

    dashboard.expect("asker", "picked Yes")
    waiting.until(
        lambda: dashboard.question_from("asker") == "" and dashboard.options_in_thread() == [],
        what="the notice gone and the buttons closed once answered",
    )
