# SPDX-License-Identifier: Apache-2.0
"""When a version on trial becomes the one served: the policy, and a trial held against it.

A model's name carries a `Policy` saying what its shadow must show before it
may become ``active``: the figure compared, the margin, how long the trial
must have run, how many labelled inputs it must hold, whether the difference
must be significant, and whether main promotes on its own or asks. It lives
beside the pointers in the store's index (`wactorz.core.artifacts`), is about
the name rather than a version, and is read by main on a cadence while a
shadow is set (`wactorz.agents.main.shadow_serving`). `judge_trial` holds a
trial's comparison (`wactorz.core.shadow_trial.compare`, judged against the
labels) against a policy and says which terms are met; `record_decision`
writes the verdict as a ``decision`` node of the graph. ``docs/design/
promotion-policy.md`` is the decision.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .judgement import CLASSIFIER, DETECTOR, REGRESSOR
from .provenance import DECISION, Provenance, ProvNode, model_source, stable_id

#: The figures each kind's trial reports that a policy may compare on, the
#: first its main one, compared when the policy names none.
METRICS_OF_KIND: dict[str, tuple[str, ...]] = {
    DETECTOR: ("f1", "precision", "recall", "mcc", "false_positive_rate"),
    CLASSIFIER: ("macro_f1", "weighted_f1", "accuracy"),
    REGRESSOR: ("mae", "rmse"),
}
#: Every figure a policy may name, whatever the kind.
METRICS = tuple(m for metrics in METRICS_OF_KIND.values() for m in metrics)
#: The figures where less is better.
LOWER_IS_BETTER = frozenset({"false_positive_rate", "mae", "rmse"})

#: What a name's policy says when nothing is set; no metric is the kind's main one.
DEFAULT_METRIC: str | None = None
DEFAULT_MARGIN = 0.0
DEFAULT_MIN_DAYS = 1.0
DEFAULT_MIN_LABELLED = 100
DEFAULT_SIGNIFICANT = True
DEFAULT_AUTOMATIC = False

#: Who a promotion the policy makes is recorded as.
POLICY = "policy"

#: What the decision is about, in its payload's ``what``.
PROMOTION = "promotion"

#: The fields a policy is given, and the words for each when it is said.
FIELDS = ("metric", "margin", "min_days", "min_labelled", "significant", "automatic")

_DAY_S = 86400.0

#: The names of the tests the ``significant`` term reads.
MCNEMAR = "mcnemar"
SIGN_TEST = "sign"


class PolicyError(ValueError):
    """A policy that cannot be taken as given, and why."""

    @classmethod
    def not_a_mapping(cls, given: Any) -> PolicyError:
        return cls(f"a policy is a mapping of its fields, not {given!r}")

    @classmethod
    def unknown_field(cls, name: str) -> PolicyError:
        return cls(f"a policy has no {name!r}; its fields are {', '.join(FIELDS)}")

    @classmethod
    def unknown_metric(cls, metric: Any) -> PolicyError:
        return cls(f"a policy compares on one of {', '.join(METRICS)}, not {metric!r}")

    @classmethod
    def not_a_number(cls, name: str, value: Any) -> PolicyError:
        return cls(f"{name} is a number, not {value!r}")

    @classmethod
    def negative(cls, name: str, value: Any) -> PolicyError:
        return cls(f"{name} cannot be below zero: {value!r}")

    @classmethod
    def not_a_bool(cls, name: str, value: Any) -> PolicyError:
        return cls(f"{name} is yes or no, not {value!r}")

    @classmethod
    def not_a_count(cls, name: str, value: Any) -> PolicyError:
        return cls(f"{name} is a whole number, not {value!r}")


@dataclass(frozen=True)
class Policy:
    """When a name's shadow may become active, and who said so when."""

    #: The figure compared; None for the main one of the kind the trial is judged as.
    metric: str | None = DEFAULT_METRIC
    margin: float = DEFAULT_MARGIN
    min_days: float = DEFAULT_MIN_DAYS
    min_labelled: int = DEFAULT_MIN_LABELLED
    significant: bool = DEFAULT_SIGNIFICANT
    automatic: bool = DEFAULT_AUTOMATIC
    by: str = ""
    at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def same_terms(self, other: Policy | None) -> bool:
        """Whether ``other`` asks the same, whoever set it and when."""
        return other is not None and self.terms() == other.terms()

    def terms(self) -> dict[str, Any]:
        """The policy's fields alone."""
        return {name: getattr(self, name) for name in FIELDS}

    def words(self) -> str:
        """The policy as a line: ``f1 by 0.02 over 1 day and 100 labelled, significant, asked``."""
        days = f"{self.min_days:g} day" + ("" if self.min_days == 1 else "s")
        metric = self.metric or "the main figure of its kind"
        return (
            f"{metric} by {self.margin:g} over {days} and {self.min_labelled} labelled, "
            f"{'significant' if self.significant else 'significance not required'}, "
            f"{'promoted on its own' if self.automatic else 'asked'}"
        )


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError.not_a_number(name, value)
    if value < 0:
        raise PolicyError.negative(name, value)
    return float(value)


