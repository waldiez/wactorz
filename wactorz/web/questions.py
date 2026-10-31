"""The questions agents have put to a person, as the server knows them.

Each agent publishes its question book, retained, on ``agents/<id>/questions``
(see `wactorz.core.questions`): when it starts, and whenever a question is
asked or answered. The ledger here is the newest book from each agent, so an
agent on a node reads the same as one in this process, and a server that comes
back finds on the broker every question asked while it was away.

An agent's book leaves the ledger when the agent goes: its manifest withdrawn,
or the retained book cleared, which is what deleting an agent does. A deleted
agent's book is left out of a listing even before either arrives.

The ledger also checks an answer before it is sent: to a question still open,
with one of the options it offered. A question the ledger does not know -- one
sent with ``notify_user`` alone, which keeps no book -- is the agent's to judge.
"""

import dataclasses
from typing import Any

from aiohttp import web

from ..core.outbound import Answer, numbered
from ..core.questions import ANSWERED_KEPT, Question, questions_in_frame
from ..monitoring.log_redaction import redact
from . import runtime

#: agent id -> (the agent's name, the questions in its newest book).
_books: dict[str, tuple[str, list[Question]]] = {}


class AnswerRefusedError(ValueError):
    """An answer the ledger will not send, and the HTTP status that says so."""

    status = 400


class UnknownQuestionError(AnswerRefusedError):
    status = 404

    def __init__(self, agent_id: str, question_id: str) -> None:
        super().__init__(f"{agent_id[:8]} has asked no question {question_id!r}")


class AlreadyAnsweredError(AnswerRefusedError):
    status = 409

    def __init__(self, question: Question) -> None:
        super().__init__(f"{question.text[:60]!r} was already answered {question.answer!r}")


class NotAnOptionError(AnswerRefusedError):
    def __init__(self, option: str, question: Question) -> None:
        super().__init__(f"{option!r} is not one of {', '.join(map(repr, question.options))}")


def record(agent_id: str, frame: Any) -> None:
    """Take ``frame``, one questions-topic payload, as ``agent_id``'s book now.

    Anything that is not a book -- the empty payload that clears a retained
    topic included -- leaves the agent with none.
    """
    if not isinstance(frame, dict):
        forget(agent_id)
        return
    agent = frame.get("agent")
    name = agent if isinstance(agent, str) and agent else agent_id[:8]
    _books[agent_id] = (name, questions_in_frame(frame))


def forget(agent_id: str) -> None:
    """Drop ``agent_id``'s book."""
    _books.pop(agent_id, None)


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


async def questions_handler(_request: web.Request) -> web.Response:
    """``GET /api/questions``: what is waiting for an answer, and what was answered lately."""
    return web.json_response(listing())
