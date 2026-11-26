# SPDX-License-Identifier: Apache-2.0
"""The questions an agent has asked a person, kept until they are answered.

An agent that asks the operator something (:meth:`wactorz.Actor.ask_operator`)
may be restarted, moved or stopped before the answer comes, and the person
may answer hours later. So the question is not held in a coroutine alone: it
is written into the agent's persisted state under :data:`QUESTIONS_KEY`, as a
plain dict a node can store as JSON, and the answer is matched against that
record when it arrives, whether or not anything is still waiting for it.

The book is also published, retained, on ``agents/<id>/questions`` -- when the
agent starts and whenever a question is asked or answered -- so whoever shows a
person what is waiting for them reads one place, for agents here and on nodes
alike, and a question asked while that reader was down is still there when it
comes back.

This module is the record and the few operations on the book of them. The
waiting, the sending and the listening are the actor's.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from ..monitoring.log_redaction import redact
from .topics import agent_topic

#: The persisted key holding the book: question id to its record.
QUESTIONS_KEY = "_questions"

#: How many answered questions the book keeps, newest last, so the agent can
#: read what was decided a while ago without the book growing for ever.
ANSWERED_KEPT = 100

#: The leaf of the retained topic an agent publishes its book on.
QUESTIONS_LEAF = "questions"

#: How much of a question's ``context`` is kept: a line or two a person reads
#: before answering, not a report.
MAX_CONTEXT_CHARS = 1000

#: How a permission question still open at a start reads: what waited for the
#: answer is gone, so a press now would let nothing happen.
CUT_SHORT = "The server restarted first"


#: The longest id a caller may give a question (`ask_operator(..., id=...)`).
MAX_ID_CHARS = 128


class QuestionIdError(ValueError):
    """An ``id=`` for a question that cannot be used: not one, or another question's."""

    @classmethod
    def unusable(cls, question_id: object) -> QuestionIdError:
        """Not printable text of at most :data:`MAX_ID_CHARS` characters."""
        return cls(
            f"A question's id is printable text of 1 to {MAX_ID_CHARS} characters, "
            f"not {str(question_id)[:40]!r}"
        )

    @classmethod
    def taken(cls, question_id: str) -> QuestionIdError:
        """The id names a question already asked that is not this one."""
        return cls(
            f"The question {question_id!r} was already asked with other words, options, "
            "channel, person, label or context; give this one an id of its own"
        )


def checked_id(question_id: object) -> str:
    """``question_id`` as a question's id, or :class:`QuestionIdError` saying why it cannot be one."""
    if (
        not isinstance(question_id, str)
        or not question_id
        or len(question_id) > MAX_ID_CHARS
        or not question_id.isprintable()
    ):
        raise QuestionIdError.unusable(question_id)
    return question_id


def context_text(raw: str) -> str:
    """``raw`` as a question's context: redacted, trimmed and cut to `MAX_CONTEXT_CHARS`.

    The book is retained on the broker, and an agent writes this from what it
    was reading, so anything shaped like a secret goes before it is kept.
    """
    return redact(str(raw)).strip()[:MAX_CONTEXT_CHARS]


def questions_topic(agent_id: str) -> str:
    """The retained topic ``agent_id`` publishes its question book on."""
    return agent_topic(agent_id, QUESTIONS_LEAF)


def book_frame(agent: str, questions: dict[str, Question]) -> dict[str, Any]:
    """What the questions topic carries: who asks, and every question in the book."""
    return {"agent": agent, "questions": [q.to_dict() for q in questions.values()]}


def questions_in_frame(frame: Any) -> list[Question]:
    """The questions a questions-topic frame holds; records that are not one are left out."""
    records = frame.get("questions") if isinstance(frame, dict) else None
    if not isinstance(records, list):
        return []
    return [q for q in (Question.from_dict(r) for r in records) if q is not None]


