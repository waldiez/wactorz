# SPDX-License-Identifier: Apache-2.0
"""The questions agents have put to a person, as the server knows them.

Each agent publishes its question book, retained, on ``agents/<id>/questions``
(see `wactorz.core.questions`): when it starts, and whenever a question is
asked or answered. The ledger here is the newest book from each agent, so an
agent on a node reads the same as one in this process, and a server that comes
back finds on the broker every question asked while it was away.

An agent's book leaves the ledger when the agent goes: its manifest withdrawn,
or the retained book cleared, which is what deleting an agent does. A deleted
agent's book is left out of a listing even before either arrives.

A book is taken only as its agent's word, by the rule main's labels use
(`wactorz.agents.main.labels.admit_book`): an agent in this process is read from
the agent, and one on a node must come from the node main places it on. A book
that is not admitted is neither kept nor shown to a browser, and the last one
admitted stands. Until the system has started there is no main to place agents
by, so books are held, the newest per agent, and admitted once it has; which
profile it started in is latched then, so a main that is restarting is not taken
for a profile that runs none.

The ledger also checks an answer before it is sent: to a question still open,
with one of the options it offered. A question the ledger does not know -- one
sent with ``notify_user`` alone, which keeps no book -- is the agent's to judge.
"""

import dataclasses
import json
import logging
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from aiohttp import web

from ..agents.lookup import find_main_actor
from ..agents.main.labels import admit_book
from ..core.outbound import Answer, numbered
from ..core.questions import ANSWERED_KEPT, QUESTIONS_LEAF, Question, questions_in_frame
from ..monitoring.log_redaction import redact
from . import runtime

logger: logging.Logger = logging.getLogger(__name__)

#: agent id -> (the agent's name, the questions in its newest book).
_books: dict[str, tuple[str, list[Question]]] = {}

#: agent id -> (its name, its node) as last admitted: whose a clear is.
_admitted: dict[str, tuple[str, str]] = {}

#: Where the system is: books are held while it starts, and admitted once it has,
#: by main (the full profile) or without one (the minimal profile).
STARTING, WITH_MAIN, WITHOUT_MAIN = "starting", "with main", "without main"

#: agent id -> (topic, payload, properties) of the newest book held, oldest first.
_held: OrderedDict[str, tuple[str, bytes, dict[str, str]]] = OrderedDict()

#: The most books held at once; the oldest goes past it.
HELD_KEPT = 1000


class AnswerRefusedError(ValueError):
    """An answer the ledger will not send, and the HTTP status that says so."""

    status = 400


class UnknownQuestionError(AnswerRefusedError):
    """An answer to a question the agent never asked, or one no longer kept."""

    status = 404

    def __init__(self, agent_id: str, question_id: str) -> None:
        super().__init__(f"{agent_id[:8]} has asked no question {question_id!r}")


class AlreadyAnsweredError(AnswerRefusedError):
    """An answer to a question that already has one."""

    status = 409

    def __init__(self, question: Question) -> None:
        super().__init__(f"{question.text[:60]!r} was already answered {question.answer!r}")


class NotAnOptionError(AnswerRefusedError):
    """An answer that is not one of the options the question offers."""

    def __init__(self, option: str, question: Question) -> None:
        super().__init__(f"{option!r} is not one of {', '.join(map(repr, question.options))}")


def is_book_topic(topic: str) -> bool:
    """Whether ``topic`` is an agent's question book, ``agents/<id>/questions``."""
    parts = topic.split("/")
    return len(parts) == 3 and parts[0] == "agents" and parts[2] == QUESTIONS_LEAF


def receive(topic: str, payload: bytes, properties: Mapping[str, str]) -> tuple[str, str] | None:
    """What to take in for a book that came off the broker: its topic and its admitted payload.

    The payload is the book as admitted (its agent's own name and questions),
    or "" for an admitted clear. None when there is nothing to take: the
    book is not its agent's word, or it is held until the system has started
    or while main is not there.
    """
    if _waiting():
        _hold(topic, payload, dict(properties))
        return None
    return _admitted_payload(topic, payload, properties)


def started(*, with_main: bool) -> list[tuple[str, str]]:
    """Mark the system started, latching whether it runs main; the held books, admitted."""
    runtime.started_with_main = with_main
    return release()


def holding() -> bool:
    """Whether any book is held, waiting for something to admit it by."""
    return bool(_held)


def release() -> list[tuple[str, str]]:
    """The books held, each admitted now or dropped, once there is something to admit them by."""
    if _waiting():
        return []
    held = list(_held.values())
    _held.clear()
    return [
        taken
        for topic, payload, properties in held
        if (taken := _admitted_payload(topic, payload, properties))
    ]


def _hold(topic: str, payload: bytes, properties: dict[str, str]) -> None:
    agent_id = topic.split("/")[1]
    _held.pop(agent_id, None)
    _held[agent_id] = (topic, payload, properties)
    while len(_held) > HELD_KEPT:
        dropped, _ = _held.popitem(last=False)
        logger.warning(
            "[questions] Too many books held until main can place their agents; dropped %s's",
            dropped[:8],
        )


