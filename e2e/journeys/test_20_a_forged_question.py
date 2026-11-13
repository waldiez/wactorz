# SPDX-License-Identifier: Apache-2.0
"""A question nobody asked does not reach the Inbox, whatever is published in an agent's name.

Anything that can publish under ``agents/<id>/`` could otherwise put a question
in front of the operator under any agent's name. The server takes a book only
as its agent's word: one of main's own agents is read from the agent itself,
and one on a node must come from the node main places it on. So a book forged
under main's own id shows main's real questions, and one for an agent no node
speaks for is not shown at all: not over the socket, not in ``/api/questions``,
not in the Inbox.

The forgeries are published with the server's own broker account, which may
write anywhere, as a node's account may under ``agents/``.
"""

import json
from typing import Any

from harness import backend, browser
from harness.probe import Broker, Socket, websocket
from harness.run import Run

#: What the forged books ask: words no agent of this suite says.
FORGED = "Turn the alarm off now?"

#: An agent main places nowhere and no node's heartbeat lists.
IMPOSTOR_ID = "e2e-impostor-id"


def _book(agent: str) -> str:
    question = {
        "id": "forged-1",
        "text": FORGED,
        "options": ["Yes", "No"],
        "channel": "dashboard",
        "asked_at": 1.0,
    }
    return json.dumps({"agent": agent, "questions": [question]})


def _book_frame(sock: Socket, topic: str, limit: int = 400) -> Any:
    """The payload of the next relayed frame of ``topic``, reading past the rest."""
    for _ in range(limit):
        frame = sock.next_frame(timeout=30.0)
        if frame.get("type") == "server_event" and frame.get("topic") == topic:
            return frame.get("payload")
    raise AssertionError(f"no frame of {topic} in {limit} frames")


def test_a_forged_book_reaches_neither_the_ledger_nor_the_page(
    dashboard: browser.Dashboard, app: backend.Backend, run: Run
) -> None:
    main_id = app.rest.actor_id("main")
    main_book = f"agents/{main_id}/questions"
    with websocket(app.url, api_key=run.api_key) as sock, Broker(run) as broker:
        # In this order, on one connection: by the time main's own book comes
        # back over the socket, the impostor's has been weighed too.
        broker.publish(f"agents/{IMPOSTOR_ID}/questions", _book("e2e-impostor"))
        broker.publish(main_book, _book("main"))

        relayed = _book_frame(sock, main_book)

        assert relayed["agent"] == "main"
        assert FORGED not in json.dumps(relayed)

    listed = app.rest.call("GET", "/api/questions")
    assert FORGED not in json.dumps(listed)
    dashboard.open_inbox()
    assert dashboard.inbox_card_text(FORGED) == ""
    assert dashboard.question_from("main") == ""
    assert dashboard.question_from("e2e-impostor") == ""
