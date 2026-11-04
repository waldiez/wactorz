/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A shadow trial judged against the labels: whether each side was right, as the
 * version on trial declares it is judged (`wactorz/core/judges.py`).
 *
 * Each kind has its figures: a detector's here, a classifier's and a
 * regressor's in their own files. Every rate is shown with its 95% interval,
 * so a recall of 1.0 from three faults reads as the uncertain thing it is; and
 * the verdict says whether the shadow is really better or the difference could
 * be chance. A trial nothing says how to judge shows how to say it, since the
 * figures it would have are the reason to promote or not.
 */
import type {
    DetectorJudged,
    DetectorSide,
    Judgement,
    McNemar,
    ShadowTrial,
    TrialJudged,
} from "../../types/models";
import { el, labelled } from "../dom";
import { classifierBasis, classifierBoxes } from "./trialClassifier";
import { box, count, grid, NONE, rate, SIGNIFICANT, type Row, pValue } from "./trialFigures";
import { regressorBasis, regressorBoxes, regressorVerdict } from "./trialRegressor";

/** A delay as a person reads it: seconds, minutes or hours. */
export function duration(seconds: number | null): string {
    if (seconds === null) {
        return NONE;
    }
    if (seconds < 120) {
        return `${seconds.toFixed(0)} s`;
    }
    if (seconds < 7200) {
        return `${(seconds / 60).toFixed(0)} min`;
    }
    return `${(seconds / 3600).toFixed(1)} h`;
}

/**
 * What a version is judged on, as a phrase: `fault, bearing labels of each input's source`, or
 * `appliance as a classifier, labels of meter`.
 */
export function judgedOn(judgement: Judgement): string {
    const where = judgement.source ? `of ${judgement.source}` : "of each input's source";
    if (judgement.kind !== "detector" && judgement.field) {
        return `${judgement.field} as a ${judgement.kind}, labels ${where}`;
    }
    return `${judgement.positive.join(", ")} labels ${where}`;
}

/** The figures per input: whether each answer was right. */
const PER_INPUT: readonly Row<DetectorSide>[] = [
    {
        name: "Precision",
        hint: "of the inputs it flagged, the share that were faults",
        value: side => rate(side.precision),
        interval: side => side.intervals.precision,
    },
    {
        name: "Recall",
        hint: "of the fault inputs, the share it flagged",
        value: side => rate(side.recall),
        interval: side => side.intervals.recall,
    },
    { name: "F1", hint: "precision and recall in one number", value: side => rate(side.f1) },
    {
        name: "False-positive rate",
        hint: "of the normal inputs, the share it flagged",
        value: side => rate(side.false_positive_rate),
        interval: side => side.intervals.false_positive_rate,
    },
    { name: "MCC", hint: "from −1 to 1, honest when faults are rare", value: side => rate(side.mcc) },
];

/** The figures an operator lives with: the faults caught and how soon, and the alarms raised for nothing. */
const PER_FAULT_AND_DAY: readonly Row<DetectorSide>[] = [
    {
        name: "Faults caught",
        hint: "a fault is caught when anything inside it was flagged",
        value: side => `${count(side.faults.caught)} of ${count(side.faults.seen)}`,
        interval: side => side.faults.recall_interval,
    },
    {
        name: "Detection delay",
        hint: "mean time from a fault's start to the first flag",
        value: side => duration(side.faults.delay_s.mean),
    },
    {
        name: "False alarms per day",
        hint: "alarms where no known fault was",
        // Under an hour of event time a rate per day would be noise: the count says what happened.
        value: side =>
            side.alarms_per_day === null
                ? `${count(side.alarms_without_fault)} in the trial`
                : side.alarms_per_day.toFixed(1),
    },
];

/** Whether the sides really differ, in words: the side right alone more often first. */
export function verdict(judged: { mcnemar: McNemar }): string {
    const { active_only: active, shadow_only: shadow, p, better } = judged.mcnemar;
    if (active + shadow === 0) {
        return "Both sides were right on the same inputs: nothing tells them apart yet.";
    }
    const counts = `active right alone on ${count(active)} inputs, shadow on ${count(shadow)}; ${pValue(p)}`;
    if (better === "shadow") {
        return `The shadow is better: shadow right alone on ${count(shadow)} inputs, active on ${count(active)}; ${pValue(p)}.`;
    }
    if (better === "active") {
        return `The active version is better: ${counts}.`;
    }
    const below = p < SIGNIFICANT ? "" : ", so the difference could be chance";
    return `No clear difference: ${counts}${below}.`;
}

