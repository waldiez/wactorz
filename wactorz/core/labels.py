"""Labels: what a source was doing over a period, as someone said.

A label is a node of the provenance graph (`wactorz.core.provenance`): a window
of event time on one source -- a topic or an entity -- with a value, who gave
it, through which channel, and whether that channel can be trusted. See
docs/design/labels-and-provenance.md.

Labels are never edited. A later label on the same source supersedes an
earlier one wherever their windows overlap: the earlier one gains a
``superseded_by`` edge and the status ``superseded``, or ``partly_superseded``
while some of its window is still its own. What a source was doing at a moment
is the newest valid label covering it, and :meth:`Labels.effective` resolves a
period into those intervals. "Later" is when main wrote the label, not the
event time it is about: an operator correcting themselves an hour later wins.
Two labels written at the same instant are ordered as they were written.

An answer becomes a label when the question carried a :class:`LabelSpec`:
which source and window it is about, and which option means which value. The
asking agent keeps the spec in its question book, and main writes the label
when the book says the question was answered (`wactorz.agents.main.labels`).

The tables are SQLite's, so every write and query here blocks; from the event
loop, call through ``asyncio.to_thread``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .provenance import (
    ANSWERED_BY,
    INVALIDATED,
    LABEL,
    PARTLY_SUPERSEDED,
    QUESTION,
    SUPERSEDED,
    SUPERSEDED_BY,
    VALID,
    Provenance,
    ProvNode,
    stable_id,
)

if TYPE_CHECKING:
    from .persistence.db import WactorzDB
    from .questions import Question

#: Who an answer to a question is recorded as given by.
OPERATOR = "operator"

#: The longest label value, source and note kept. A label is a word, not a report.
MAX_VALUE_CHARS = 64
MAX_SOURCE_CHARS = 256
MAX_NOTE_CHARS = 1000

#: Characters a source cannot hold: a label is about one source, not a pattern.
_WILDCARDS = ("+", "#")

#: How many answers the dashboard vouches for at once; the oldest go first.
_VOUCHED_KEPT = 1000

#: (agent id, question id, option) for the answers this process sent itself.
_vouched: dict[tuple[str, str, str], None] = {}


class LabelError(ValueError):
    """A label, or a label spec, that cannot be taken as written."""

    @classmethod
    def no_source(cls) -> LabelError:
        return cls("a label names the source it is about")

    @classmethod
    def wildcard_source(cls, source: str) -> LabelError:
        return cls(f"a label is about one source, not a pattern: {source!r}")

    @classmethod
    def no_value(cls) -> LabelError:
        return cls("a label has a value")

    @classmethod
    def bad_time(cls, what: str, value: Any) -> LabelError:
        return cls(f"a label's {what} is an event time in Unix seconds, not {value!r}")

    @classmethod
    def ends_before_start(cls) -> LabelError:
        return cls("a label's window ends after it starts")

    @classmethod
    def spec_not_a_mapping(cls) -> LabelError:
        return cls("a question's label is a mapping with 'source', 'start' and 'values'")

    @classmethod
    def no_values(cls) -> LabelError:
        return cls("a question's label maps at least one option to a value")

    @classmethod
    def not_here(cls) -> LabelError:
        return cls(
            "labels are kept by main, and this agent cannot write to main's database: "
            "ask the operator with a label spec instead"
        )

    @classmethod
    def not_an_option(cls, option: str) -> LabelError:
        return cls(f"a question's label maps {option!r}, which is not one of its options")


def _time(value: Any, what: str) -> float:
    """``value`` as Unix seconds: a number, or an ISO 8601 string with an offset."""
    if isinstance(value, bool):
        raise LabelError.bad_time(what, value)
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise LabelError.bad_time(what, value) from None
        if parsed.tzinfo is None:
            raise LabelError.bad_time(what, value)
        seconds = parsed.timestamp()
    else:
        raise LabelError.bad_time(what, value)
    if not math.isfinite(seconds) or seconds < 0:
        raise LabelError.bad_time(what, value)
    return seconds


def _source(value: Any) -> str:
    source = str(value or "").strip()
    if not source:
        raise LabelError.no_source()
    if any(c in source for c in _WILDCARDS):
        raise LabelError.wildcard_source(source)
    return source[:MAX_SOURCE_CHARS]


def _value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise LabelError.no_value()
    return text[:MAX_VALUE_CHARS]


def event_time(payload: Any, default: float | None = None) -> float:
    """When ``payload`` says it happened: its ``ts``, else its ``timestamp``, else ``default``.

    ``default`` is now when unsaid. ``ts`` is Unix seconds; ``timestamp`` is
    that or an ISO 8601 string with an offset. A time that is not one counts
    as absent.
    """
    fallback = time.time() if default is None else default
    if not isinstance(payload, Mapping):
        return fallback
    for key in ("ts", "timestamp"):
        if key not in payload:
            continue
        value = payload[key]
        if key == "ts" and isinstance(value, str):
            continue
        try:
            return _time(value, key)
        except LabelError:
            continue
    return fallback


@dataclass(frozen=True)
class LabelSpec:
    """How a question's answer becomes a label.

    ``values`` maps an option to the label value it stands for; an option it
    leaves out, such as "Not sure", makes no label. ``end`` None leaves the
    window open, "until further notice".
    """

    source: str
    start: float
    values: dict[str, str]
    end: float | None = None
    model: str = ""

    @classmethod
    def parse(cls, raw: Any, options: Sequence[str] = ()) -> LabelSpec:
        """The spec ``raw`` describes, checked against the question's ``options``.

        Raises :class:`LabelError` for one that cannot be used.
        """
        if not isinstance(raw, Mapping):
            raise LabelError.spec_not_a_mapping()
        source = _source(raw.get("source"))
        start = _time(raw.get("start"), "start")
        end = None if raw.get("end") is None else _time(raw.get("end"), "end")
        if end is not None and end <= start:
            raise LabelError.ends_before_start()
        raw_values = raw.get("values")
        if not isinstance(raw_values, Mapping):
            raise LabelError.no_values()
        values: dict[str, str] = {}
        for option, value in raw_values.items():
            if value is None:
                continue
            if options and str(option) not in options:
                raise LabelError.not_an_option(str(option))
            values[str(option)] = _value(value)
        if not values:
            raise LabelError.no_values()
        return cls(
            source=source,
            start=start,
            values=values,
            end=end,
            model=str(raw.get("model") or "").strip(),
        )

    @classmethod
    def from_dict(cls, record: Any) -> LabelSpec | None:
        """The spec a persisted record holds, or None for one that is not a spec."""
        try:
            return cls.parse(record)
        except LabelError:
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "start": self.start,
            "end": self.end,
            "values": dict(self.values),
            "model": self.model,
        }

    def value_for(self, option: str | None) -> str | None:
        """The label value ``option`` stands for, or None when it makes no label."""
        return None if option is None else self.values.get(option)


@dataclass(frozen=True)
class Label:
    """A label as it is read back from the graph."""

    id: str
    source: str
    start: float
    end: float | None
    value: str
    status: str
    recorded: float
    actor: str
    node: str = ""
    channel: str = ""
    note: str = ""
    question_id: str = ""
    model: str = ""
    verified: bool = True
    #: The order it was written in, which breaks a tie in ``recorded``.
    seq: int = 0

    @property
    def order(self) -> tuple[float, int]:
        """What decides which of two labels is the later: when written, then write order."""
        return (self.recorded, self.seq)

    @classmethod
    def from_node(cls, node: ProvNode) -> Label:
        payload = node.payload
        return cls(
            id=node.id,
            source=node.source,
            start=node.ts,
            end=node.ts_end,
            seq=node.seq,
            value=str(payload.get("value", "")),
            status=node.status,
            recorded=node.recorded,
            actor=node.actor,
            node=node.node,
            channel=str(payload.get("channel", "")),
            note=str(payload.get("note", "")),
            question_id=str(payload.get("question_id", "")),
            model=str(payload.get("model", "")),
            verified=bool(payload.get("verified", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "start": self.start,
            "end": self.end,
            "value": self.value,
            "status": self.status,
            "recorded": self.recorded,
            "actor": self.actor,
            "node": self.node,
            "channel": self.channel,
            "note": self.note,
            "question_id": self.question_id,
            "model": self.model,
            "verified": self.verified,
            "seq": self.seq,
        }

    def overlaps(self, start: float, end: float | None) -> bool:
        """Whether this label's window and ``[start, end)`` share any time."""
        return _overlap(self.start, self.end, start, end)

    def covers(self, moment: float) -> bool:
        return self.start <= moment and (self.end is None or moment < self.end)