def _count(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError.not_a_count(name, value)
    if value < 0:
        raise PolicyError.negative(name, value)
    return value


def _flag(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("yes", "no", "true", "false"):
        return value.strip().lower() in ("yes", "true")
    raise PolicyError.not_a_bool(name, value)


def declared(
    given: Mapping[str, Any] | None = None,
    *,
    over: Policy | None = None,
    by: str = "",
    at: float | None = None,
) -> Policy:
    """A policy as a person gives it: ``given``'s fields over ``over``'s, else the defaults.

    Raises `PolicyError` saying what is wrong. ``by`` and ``at`` are who set
    it and when; ``at`` is now when unsaid.
    """
    if given is None:
        given = {}
    if not isinstance(given, Mapping):
        raise PolicyError.not_a_mapping(given)
    base = (over or Policy()).terms()
    for name in given:
        if name not in FIELDS:
            raise PolicyError.unknown_field(str(name))
    fields = {**base, **given}
    metric = fields["metric"]
    if metric is not None and metric not in METRICS:
        raise PolicyError.unknown_metric(metric)
    return Policy(
        metric=None if metric is None else str(metric),
        margin=_number("margin", fields["margin"]),
        min_days=_number("min_days", fields["min_days"]),
        min_labelled=_count("min_labelled", fields["min_labelled"]),
        significant=_flag("significant", fields["significant"]),
        automatic=_flag("automatic", fields["automatic"]),
        by=str(by),
        at=time.time() if at is None else float(at),
    )


def from_json(given: Any) -> Policy:
    """The policy ``given`` holds, as the store keeps it. Raises `PolicyError`."""
    if not isinstance(given, Mapping):
        raise PolicyError.not_a_mapping(given)
    terms = {name: given[name] for name in FIELDS if name in given}
    at = given.get("at")
    return declared(
        terms,
        by=str(given.get("by") or ""),
        at=float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0,
    )


# ── A trial against a policy ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Term:
    """One thing the policy asks, what the trial shows for it, and whether that is enough."""

    name: str
    wanted: Any
    actual: Any
    met: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Verdict:
    """A trial held against a policy: every term, and whether all are met."""

    met: bool
    terms: tuple[Term, ...]
    #: The figure compared, each side's value and the shadow's gain on it.
    metric: str
    shadow: float | None = None
    active: float | None = None
    gain: float | None = None
    policy: Policy = field(default_factory=Policy)
    #: The kind the trial was judged as; None for a trial not judged.
    kind: str | None = None
    #: The test the ``significant`` term reads: McNemar's, or a regressor's sign test.
    test: str = "mcnemar"

    @property
    def reported(self) -> bool:
        """Whether the trial's kind reports the figure compared; true for a trial not judged."""
        return self.kind is None or self.metric in METRICS_OF_KIND.get(self.kind, ())

    def to_dict(self) -> dict[str, Any]:
        return {
            "met": self.met,
            "metric": self.metric,
            "kind": self.kind,
            "reported": self.reported,
            "metrics": list(METRICS_OF_KIND.get(self.kind or "", ())),
            "test": self.test,
            "shadow": self.shadow,
            "active": self.active,
            "gain": self.gain,
            "terms": [t.to_dict() for t in self.terms],
            "policy": self.policy.to_json(),
            "words": self.words(),
        }

    def words(self) -> str:
        """The verdict as a line, for a move's reason or the feed."""
        parts: list[str] = []
        if not self.reported:
            parts.append(f"{self.metric} is not a figure a {self.kind} reports")
        elif self.shadow is not None and self.active is not None and self.gain is not None:
            parts.append(
                f"{self.metric} {self.shadow:.4g} vs {self.active:.4g} "
                f"({'+' if self.gain >= 0 else ''}{self.gain:.4g}, margin {self.policy.margin:g})"
            )
        else:
            parts.append(f"{self.metric} not measured")
        by_name = {t.name: t for t in self.terms}
        days = by_name.get("min_days")
        if days is not None:
            parts.append(f"{float(days.actual):.3g} day(s)")
        labelled = by_name.get("min_labelled")
        if labelled is not None:
            parts.append(f"{int(labelled.actual)} labelled")
        significant = by_name.get("significant")
        if significant is not None and significant.wanted:
            test = "sign test" if self.test == SIGN_TEST else "McNemar"
            parts.append(f"{test} p={float(significant.actual):.3g}")
        verdict = "met" if self.met else "not met"
        return f"{verdict}: {', '.join(parts)}"


def _figure(side: Mapping[str, Any] | None, metric: str) -> float | None:
    if not isinstance(side, Mapping):
        return None
    value = side.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _days_of(summary: Mapping[str, Any], since: float, now: float) -> float:
    """How long the trial has run: its pairs' event-time span, else wall-clock time since the shadow was set."""
    span = summary.get("span")
    if isinstance(span, Mapping):
        first, last = span.get("since"), span.get("until")
        if isinstance(first, (int, float)) and isinstance(last, (int, float)):
            return max(0.0, float(last) - float(first)) / _DAY_S
    return max(0.0, now - since) / _DAY_S


def judge_trial(summary: Mapping[str, Any], policy: Policy, *, since: float, now: float) -> Verdict:
    """``summary``, a trial compared and judged against the labels, held against ``policy``.

    A trial that is not judged (``labels`` absent) meets nothing on the
    figures and says so in its terms. The figure compared is the policy's,
    else the main one of the kind the trial is judged as; one that kind does
    not report is never met, and the verdict says so. Significance is
    McNemar's test, or for a regressor the sign test on which side was nearer
    the truth. The days are the event time the trial's pairs cover (the
    summary's ``span``), so a replayed day counts as one however fast it was
    played, as ``docs/design/event-time.md`` asks; a trial with no pairs has
    run from ``since``, when the shadow was set, to ``now``, on the wall clock.
    """
    labels = summary.get("labels")
    judged = isinstance(labels, Mapping)
    kind = str(labels.get("kind") or DETECTOR) if judged else None
    metric = policy.metric or METRICS_OF_KIND.get(kind or DETECTOR, ("f1",))[0]
    shadow = _figure(labels.get("shadow") if judged else None, metric)
    active = _figure(labels.get("active") if judged else None, metric)
    gain: float | None = None
    if shadow is not None and active is not None:
        gain = (active - shadow) if metric in LOWER_IS_BETTER else (shadow - active)
    days = _days_of(summary, since, now)
    labelled = int(labels.get("labelled") or 0) if judged else 0
    test = SIGN_TEST if kind == REGRESSOR else MCNEMAR
    tested = labels.get("closer" if test == SIGN_TEST else "mcnemar") if judged else None
    p = float(tested.get("p", 1.0)) if isinstance(tested, Mapping) else 1.0
    better = tested.get("better") if isinstance(tested, Mapping) else None
    terms = (
        Term("judged", True, judged, judged),
        Term("margin", policy.margin, gain, gain is not None and gain >= policy.margin),
        Term("min_days", policy.min_days, days, days >= policy.min_days),
        Term("min_labelled", policy.min_labelled, labelled, labelled >= policy.min_labelled),
        Term(
            "significant",
            policy.significant,
            p,
            (not policy.significant) or (better == "shadow"),
        ),
    )
    verdict = Verdict(
        met=all(t.met for t in terms),
        terms=terms,
        metric=metric,
        shadow=shadow,
        active=active,
        gain=gain,
        policy=policy,
        kind=kind,
        test=test,
    )
    # A figure the kind does not report is never met, whatever the sides hold under that name.
    return verdict if verdict.reported else replace(verdict, met=False)


# ── The decision in the graph ─────────────────────────────────────────────────


def decision_id(name: str, shadow_version: int, since: float, met: bool, policy_at: float) -> str:
    """The id of the decision on the trial of ``name:shadow_version`` from ``since``, under the policy set at ``policy_at``, coming to ``met``.

    A decision is made under a policy, so a policy set anew gives a new
    decision on the same trial, and main acts on it again.
    """
    return stable_id(PROMOTION, name, shadow_version, since, met, policy_at)


def record_decision(
    prov: Provenance,
    *,
    name: str,
    shadow_version: int,
    active_version: int | None,
    since: float,
    verdict: Verdict,
    at: float,
    by: str = POLICY,
) -> ProvNode:
    """Write what the policy decided of the trial, as a ``decision`` about the model.

    The id is the trial's, the policy's and the verdict's, so a check that
    finds the same answer again writes nothing new; a verdict that changed,
    or a policy set anew, is a new node.
    """
    return prov.add_node(
        DECISION,
        ts=since,
        ts_end=at,
        source=model_source(name),
        actor=by,
        payload={
            "what": PROMOTION,
            "model": name,
            "shadow_version": shadow_version,
            "active_version": active_version,
            **verdict.to_dict(),
        },
        node_id=decision_id(name, shadow_version, since, verdict.met, verdict.policy.at),
        recorded=at,
    )


def decisions_of(prov: Provenance, name: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """The policy's decisions about ``name``'s trials, newest first."""
    nodes = [
        node
        for node in prov.nodes(DECISION, source=model_source(name), limit=None)
        if node.payload.get("what") == PROMOTION
    ]
    newest_first = sorted(nodes, key=lambda node: (node.recorded, node.seq), reverse=True)
    return [
        {"id": node.id, "since": node.ts, "at": node.recorded, "by": node.actor, **node.payload}
        for node in newest_first[: max(0, limit)]
    ]