def phase() -> str:
    """Where the system is, as the latch says: starting, with main, or without one."""
    if runtime.started_with_main is None:
        return STARTING
    return WITH_MAIN if runtime.started_with_main else WITHOUT_MAIN


def _waiting() -> bool:
    """Whether there is nothing yet to admit a book by: not started, or main not there."""
    return phase() == STARTING or (phase() == WITH_MAIN and _main() is None)


def _main() -> Any:
    return find_main_actor(runtime.registry) if runtime.registry is not None else None


def _admitted_payload(
    topic: str, payload: bytes, properties: Mapping[str, str]
) -> tuple[str, str] | None:
    book = admit_book(
        topic,
        payload,
        properties,
        host=_main() if phase() == WITH_MAIN else None,
        registry=runtime.registry,
        last=_admitted,
    )
    if book is None:
        return None
    _admitted[book.actor_id] = (book.name, book.node)
    if book.cleared and not payload:
        return topic, ""
    return topic, json.dumps(book.frame())


def record(agent_id: str, frame: Any) -> None:
    """Take ``frame``, an admitted questions-topic payload, as ``agent_id``'s book now.

    Anything that is not a book is passed over, so the last one taken stands;
    an empty payload, a clear, is `forget`.
    """
    if not isinstance(frame, dict):
        return
    agent = frame.get("agent")
    name = agent if isinstance(agent, str) and agent else agent_id[:8]
    _books[agent_id] = (name, questions_in_frame(frame))


def forget(agent_id: str) -> None:
    """Drop ``agent_id``'s book."""
    _books.pop(agent_id, None)
    _admitted.pop(agent_id, None)


def find(agent_id: str, question_id: str) -> Question | None:
    """The question ``question_id`` in ``agent_id``'s book, or None when the ledger has none."""
    if runtime.is_deleted(agent_id):
        return None
    _, questions = _books.get(agent_id, ("", []))
    return next((q for q in questions if q.id == question_id), None)


def stored_forms(question: Question) -> tuple[str, ...]:
    """The words a chat-log row holds for ``question``, as the server stores an agent's message.

    With and without the reply hint: rows stored before the dashboard's copy
    dropped it still say "Reply with the number."
    """
    return tuple(
        redact(numbered(question.text, question.options, reply_hint=hint)) for hint in (False, True)
    )


def asked_in(agent_name: str, content: str, ts: float) -> tuple[str, Question] | None:
    """The question a chat-log row of ``agent_name`` is, with its agent's id, or None.

    The row holds only the question's words, so the same words asked twice
    are told apart by time: the one asked nearest ``ts``.
    """
    found = [
        (agent_id, question)
        for agent_id, (name, questions) in _books.items()
        if name == agent_name and not runtime.is_deleted(agent_id)
        for question in questions
        if content in stored_forms(question)
    ]
    return min(found, key=lambda pair: abs(pair[1].asked_at - ts), default=None)


def checked(answer: Answer, *, known_only: bool = False) -> Answer:
    """``answer``, with the index of its option, once it may be sent.

    Raises an `AnswerRefusedError` for a question already answered or an
    option it did not offer. A question the ledger does not know passes
    unchanged, unless ``known_only``.
    """
    question = find(answer.agent_id, answer.id)
    if question is None:
        if known_only:
            raise UnknownQuestionError(answer.agent_id, answer.id)
        return answer
    if not question.open:
        raise AlreadyAnsweredError(question)
    if answer.option not in question.options:
        raise NotAnOptionError(answer.option, question)
    return dataclasses.replace(answer, index=question.options.index(answer.option))


def listing() -> dict[str, list[dict[str, Any]]]:
    """Every question in the ledger: the open ones oldest first, then the newest answered.

    Each is the question's record with the agent that asked it, by id and name.
    The answered ones are as many as one agent's book keeps.
    """
    open_: list[dict[str, Any]] = []
    answered: list[dict[str, Any]] = []
    for agent_id, (name, questions) in _books.items():
        if runtime.is_deleted(agent_id):
            continue
        for question in questions:
            entry = {**question.to_dict(), "agent_id": agent_id, "agent": name}
            (open_ if question.open else answered).append(entry)
    open_.sort(key=lambda q: q["asked_at"])
    answered.sort(key=lambda q: q["answered_at"], reverse=True)
    return {"open": open_, "answered": answered[:ANSWERED_KEPT]}


def asked_by() -> dict[str, dict[str, Any]]:
    """What each agent has asked a person, by the agent's id: how many are open and answered, and where.

    ``channels`` are where its questions went, as each was asked. An agent
    that asked nothing, or whose book is gone, is not here.
    """
    asked: dict[str, dict[str, Any]] = {}
    for agent_id, (_name, questions) in _books.items():
        if runtime.is_deleted(agent_id) or not questions:
            continue
        asked[agent_id] = {
            "open": sum(1 for q in questions if q.open),
            "answered": sum(1 for q in questions if not q.open),
            "channels": sorted({q.channel for q in questions if q.channel}),
        }
    return asked


async def questions_handler(_request: web.Request) -> web.Response:
    """``GET /api/questions``: what is waiting for an answer, and what was answered lately."""
    return web.json_response(listing())
