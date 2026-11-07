# SPDX-License-Identifier: Apache-2.0
"""Starting and ending a model's shadow trial in the agents that serve it.

A trial begins when a model's ``shadow`` pointer is set and ends when it is
cleared or promoted. The pointer watcher hands each such change here, as it
hands every change to the hot swap. For a set pointer, every function agent on
main serving the model by its bare name is given the version to try beside the
one it serves (`FunctionAgent.swap_shadow`); for a cleared one, it drops it.
Then the trial's pairs, kept under ``shadow/<model>`` in the data directory,
are read back and compared (`wactorz.core.shadow_trial`), and the result is
written as an ``outcome`` node about the model, whichever way the trial ended,
with one line on the feed saying what it showed.

While a shadow is set, main reads the trial on a cadence and sets its gauges,
``wactorz_model_score`` and ``wactorz_model_shadow_agreement``
(`wactorz.monitoring.model_metrics`); when its name carries a policy
(`wactorz.core.promotion`) it holds the trial against the policy too. When every
term is met it writes a ``decision`` node and, as the policy says, promotes on
its own as ``policy`` or asks on the dashboard, whose answer promotes or clears
the shadow; a trial that falls short is left running. ``docs/design/
promotion-policy.md`` is the decision.

Agents on nodes run no shadow yet: their pairs would be kept where main
cannot read them, until the data package ships files to main. The hot swap
already moves the served version when ``active`` moves, promotion included;
this only ever touches the shadow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ...config import POLICY_CHECK_S
from ...core.artifacts import ArtifactError, ArtifactStore, Pointer
from ...core.judges import labels_for
from ...core.labels import Labels
from ...core.paths import resolve_state_dir
from ...core.persistence.stores import get_db
from ...core.pointer_moves import record_move
from ...core.promotion import (
    POLICY,
    Policy,
    Verdict,
    decision_id,
    judge_trial,
    record_decision,
)
from ...core.provenance import LED_TO, Provenance
from ...core.shadow_trial import (
    CLEARED,
    PROMOTED,
    compare,
    judgement_of_trial,
    record_outcome,
)
from ...data.files import data_root
from ...monitoring import model_metrics
from ..shadow_serving import shadow_source

if TYPE_CHECKING:
    from ...core.persistence import WactorzDB
    from .artifact_service import Change
    from .hosts import SwapHost

logger: logging.Logger = logging.getLogger(__name__)

#: The pointer this follows.
SHADOW = "shadow"

#: How long after starting main first holds the trials against their
#: policies: a restart need not begin by reading every pair. Then every
#: `POLICY_CHECK_S` (``WACTORZ_POLICY_CHECK_S``): a trial is days long, so a
#: check every few minutes is plenty; a test sets it short.
FIRST_POLICY_CHECK_AFTER_S = 60.0

#: The answers the operator is offered when a trial meets its policy and the
#: policy asks rather than promotes.
PROMOTE = "Promote"
CLEAR = "Clear the shadow"
KEEP = "Keep trying"

#: Who a move the operator's answer makes is recorded as.
OPERATOR = "operator"

#: What asks the operator: `Actor.ask_operator`, or a stand-in with its signature.
Asker = Callable[..., Awaitable[str]]

#: One trial under one policy: the model, the version on trial, when it was
#: set, and when the policy was. A policy set anew is decided afresh.
Trial = tuple[str, int, float, float]


def _following_active(actor: Any, name: str) -> list[str]:
    """The keys under which ``actor`` serves ``name`` by following its ``active`` pointer."""
    artifacts = getattr(actor, "artifacts", None)
    if not isinstance(artifacts, dict):
        return []
    return [
        key
        for key, entry in artifacts.items()
        if entry.get("name") == name and entry.get("follows") == "active"
    ]


class ShadowServing:
    """Gives the agents serving a model its shadow version, and records what the trial showed."""

    def __init__(self, host: SwapHost | None = None, *, ask: Asker | None = None) -> None:
        self.host = host
        #: What puts a promotion to the operator; None asks nobody.
        self._ask = ask
        #: Whether each trial met its policy at the last check, so a verdict
        #: is said and recorded when it changes, not at every check.
        self._verdicts: dict[Trial, bool] = {}
        #: The trials the operator has been asked about, each once.
        self._asked: set[Trial] = set()
        self._questions: set[asyncio.Task[Any]] = set()

    @property
    def store(self) -> ArtifactStore:
        """The store, read where the state directory is at the time of asking."""
        return ArtifactStore.in_state_dir(resolve_state_dir())

    async def follow(self, changes: list[Change]) -> None:
        """Start or end a trial in the serving agents, for each ``shadow`` pointer that changed."""
        host = self.host
        if host is None:
            return
        for name, which, now, was in changes:
            if which != SHADOW:
                continue
            try:
                entry = await self._shadow_entry(name) if now is not None else None
            except ArtifactError as exc:
                await self._say(f"{name}:shadow cannot be tried: {exc}")
                continue
            for actor in self._serving(name):
                for key in _following_active(actor, name):
                    try:
                        await actor.swap_shadow(key, entry)
                    except Exception as exc:
                        await self._say(f"{actor.name} cannot try {name}:shadow as {key}: {exc}")
            if now is None and was is not None:
                await self._ended(name, was)

    def _serving(self, name: str) -> list[Any]:
        """The agents here that serve ``name`` and can run a shadow beside it."""
        host = self.host
        if host is None or not host._registry:
            return []
        return [
            actor
            for actor in host._registry.all_actors()
            if callable(getattr(actor, "swap_shadow", None)) and _following_active(actor, name)
        ]

    async def _shadow_entry(self, name: str) -> dict[str, Any]:
        """``name:shadow`` pinned, with the path it is read at on main."""

        def resolve() -> dict[str, Any]:
            store = self.store
            pinned = store.pin(f"{name}:shadow")
            return {**pinned, "path": str(store.readable_path(pinned))}

        return await asyncio.to_thread(resolve)

    # ── The policy ────────────────────────────────────────────────────────

    async def policy_watcher(self) -> None:
        """Hold every trial against its policy on a cadence, until the actor stops."""
        host = self.host
        if host is None:
            return
        await asyncio.sleep(min(FIRST_POLICY_CHECK_AFTER_S, POLICY_CHECK_S))
        while host.state.value not in ("stopped", "failed"):
            try:
                await self.check_policies()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "[%s] cannot hold the trials against their policies: %s", host.name, exc
                )
            await asyncio.sleep(POLICY_CHECK_S)

    async def check_policies(self, now: float | None = None) -> dict[str, Verdict]:
        """One check of every trial under way: its gauges set, and, where its name has a policy, its verdict by name."""
        at = time.time() if now is None else now
        store = self.store
        verdicts: dict[str, Verdict] = {}
        for name in await asyncio.to_thread(store.names):
            shadow = await asyncio.to_thread(store.pointer, name, SHADOW)
            if shadow is None:
                continue
            policy = await asyncio.to_thread(store.policy, name)
            if policy is None:
                summary = await self._compare(name, shadow.moved_at, at, shadow=shadow.version)
                model_metrics.record_trial(name, summary)
                continue
            verdicts[name] = await self._check(name, shadow, policy, at)
        return verdicts

    async def _check(self, name: str, shadow: Pointer, policy: Policy, now: float) -> Verdict:
        """Hold ``name``'s trial against ``policy``; act when it has just come to be met."""
        summary = await self._compare(name, shadow.moved_at, now, shadow=shadow.version)
        verdict = judge_trial(summary, policy, since=shadow.moved_at, now=now)
        model_metrics.record_trial(name, summary, metric=verdict.metric)
        trial: Trial = (name, shadow.version, shadow.moved_at, policy.at)
        before = self._verdicts.get(trial)
        self._verdicts[trial] = verdict.met
        if before == verdict.met:
            return verdict
        active = await asyncio.to_thread(self.store.pointer, name, "active")
        if not verdict.met:
            if before:
                # Met at an earlier check and no longer: the decision then
                # stands, and this one records the change.
                await self._record_decision(name, shadow, active, verdict, now)
                await self._say(
                    f"{shadow.reference}'s trial no longer meets its policy: {verdict.words()}"
                )
            return verdict
        decided_before = await self._record_decision(name, shadow, active, verdict, now)
        if decided_before:
            # Decided in an earlier run of main: promoted, or asked, then.
            return verdict
        await self._say(f"{shadow.reference}'s trial meets its policy: {verdict.words()}")
        if policy.automatic:
            await self._promote(name, shadow, verdict, by=POLICY, reason=verdict.words())
        elif self._ask is not None and trial not in self._asked:
            self._asked.add(trial)
            task = asyncio.create_task(self._ask_and_act(name, shadow, verdict))
            self._questions.add(task)
            task.add_done_callback(self._questions.discard)
        return verdict

    async def _record_decision(
        self, name: str, shadow: Pointer, active: Pointer | None, verdict: Verdict, now: float
    ) -> bool:
        """Write the verdict as a decision; whether the same one was already there."""
        db = get_db()
        if db is None:
            return False
        prov = Provenance(db)
        node_id = decision_id(name, shadow.version, shadow.moved_at, verdict.met, verdict.policy.at)
        try:
            if await asyncio.to_thread(prov.get, node_id) is not None:
                return True
            await asyncio.to_thread(
                record_decision,
                prov,
                name=name,
                shadow_version=shadow.version,
                active_version=None if active is None else active.version,
                since=shadow.moved_at,
                verdict=verdict,
                at=now,
            )
        except Exception as exc:
            logger.warning("cannot record the policy's decision on %s: %s", name, exc)
        return False

    async def _ask_and_act(self, name: str, shadow: Pointer, verdict: Verdict) -> None:
        """Put the promotion to the operator, and do as the answer says while the trial stands."""
        if self._ask is None:
            return
        text = f"Promote {shadow.reference}? Its trial meets the policy: {verdict.words()}."
        try:
            answer = await self._ask(
                text,
                options=[PROMOTE, CLEAR, KEEP],
                context=f"The policy asks for {verdict.policy.words()}.",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("cannot ask about %s's promotion: %s", name, exc)
            return
        standing = await asyncio.to_thread(self.store.pointer, name, SHADOW)
        if standing is None or standing.version != shadow.version:
            await self._say(f"the answer on {shadow.reference} came after its trial ended")
            return
        reason = f"asked by the policy: {verdict.words()}"
        if answer == PROMOTE:
            await self._promote(name, shadow, verdict, by=OPERATOR, reason=reason)
        elif answer == CLEAR:
            try:
                await asyncio.to_thread(self.store.clear, SHADOW, name, by=OPERATOR, reason=reason)
            except ArtifactError as exc:
                await self._say(f"cannot clear {shadow.reference}: {exc}")

    async def _promote(
        self, name: str, shadow: Pointer, verdict: Verdict, *, by: str, reason: str
    ) -> None:
        """Make the shadow active, and join the move to the decision that led to it."""
        try:
            moved = await asyncio.to_thread(self.store.promote, name, by=by, reason=reason)
        except ArtifactError as exc:
            await self._say(f"cannot promote {shadow.reference}: {exc}")
            return
        await self._say(f"{moved.reference} is now active, by {by}: {reason}")
        db = get_db()
        if db is None:
            return
        prov = Provenance(db)
        decision = decision_id(name, shadow.version, shadow.moved_at, True, verdict.policy.at)
        try:
            move = await asyncio.to_thread(record_move, prov, moved)
            if await asyncio.to_thread(prov.get, decision) is not None:
                await asyncio.to_thread(prov.add_edge, decision, move.id, LED_TO)
        except Exception as exc:
            logger.warning("cannot join %s's promotion to its decision: %s", name, exc)

    # ── The end of a trial ────────────────────────────────────────────────

    async def _ended(self, name: str, was: Pointer) -> None:
        """Compare the trial's pairs and write its outcome; say what it showed."""
        until = time.time()
        model_metrics.forget_trial(name)
        active = await asyncio.to_thread(self.store.pointer, name, "active")
        promoted = active is not None and active.version == was.version
        ended = PROMOTED if promoted else CLEARED
        active_version = None
        if active is not None:
            active_version = active.previous if promoted else active.version
        by = active.by if promoted and active is not None else ""
        summary = await self._compare(name, was.moved_at, until, shadow=was.version)
        pairs = summary.get("pairs", 0)
        agreement = summary.get("agreement")
        shown = f"{pairs} pair(s)" + (
            f", agreed on {agreement:.0%}" if isinstance(agreement, float) else ""
        )
        await self._say(f"the trial of {was.reference} ended ({ended}): {shown}")
        db = get_db()
        if db is None:
            return
        try:
            await asyncio.to_thread(
                record_outcome,
                Provenance(db),
                name=name,
                shadow_version=was.version,
                active_version=active_version,
                since=was.moved_at,
                until=until,
                ended=ended,
                summary=summary,
                by=by,
                shadow_sha256=was.sha256,
            )
        except Exception as exc:
            logger.warning("cannot record the outcome of %s's trial: %s", name, exc)

    async def _compare(
        self, name: str, since: float, until: float, *, shadow: int | None = None
    ) -> dict[str, Any]:
        """What the pairs of ``name``'s trial over ``[since, until)`` show; counts alone without pyarrow.

        Judged against the labels as the version on trial, ``shadow``, else the
        active one declares, when persistence is running to read them.
        """
        try:
            from ...data import parquet  # optional dependency: the `data` extra
        except ModuleNotFoundError:
            return {**compare([]), "pairs_unavailable": "install wactorz[data]"}
        root = data_root(resolve_state_dir())
        try:
            rows = await asyncio.to_thread(
                parquet.rows_arrived_in, root, shadow_source(name), since, until
            )
        except Exception as exc:
            logger.warning("cannot read the pairs of %s's trial: %s", name, exc)
            return {**compare([]), "pairs_unavailable": str(exc)}
        try:
            return await asyncio.to_thread(_judged, self.store, name, rows, shadow, get_db())
        except Exception as exc:
            logger.warning("cannot judge %s's trial against the labels: %s", name, exc)
            return compare(rows)

    async def _say(self, message: str) -> None:
        """One line on the feed, as a pointer's move is one."""
        host = self.host
        if host is None:
            return
        logger.info("[%s] %s", host.name, message)
        await host._mqtt_publish(
            f"agents/{host.actor_id}/logs",
            {"type": "model", "message": message, "timestamp": time.time()},
        )


def _judged(
    store: ArtifactStore,
    name: str,
    rows: list[dict[str, Any]],
    shadow: int | None,
    db: WactorzDB | None,
) -> dict[str, Any]:
    """The pairs compared as a version says it is judged, and against the labels when ``db`` holds them.

    The declaration decides what agreeing means as well as how the labels are
    read, so it is found even without the labels, and a trial agrees the same
    whether or not persistence is running.
    """
    found = judgement_of_trial(store, name, rows, shadow=shadow)
    if found is None:
        return compare(rows)
    judgement, judged_from = found
    if db is None:
        return compare(rows, judgement=judgement)
    labels = labels_for(rows, judgement, Labels(db).effective)
    summary = compare(rows, judgement=judgement, labels=labels)
    summary["labels"]["from"] = judged_from
    return summary