@dataclass(frozen=True)
class Interval:
    """A stretch of a source's time and the label that holds there."""

    start: float
    end: float | None
    label: Label = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "value": self.label.value,
            "label_id": self.label.id,
            "actor": self.label.actor,
            "channel": self.label.channel,
            "verified": self.label.verified,
        }


def _overlap(a_start: float, a_end: float | None, b_start: float, b_end: float | None) -> bool:
    a_before_b_ends = b_end is None or a_start < b_end
    b_before_a_ends = a_end is None or b_start < a_end
    return a_before_b_ends and b_before_a_ends


def _uncovered(
    start: float, end: float | None, windows: Sequence[tuple[float, float | None]]
) -> bool:
    """Whether some of ``[start, end)`` lies outside every one of ``windows``."""
    cursor = start
    for w_start, w_end in sorted(windows, key=lambda w: w[0]):
        if w_start > cursor:
            return True
        if w_end is None:
            return False
        cursor = max(cursor, w_end)
        if end is not None and cursor >= end:
            return False
    return end is None or cursor < end


def vouch(agent_id: str, question_id: str, option: str) -> None:
    """Note that this process sent ``option`` for the question itself, from the dashboard.

    An answer heard on the broker may be anyone's (see ``security.md``); one the
    dashboard sent is a person signed in to it. A label taken from an answer is
    marked verified only when the answer is vouched for here.
    """
    _vouched[(agent_id, question_id, option)] = None
    while len(_vouched) > _VOUCHED_KEPT:
        _vouched.pop(next(iter(_vouched)))


