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

#: The persisted key holding the book: question id to its record.
QUESTIONS_KEY = "_questions"

#: How many answered questions the book keeps, newest last, so the agent can
#: read what was decided a while ago without the book growing for ever.
ANSWERED_KEPT = 100

#: The leaf of the retained topic an agent publishes its book on.
QUESTIONS_LEAF = "questions"


def questions_topic(agent_id: str) -> str:
    """The retained topic ``agent_id`` publishes its question book on."""
    return f"agents/{agent_id}/{QUESTIONS_LEAF}"


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
    dashboard for want of an answer.
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