@dataclass
class Question:
    """One question put to a person, and its answer once there is one.

    ``id`` is the message id the channels know it by. ``answer`` is the option
    picked, None until then; ``answer_channel`` says where it was picked, and
    ``answer_note`` what the person added to it, if anything. ``fallen_back``
    is set once a question sent on a chat channel was shown again on the
    dashboard for want of an answer. ``label`` is how the answer becomes a
    label, as a :class:`wactorz.core.labels.LabelSpec` dict, or None for a
    question whose answer is not one. ``context`` says why the question is
    being asked, for the person weighing the answer; it stays in the book, so
    the dashboard's Inbox shows it and the chat channels send the question alone.
    ``permission`` marks a question whose answer lets something happen: it is
    settled only through the agent's ``dashboard_answer``, never from the answer
    topic, which anything on the broker can write to.
    """

    id: str
    text: str
    options: tuple[str, ...]
    channel: str
    user: str = ""
    asked_at: float = 0.0
    answer: str | None = None
    answer_index: int | None = None
    answer_channel: str = ""
    answered_at: float = 0.0
    fallen_back: bool = False
    answer_note: str = ""
    label: dict[str, Any] | None = None
    context: str = ""
    permission: bool = False
    #: Whether the question was sent to the person. An agent records it first
    #: and sends it after, so a record whose sending was cut short says so, and
    #: is sent when the agent starts or asks it again. A record from before this
    #: field was sent before it was recorded.
    sent: bool = True

    @property
    def open(self) -> bool:
        """Whether the question still waits for an answer."""
        return self.answer is None

    def to_dict(self) -> dict[str, Any]:
        """The record as it is persisted: JSON-shaped, so a node can store it."""
        record = asdict(self)
        record["options"] = list(self.options)
        return record

    @classmethod
    def from_dict(cls, record: Any) -> Question | None:
        """The question a persisted record holds, or None for a record that is not one."""
        if not isinstance(record, dict):
            return None
        try:
            options = record.get("options") or ()
            return cls(
                id=str(record["id"]),
                text=str(record.get("text", "")),
                options=tuple(str(o) for o in options),
                channel=str(record.get("channel", "")),
                user=str(record.get("user", "")),
                asked_at=float(record.get("asked_at") or 0.0),
                answer=None if record.get("answer") is None else str(record["answer"]),
                answer_index=(
                    None if record.get("answer_index") is None else int(record["answer_index"])
                ),
                answer_channel=str(record.get("answer_channel", "")),
                answer_note=str(record.get("answer_note") or ""),
                answered_at=float(record.get("answered_at") or 0.0),
                fallen_back=bool(record.get("fallen_back", False)),
                label=dict(record["label"]) if isinstance(record.get("label"), dict) else None,
                context=str(record.get("context") or ""),
                permission=bool(record.get("permission", False)),
                sent=bool(record.get("sent", True)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def questions_in(book: Any) -> dict[str, Question]:
    """The questions a persisted ``book`` holds, by id; records that are not one are left out."""
    if not isinstance(book, dict):
        return {}
    found: dict[str, Question] = {}
    for key, record in book.items():
        question = Question.from_dict(record)
        if question is not None:
            found[str(key)] = question
    return found


def book_of(questions: Sequence[Question] | dict[str, Question]) -> dict[str, dict[str, Any]]:
    """The persisted form of ``questions``, trimmed of all but the newest answered ones.

    Open questions are always kept; answered ones beyond :data:`ANSWERED_KEPT`
    go, oldest first, so the book stays a book and not a log.
    """
    items = list(questions.values()) if isinstance(questions, dict) else list(questions)
    answered = sorted((q for q in items if not q.open), key=lambda q: q.answered_at)
    dropped = {q.id for q in answered[: max(0, len(answered) - ANSWERED_KEPT)]}
    return {q.id: q.to_dict() for q in items if q.id not in dropped}


def same_question(one: Question, other: Question) -> bool:
    """Whether ``one`` and ``other`` ask the same thing, so one id may name both.

    What was asked, as it is kept: the words, the options, where and to whom,
    the label its answer makes, the context and whether it is a permission. Not
    when it was asked, nor its answer, nor ``fallen_back`` and ``sent``, which
    say how it was delivered, not what it is.
    """
    return (
        one.text,
        one.options,
        one.channel,
        one.user,
        one.label,
        one.context,
        one.permission,
    ) == (
        other.text,
        other.options,
        other.channel,
        other.user,
        other.label,
        other.context,
        other.permission,
    )


def answered(question: Question, option: str, index: int, channel: str, note: str = "") -> Question:
    """``question`` with ``option`` recorded as its answer, now, with the person's ``note``."""
    return Question(
        **{
            **asdict(question),
            "options": question.options,
            "answer": option,
            "answer_index": index,
            "answer_channel": channel,
            "answer_note": note,
            "answered_at": time.time(),
        }
    )
