"""The questions agents have put to a person, and answering them."""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .transport import Transport


@dataclass(frozen=True)
class Question:
    """One question, open or answered."""

    id: str
    agent_id: str
    agent: str
    text: str
    options: tuple[str, ...] = ()
    context: str = ""
    channel: str = ""
    asked_at: float = 0.0
    answer: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _question_from(raw: dict[str, Any]) -> Question:
    answer = raw.get("answer")
    return Question(
        id=str(raw.get("id") or ""),
        agent_id=str(raw.get("agent_id") or ""),
        agent=str(raw.get("agent") or ""),
        text=str(raw.get("text") or ""),
        options=tuple(str(option) for option in raw.get("options") or ()),
        context=str(raw.get("context") or ""),
        channel=str(raw.get("channel") or ""),
        asked_at=float(raw.get("asked_at") or 0.0),
        answer=None if answer is None else str(answer),
        raw=raw,
    )


class Questions:
    """The server's ledger of questions."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    async def _listed(self, which: str) -> list[Question]:
        book = await self._transport.request("GET", "/api/questions")
        return [_question_from(raw) for raw in book.get(which) or ()]

    async def open(self) -> list[Question]:
        """The questions waiting for an answer, oldest first."""
        return await self._listed("open")

    async def answered(self) -> list[Question]:
        """The newest answered questions."""
        return await self._listed("answered")

    async def answer(self, question: Question, option: str, *, note: str = "") -> None:
        """Pick ``option``, as a button on the dashboard does. A refusal raises."""
        path = (
            f"/api/questions/{quote(question.agent_id, safe='')}"
            f"/{quote(question.id, safe='')}/answer"
        )
        body: dict[str, Any] = {"option": option}
        if note:
            body["note"] = note
        await self._transport.request("POST", path, json=body)
