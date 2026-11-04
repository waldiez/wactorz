"""What a model version is judged on: the labels its answers are held against.

A model version is bytes; whether it is any good is a question about its
answers and what was true when it gave them. The truth is the labels
(`wactorz.core.labels`), and which labels count, and how, depends on the model:
a detector is right when it flags what a person called a fault. So a version
carries a declaration, ``judged_on``, that every step judging it reads -- a
shadow trial now, an evaluation before a promotion and a drift check later --
instead of each asking the operator again.

``kind`` says how an answer is read. A ``detector`` flags an input by
publishing something for it and stays silent otherwise; it is right when it
flags an input whose label is one of ``positive``, and when it stays silent on
one whose label is not. A ``classifier`` answers with a class in the
``field`` of its output, and is right when that is the label's value; an input
it published nothing for, or no class in that field, is an answer that is
wrong. A ``regressor`` answers with a number in its ``field``, and is judged by
how far that is from the label's value, read as a number. Each kind is judged by its own function
(`wactorz.core.judges`), so a kind is added there, not by changing what is
stored.

``source``, when set, is the label source every input is judged against. Left
unset, each input is judged against the labels of its own source, the device
or topic it came from, which is what a model serving several devices needs.

The declaration is about the version, not its bytes: it may be set after the
version is stored, and changed, and each change keeps who made it and when.
"""

import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from typing import Any

from .labels import MAX_SOURCE_CHARS, MAX_VALUE_CHARS

#: How a model's answers are read, each judged by its function in `wactorz.core.judges`.
DETECTOR = "detector"
CLASSIFIER = "classifier"
REGRESSOR = "regressor"
KINDS = (DETECTOR, CLASSIFIER, REGRESSOR)

#: The kinds that read their answer from one field of the output.
READS_A_FIELD = (CLASSIFIER, REGRESSOR)

#: The longest name of the output field a classifier or regressor answers in.
MAX_FIELD_CHARS = 64

#: The most label values one declaration may count as positive.
MAX_POSITIVE = 32

#: Characters a label source cannot hold, as `wactorz.core.labels` refuses them.
_WILDCARDS = ("+", "#")


class JudgementError(ValueError):
    """A declaration of what a version is judged on that cannot be taken as written."""

    @classmethod
    def not_a_mapping(cls, given: Any) -> "JudgementError":
        return cls(f"what a version is judged on is an object, not {type(given).__name__}")

    @classmethod
    def unknown_kind(cls, kind: Any) -> "JudgementError":
        return cls(f"{kind!r} is not a kind of model judged here: one of {', '.join(KINDS)}")

    @classmethod
    def no_positive(cls) -> "JudgementError":
        return cls(
            "a detector is judged on the label values that count as positive: name at least one"
        )

    @classmethod
    def positive_for_detector(cls, kind: str) -> "JudgementError":
        return cls(
            f"positive label values are what a detector flags; a {kind} is judged on a field"
        )

    @classmethod
    def no_field(cls, kind: str) -> "JudgementError":
        return cls(f"a {kind} is judged on the output field it answers in: name it")

    @classmethod
    def field_for_detector(cls) -> "JudgementError":
        return cls(
            "a detector is judged on whether it published, not on a field: "
            "name its positive values instead"
        )

    @classmethod
    def bad_field(cls, given: Any) -> "JudgementError":
        return cls(
            f"{given!r} is not an output field: a name of at most {MAX_FIELD_CHARS} characters"
        )

    @classmethod
    def too_many_positive(cls) -> "JudgementError":
        return cls(f"at most {MAX_POSITIVE} label values may count as positive")

    @classmethod
    def bad_value(cls, value: Any) -> "JudgementError":
        return cls(
            f"{value!r} is not a label value: a word of at most {MAX_VALUE_CHARS} characters"
        )

    @classmethod
    def bad_source(cls, source: Any) -> "JudgementError":
        return cls(
            f"{source!r} is not a label source: one source, no '+' or '#', "
            f"at most {MAX_SOURCE_CHARS} characters"
        )


