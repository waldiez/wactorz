/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A shadow trial judged against the labels: whether each side was right, as the
 * version on trial declares it is judged (`wactorz/core/judges.py`).
 *
 * Every rate is shown with its 95% interval, so a recall of 1.0 from three
 * faults reads as the uncertain thing it is; and the verdict says whether the
 * shadow is really better or the difference could be chance. A trial nothing
 * says how to judge shows how to say it, since the figures it would have are
 * the reason to promote or not.
 */
import type { DetectorSide, Judgement, RateInterval, ShadowTrial, TrialJudged } from "../../types/models";
import { el, labelled } from "../dom";

/** What no value reads as. */
const NONE = "–";

/** The p below which the server calls a difference real. */
const SIGNIFICANT = 0.05;

function rate(value: number | null): string {
    return value === null ? NONE : value.toFixed(2);
}

function span(interval: RateInterval): string {
    return interval === null ? "" : `${interval[0].toFixed(2)}–${interval[1].toFixed(2)}`;
}

function count(value: number): string {
    return value.toLocaleString();
}

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

/** What a version is judged on, as a phrase: `fault, bearing labels of each input's source`. */
export function judgedOn(judgement: Judgement): string {
    const where = judgement.source ? `of ${judgement.source}` : "of each input's source";
    return `${judgement.positive.join(", ")} labels ${where}`;
}

/** One figure for both sides: a cell each, with its interval under it when it has one. */
interface Row {
    name: string;
    /** What the figure means, for a reader who has not met it. */
    hint: string;
    value(side: DetectorSide): string;
    interval?(side: DetectorSide): RateInterval;
}

/** The figures per input: whether each answer was right. */
const PER_INPUT: readonly Row[] = [
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
const PER_FAULT_AND_DAY: readonly Row[] = [
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

function cell(row: Row, side: DetectorSide): HTMLElement {
    const shown = el("td", "", row.value(side));
    const interval = row.interval ? span(row.interval(side)) : "";
    if (interval) {
        shown.appendChild(el("span", "af-trial-interval", interval));
    }
    return shown;
}

/** One box of figures: a table of `rows`, a column for each side. */
function box(judged: TrialJudged, title: string, rows: readonly Row[]): HTMLElement {
    const shown = el("table", "af-trial-judged-table");
    shown.appendChild(el("caption", "", title));
    const head = el("tr");
    head.append(el("th", "", "Figure"), el("th", "", "Active"), el("th", "", "Shadow"));
    shown.appendChild(el("thead")).appendChild(head);
    const body = shown.appendChild(el("tbody"));
    for (const row of rows) {
        const line = el("tr");
        const name = el("th", "", row.name);
        name.scope = "row";
        name.title = row.hint;
        line.append(name, cell(row, judged.active), cell(row, judged.shadow));
        body.appendChild(line);
    }
    const framed = el("div", "af-trial-judged-box");
    framed.appendChild(shown);
    return framed;
}

/** The two boxes, side by side where the width allows and one under the other where it does not. */
function boxes(judged: TrialJudged): HTMLElement {
    const grid = el("div", "af-trial-judged-grid");
    grid.append(box(judged, "Per input", PER_INPUT), box(judged, "Per fault and per day", PER_FAULT_AND_DAY));
    return grid;
}

/** Whether the sides really differ, in words: the side right alone more often first. */
export function verdict(judged: TrialJudged): string {
    const { active_only: active, shadow_only: shadow, p, better } = judged.mcnemar;
    if (active + shadow === 0) {
        return "Both sides were right on the same inputs: nothing tells them apart yet.";
    }
    const counts = `active right alone on ${count(active)} inputs, shadow on ${count(shadow)}; p = ${p.toFixed(3)}`;
    if (better === "shadow") {
        return `The shadow is better: shadow right alone on ${count(shadow)} inputs, active on ${count(active)}; p = ${p.toFixed(3)}.`;
    }
    if (better === "active") {
        return `The active version is better: ${counts}.`;
    }
    const below = p < SIGNIFICANT ? "" : ", so the difference could be chance";
    return `No clear difference: ${counts}${below}.`;
}

/** What the figures rest on: the labels, how many inputs they covered, and who said how to judge. */
function basis(judged: TrialJudged): string {
    const from = judged.from === "request" ? "as asked" : `as ${judged.from} declares`;
    const covered = `${count(judged.labelled)} of ${count(judged.labelled + judged.unlabelled)} inputs labelled`;
    const faults = `${count(judged.active.faults.seen)} fault${judged.active.faults.seen === 1 ? "" : "s"} seen`;
    return `Against ${judgedOn(judged.judged_on)}, ${from} · ${covered} · ${faults} · rates with their 95% interval under them`;
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
        ".",
    );
    return hint;
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
    section.appendChild(el("p", "af-trial-judged-basis", basis(judged)));
    if (!judged.labelled) {
        section.appendChild(
            el(
                "p",
                "af-model-none",
                "No input of the trial has a label yet: answers to questions, or labels written, add them.",
            ),
        );
        return section;
    }
    section.append(boxes(judged), el("p", "af-trial-verdict", verdict(judged)));
    return section;
}
