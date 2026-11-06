/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * A regressor's trial judged against the labels (`judge_regressor` in
 * `wactorz/core/judges.py`): each side's errors from the labelled numbers, and
 * which side was nearer the truth more often, with a sign test saying whether
 * that could be chance.
 */
import type { RegressorJudged, RegressorSide } from "../../types/models";
import { amount, box, count, grid, inputs, SIGNIFICANT, type Row, pValue } from "./trialFigures";

const ERRORS: readonly Row<RegressorSide>[] = [
    {
        name: "Mean absolute error",
        hint: "how far its answer was from the truth, on average",
        value: side => amount(side.mae),
    },
    {
        name: "RMS error",
        hint: "the root mean square error: large misses count for more",
        value: side => amount(side.rmse),
    },
    {
        name: "Bias",
        hint: "the mean of the answer less the truth: above zero it reads high, below it low",
        value: side => amount(side.bias, true),
    },
    {
        name: "Worst error",
        hint: "the furthest any answer was from the truth",
        value: side => amount(side.worst),
    },
    {
        name: "Answered",
        hint: "labelled inputs it gave a number for",
        value: side => `${count(side.judged)} of ${count(side.judged + side.no_answer)}`,
    },
];

/** A regressor's figures. */
export function regressorBoxes(judged: RegressorJudged): HTMLElement[] {
    return [grid(box(judged, "Errors", ERRORS))];
}

/** Which side was nearer the truth more often, in words, and whether that could be chance. */
export function regressorVerdict(judged: RegressorJudged): string {
    const { active, shadow, ties, p, better } = judged.closer;
    if (active + shadow === 0) {
        return "Both sides were as near the truth on every input: nothing tells them apart yet.";
    }
    const tied = ties ? `, ${inputs(ties)} tied` : "";
    const counts = `active nearer on ${inputs(active)}, shadow on ${count(shadow)}${tied}; ${pValue(p)}`;
    if (better === "shadow") {
        return `The shadow is closer: shadow nearer on ${inputs(shadow)}, active on ${count(active)}${tied}; ${pValue(p)}.`;
    }
    if (better === "active") {
        return `The active version is closer: ${counts}.`;
    }
    const below = p < SIGNIFICANT ? "" : ", so the difference could be chance";
    return `No clear difference: ${counts}${below}.`;
}

/** What the figures rest on beyond the labels: the labels left out for not being numbers. */
export function regressorBasis(judged: RegressorJudged): string {
    const left = judged.not_numbers;
    return left ? `${count(left)} label${left === 1 ? "" : "s"} not a number, left out` : "";
}