def vouched(agent_id: str, question_id: str, option: str | None) -> bool:
    """Whether this process vouched for ``option`` as the answer to the question."""
    return option is not None and (agent_id, question_id, option) in _vouched


class Labels:
    """The labels in ``db``: write them, list them, resolve what holds when."""

    def __init__(self, db: WactorzDB) -> None:
        self.db = db
        self.graph = Provenance(db)

    def write(
        self,
        *,
        source: str,
        start: Any,
        value: str,
        actor: str,
        end: Any = None,
        channel: str = "",
        note: str = "",
        question_id: str = "",
        model: str = "",
        node: str = "",
        verified: bool = True,
        label_id: str | None = None,
        recorded: float | None = None,
    ) -> Label:
        """Write a label, superseding the earlier ones it overlaps on the same source.

        A label already written under ``label_id`` is returned as it is, and
        supersedes nothing again. Raises :class:`LabelError` for a label that
        cannot be taken as written.
        """
        clean_source = _source(source)
        clean_start = _time(start, "start")
        clean_end = None if end is None else _time(end, "end")
        if clean_end is not None and clean_end <= clean_start:
            raise LabelError.ends_before_start()
        clean_value = _value(value)
        with self.db.transaction():
            if label_id is not None:
                existing = self.graph.get(label_id)
                if existing is not None:
                    return Label.from_node(existing)
            written = self.graph.add_node(
                LABEL,
                ts=clean_start,
                ts_end=clean_end,
                source=clean_source,
                actor=str(actor),
                node=str(node),
                node_id=label_id,
                recorded=recorded,
                payload={
                    "value": clean_value,
                    "channel": str(channel),
                    "note": str(note)[:MAX_NOTE_CHARS],
                    "question_id": str(question_id),
                    "model": str(model),
                    "verified": bool(verified),
                },
            )
            self._supersede(Label.from_node(written))
            refreshed = self.graph.get(written.id)
        return Label.from_node(refreshed or written)

    def _supersede(self, new: Label) -> None:
        """Mark every earlier label ``new`` overlaps as superseded by it, wholly or in part.

        A label written later but about an earlier answer -- one replayed from a
        retained book -- is itself superseded by what was recorded after it.
        """
        for other in self._standing(new.source, new.start, new.end, exclude=new.id):
            older, newer = (other, new) if other.order < new.order else (new, other)
            self.graph.add_edge(older.id, newer.id, SUPERSEDED_BY)
            self._restate(older.id)

    def _restate(self, label_id: str) -> None:
        """Set a label's status from the labels that supersede it."""
        node = self.graph.get(label_id)
        if node is None:
            return
        label = Label.from_node(node)
        if label.status == INVALIDATED:
            return
        superseding = (
            self.graph.get(e.dst)
            for e in self.graph.edges(label_id)
            if e.src == label_id and e.relation == SUPERSEDED_BY
        )
        later = [Label.from_node(n) for n in superseding if n is not None and n.kind == LABEL]
        windows = [(lab.start, lab.end) for lab in later]
        if not windows:
            status = VALID
        elif _uncovered(label.start, label.end, windows):
            status = PARTLY_SUPERSEDED
        else:
            status = SUPERSEDED
        if status != label.status:
            self.graph.set_status(label_id, status)

    def _standing(
        self, source: str, start: float, end: float | None, exclude: str = ""
    ) -> list[Label]:
        """The labels on ``source`` that still hold somewhere and touch ``[start, end)``, every one."""
        nodes = self.graph.nodes(
            LABEL,
            source=source,
            statuses=(VALID, PARTLY_SUPERSEDED),
            since=start,
            until=end,
            limit=None,
        )
        return [Label.from_node(n) for n in nodes if n.id != exclude]

    def get(self, label_id: str) -> Label | None:
        node = self.graph.get(label_id)
        return None if node is None or node.kind != LABEL else Label.from_node(node)

    def query(
        self,
        source: str | None = None,
        *,
        since: float | None = None,
        until: float | None = None,
        include_superseded: bool = False,
        limit: int | None = 1000,
    ) -> list[Label]:
        """Labels on ``source`` (or every source) whose window touches ``[since, until)``.

        Oldest written first; ``limit`` cuts the newest off, None cuts none.
        Superseded ones are left out unless asked for.
        """
        statuses = None if include_superseded else (VALID, PARTLY_SUPERSEDED)
        nodes = self.graph.nodes(
            LABEL, source=source, statuses=statuses, since=since, until=until, limit=limit
        )
        return [Label.from_node(n) for n in nodes]

    def effective(
        self, source: str, *, since: float | None = None, until: float | None = None
    ) -> list[Interval]:
        """What ``source`` was doing over ``[since, until)``: the newest label at each moment.

        Adjacent stretches held by the same label are one interval; a stretch no
        label covers is left out. An open window ends at ``until``, or stays open
        when that is unsaid.
        """
        candidates = self.query(source, since=since, until=until, limit=None)
        if not candidates:
            return []
        lower = min(c.start for c in candidates) if since is None else since
        points = {lower}
        for c in candidates:
            for p in (c.start, c.end):
                if p is not None and p > lower and (until is None or p < until):
                    points.add(p)
        ordered = sorted(points)
        segments: list[tuple[float, float | None]] = list(
            zip(ordered, [*ordered[1:], until], strict=True)
        )
        intervals: list[Interval] = []
        for seg_start, seg_end in segments:
            holding = [c for c in candidates if c.covers(seg_start)]
            if not holding:
                continue
            winner = max(holding, key=lambda c: c.order)
            if intervals and intervals[-1].label.id == winner.id and intervals[-1].end == seg_start:
                intervals[-1] = Interval(intervals[-1].start, seg_end, winner)
            else:
                intervals.append(Interval(seg_start, seg_end, winner))
        return intervals

    def from_answer(
        self, question: Question, *, agent: str, node: str = "", verified: bool = False
    ) -> Label | None:
        """Record an answered question that carried a label spec, and the label it makes.

        The question becomes a node of the graph either way; a label is written
        only when the option picked maps to a value. Safe to call again for the
        same question: both are written under ids minted from it, once. Returns
        the label, or None when the answer makes none.

        The label is recorded as of now, when main writes it, not as of the
        agent's ``answered_at``: a clock that runs ahead on a node would
        otherwise make its answers newer than every later correction. A caller
        recording several answers from one book writes them in the order they
        were answered, so their own order holds.
        """
        spec = LabelSpec.from_dict(question.label) if question.label else None
        if spec is None or question.answer is None:
            return None
        asked_at = question.asked_at or question.answered_at or time.time()
        question_node = self.graph.add_node(
            QUESTION,
            ts=asked_at,
            ts_end=asked_at,
            source=spec.source,
            actor=f"agent:{agent}",
            node=node,
            node_id=stable_id(QUESTION, agent, question.id),
            payload={
                "agent": agent,
                "question_id": question.id,
                "text": question.text,
                "options": list(question.options),
                "answer": question.answer,
                "answer_channel": question.answer_channel,
                "answered_at": question.answered_at,
            },
        )
        value = spec.value_for(question.answer)
        if value is None:
            return None
        label = self.write(
            source=spec.source,
            start=spec.start,
            end=spec.end,
            value=value,
            actor=OPERATOR,
            channel=question.answer_channel,
            note=question.answer_note,
            question_id=question.id,
            model=spec.model,
            node=node,
            verified=verified,
            label_id=stable_id(LABEL, agent, question.id),
        )
        self.graph.add_edge(question_node.id, label.id, ANSWERED_BY)
        return label
