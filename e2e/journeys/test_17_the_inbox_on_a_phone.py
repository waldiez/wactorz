"""A question answered from a phone: the Inbox holds what the agents wait on the person for.

An agent on the node asks with its reason. On a page a phone's size the Inbox's
tab in the bottom bar counts the question, its card says why it is asked, and
the answer goes with a note. The agent gets the pick, the count goes, and the
question is in the Answered list with the note.

It starts from what journey 16 left: the node deployed, and model code going to it.
"""

import re
from collections.abc import Iterator

import pytest
from harness import browser, waiting
from harness.run import NODE_NAME
from model import CHECK, CHECK_CONTEXT

#: How long the question may take to reach the page: the spawn on the node, the
#: agent's first round, and its book over the broker.
ASKED_WITHIN_S = 60.0

NOTE = "Closed by hand at the panel."


@pytest.fixture(name="phone")
def phone_fixture(dashboard: browser.Dashboard) -> Iterator[browser.Dashboard]:
    """The dashboard a phone's size, for this journey alone."""
    yield dashboard.at_phone_size()
    dashboard.at_desktop_size()


def test_a_question_from_the_node_is_answered_from_a_phone(phone: browser.Dashboard) -> None:
    phone.at_desktop_size().say("please start a checker", to="main")
    phone.expect_like("main", rf"Starting it\.\n+ℹ️ Spawned 'checker' on node '{NODE_NAME}'")
    phone.at_phone_size()

    waiting.until(
        lambda: phone.inbox_tab_name() == "Inbox 1 waiting",
        what="the Inbox's tab to count the question",
        timeout=ASKED_WITHIN_S,
    )
    phone.open_inbox()
    assert CHECK_CONTEXT in phone.inbox_card_text(CHECK)
    phone.assert_accessible("the Inbox on a phone, with a question waiting")

    phone.answer_in_inbox(CHECK, "Closed", note=NOTE)

    waiting.until(
        lambda: phone.inbox_tab_name() == "Inbox" and not phone.inbox_card_text(CHECK),
        what="the question to leave what is waiting",
    )
    phone.open_answered()
    answered = phone.inbox_card_text(CHECK)
    assert "Answered: Closed" in answered
    assert NOTE in answered

    phone.at_desktop_size().open_from_list("checker")
    phone.expect_like("checker", rf"{re.escape(CHECK)}\s*Closed\s*Open")
    phone.expect("checker", "checked: Closed")