@dataclass(frozen=True)
class Judgement:
    """What a version is judged on, and who said so when."""

    kind: str
    positive: tuple[str, ...] = ()
    #: The label source every input is judged against; None for each input's own.
    source: str | None = None
    #: The output field a classifier or regressor answers in; None for a detector.
    field: str | None = None
    by: str = ""
    at: float = 0.0
    #: The version this was copied from when the version was stored without one.
    from_version: int | None = None
    #: Anything a later kind declares that this one does not read.
    extra: dict[str, Any] = dataclass_field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        kept = asdict(self)
        kept["positive"] = list(self.positive)
        if self.field is None:
            del kept["field"]
        return kept

    def same_terms(self, other: "Judgement | None") -> bool:
        """Whether ``other`` judges the same way, whoever declared it and when."""
        return other is not None and (
            self.kind,
            self.positive,
            self.source,
            self.field,
            self.extra,
        ) == (other.kind, other.positive, other.source, other.field, other.extra)


def declared(
    *,
    kind: str = DETECTOR,
    positive: Iterable[str] = (),
    source: str | None = None,
    field: str | None = None,
    by: str = "",
    at: float | None = None,
) -> Judgement:
    """A declaration as an operator gives it, checked. Raises `JudgementError` saying what is wrong.

    ``positive`` is the values, or one string of them separated by commas.
    """
    values = positive if isinstance(positive, str) else list(positive)
    given = {"kind": kind, "positive": values, "source": source, "by": by, "at": at}
    if field is not None:
        given["field"] = field
    return from_json(given)


def from_json(given: Any) -> Judgement:
    """The declaration ``given`` holds, as the store keeps it. Raises `JudgementError` saying what is wrong."""
    if not isinstance(given, Mapping):
        raise JudgementError.not_a_mapping(given)
    kind = given.get("kind") or DETECTOR
    if kind not in KINDS:
        raise JudgementError.unknown_kind(kind)
    positive = _values(given.get("positive") or ())
    answer_field = _field(given.get("field"))
    if kind == DETECTOR:
        if answer_field is not None:
            raise JudgementError.field_for_detector()
        if not positive:
            raise JudgementError.no_positive()
    else:
        if positive:
            raise JudgementError.positive_for_detector(str(kind))
        if answer_field is None:
            raise JudgementError.no_field(str(kind))
    known = {"kind", "positive", "source", "field", "by", "at", "from_version", "extra"}
    extra = dict(given.get("extra") or {})
    extra.update({k: v for k, v in given.items() if k not in known})
    at = given.get("at")
    from_version = given.get("from_version")
    return Judgement(
        kind=str(kind),
        positive=positive,
        source=_source(given.get("source")),
        field=answer_field,
        by=str(given.get("by") or ""),
        at=float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else time.time(),
        from_version=int(from_version) if isinstance(from_version, int) else None,
        extra=extra,
    )


def _values(given: Any) -> tuple[str, ...]:
    if isinstance(given, str):
        given = given.split(",")
    values: list[str] = []
    for value in given:
        text = str(value).strip() if isinstance(value, str) else ""
        if not text or len(text) > MAX_VALUE_CHARS:
            raise JudgementError.bad_value(value)
        if text not in values:
            values.append(text)
    if len(values) > MAX_POSITIVE:
        raise JudgementError.too_many_positive()
    return tuple(values)


def _field(given: Any) -> str | None:
    if given is None or (isinstance(given, str) and not given.strip()):
        return None
    if not isinstance(given, str) or len(given.strip()) > MAX_FIELD_CHARS:
        raise JudgementError.bad_field(given)
    return given.strip()


def _source(given: Any) -> str | None:
    if given is None or (isinstance(given, str) and not given.strip()):
        return None
    if not isinstance(given, str):
        raise JudgementError.bad_source(given)
    source = given.strip()
    if len(source) > MAX_SOURCE_CHARS or any(c in source for c in _WILDCARDS):
        raise JudgementError.bad_source(given)
    return source