function detectorBasis(judged: DetectorJudged): string {
    const seen = judged.active.faults.seen;
    return `${count(seen)} fault${seen === 1 ? "" : "s"} seen`;
}

/** What one kind adds to the basis line, its boxes, and its verdict. */
interface KindView {
    basis: string;
    boxes: HTMLElement[];
    verdict: string;
    /** What reads under the basis when nothing it could judge was labelled. */
    empty: string;
}

const NO_LABEL = "No input of the trial has a label yet: answers to questions, or labels written, add them.";

function viewOf(judged: TrialJudged): KindView {
    switch (judged.kind) {
        case "classifier":
            return {
                basis: classifierBasis(judged),
                boxes: classifierBoxes(judged),
                verdict: verdict(judged),
                empty: NO_LABEL,
            };
        case "regressor":
            return {
                basis: regressorBasis(judged),
                boxes: regressorBoxes(judged),
                verdict: regressorVerdict(judged),
                empty: judged.not_numbers
                    ? "No label of the trial's inputs is a number: a regressor is judged against labels that are."
                    : NO_LABEL,
            };
        default:
            return {
                basis: detectorBasis(judged),
                boxes: [
                    grid(
                        box(judged, "Per input", PER_INPUT),
                        box(judged, "Per fault and per day", PER_FAULT_AND_DAY),
                    ),
                ],
                verdict: verdict(judged),
                empty: NO_LABEL,
            };
    }
}

/** What the figures rest on: the labels, how many inputs they covered, and who said how to judge. */
function basis(judged: TrialJudged, added: string): string {
    const from = judged.from === "request" ? "as asked" : `as ${judged.from} declares`;
    // A regressor's labels that are not numbers covered an input too, though it was not judged.
    const total =
        judged.labelled + judged.unlabelled + (judged.kind === "regressor" ? judged.not_numbers : 0);
    const covered = `${count(judged.labelled)} of ${count(total)} inputs labelled`;
    const parts = [`Against ${judgedOn(judged.judged_on)}, ${from}`, covered];
    if (added) {
        parts.push(added);
    }
    if (judged.kind !== "regressor") {
        parts.push("rates with their 95% interval under them");
    }
    return parts.join(" · ");
}

/** How to say what a model is judged on, for a trial nothing says it of. */
function howToJudge(name: string, version: number | null): HTMLElement {
    const reference = version === null ? name : `${name}:${version}`;
    const hint = el(
        "p",
        "af-model-none",
        "Not judged against the labels: nothing says which labels count. Say it with ",
    );
    hint.append(
        el("code", "", `wactorz-artifacts judge ${reference} --positive fault`),
        " or ",
        el("code", "", `/artifacts judge ${reference} fault`),
        "; for a model that answers with a class or a number, ",
        el("code", "", `wactorz-artifacts judge ${reference} --kind classifier --field <field>`),
        " (or ",
        el("code", "", "--kind regressor"),
        ").",
    );
    return hint;
}

/** A kind this dashboard has no figures for: say so rather than show the wrong ones. */
function unknownKind(kind: string): HTMLElement {
    return el("p", "af-model-none", `Judged as a ${kind}, which this dashboard cannot show yet.`);
}

/** The trial judged against the labels, why it is not, or how to have it judged. */
export function judgedSection(trial: ShadowTrial): HTMLElement {
    const section = labelled(el("section", "af-trial-judged"), "Against the labels");
    section.appendChild(el("h4", "", "Against the labels"));
    const judged = trial.labels;
    if (trial.labels_unavailable) {
        section.appendChild(
            el("p", "af-model-none", `The labels could not be read: ${trial.labels_unavailable}`),
        );
        return section;
    }
    if (!judged) {
        section.appendChild(howToJudge(trial.name, trial.shadow?.version ?? null));
        return section;
    }
    if (!["detector", "classifier", "regressor"].includes(judged.kind)) {
        section.appendChild(unknownKind(judged.kind));
        return section;
    }
    const view = viewOf(judged);
    section.appendChild(el("p", "af-trial-judged-basis", basis(judged, view.basis)));
    if (!judged.labelled) {
        section.appendChild(el("p", "af-model-none", view.empty));
        return section;
    }
    section.append(...view.boxes, el("p", "af-trial-verdict", view.verdict));
    return section;
}
