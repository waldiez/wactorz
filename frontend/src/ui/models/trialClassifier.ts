/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A classifier's trial judged against the labels (`judge_classifier` in
 * `wactorz/core/judges.py`): accuracy and the F1 means per side, each class's
 * precision, recall and F1, and the confusion of the labels against the
 * answers. A labelled input a side gave no class for is wrong and counted as
 * no answer, so a model that answers only when it is sure is not flattered.
 */
import type { ClassifierJudged, ClassifierSide } from "../../types/models";
import { el } from "../dom";
import { box, captioned, count, framed, grid, rate, rowHeader, type Row } from "./trialFigures";

const OVERALL: readonly Row<ClassifierSide>[] = [
    {
        name: "Accuracy",
        hint: "of the labelled inputs, the share it named the labelled class for",
        value: side => rate(side.accuracy),
        interval: side => side.accuracy_interval,
    },
    {
        name: "Macro F1",
        hint: "each class's F1, every class counting the same",
        value: side => rate(side.macro_f1),
    },
    {
        name: "Weighted F1",
        hint: "each class's F1, weighted by how many inputs held it",
        value: side => rate(side.weighted_f1),
    },
    {
        name: "No answer",
        hint: "labelled inputs it gave no class for; each counts as wrong",
        value: side => count(side.no_answer),
    },
];

/** `0.75 · 1.00 · 0.86`: one side's precision, recall and F1 for a class. */
function triple(side: ClassifierSide, name: string): string {
    const figures = side.per_class[name];
    if (!figures) {
        return "–";
    }
    return [figures.precision, figures.recall, figures.f1].map(rate).join(" · ");
}

/** Each class: how many inputs held it, and its precision, recall and F1 on each side. */
function perClass(judged: ClassifierJudged): HTMLElement {
    const { table, body } = captioned("Per class", [
        "Class",
        "Inputs",
        "Active P · R · F1",
        "Shadow P · R · F1",
    ]);
    table.classList.add("af-trial-columns");
    for (const name of judged.classes) {
        const line = el("tr");
        line.append(
            rowHeader(name),
            el("td", "", count(judged.active.per_class[name]?.support ?? 0)),
            el("td", "", triple(judged.active, name)),
            el("td", "", triple(judged.shadow, name)),
        );
        body.appendChild(line);
    }
    return framed(table);
}

/** One side's confusion: a row per class labelled, a column per class answered, then no answer. */
function confusion(judged: ClassifierJudged, side: "active" | "shadow"): HTMLElement {
    const title = side === "active" ? "Active: labelled ▸ answered" : "Shadow: labelled ▸ answered";
    const { table, body } = captioned(title, ["Labelled", ...judged.classes, "No answer"]);
    table.classList.add("af-trial-columns", "af-trial-confusion");
    judged.classes.forEach((name, index) => {
        const line = el("tr");
        line.appendChild(rowHeader(name));
        for (const [column, value] of (judged[side].confusion[index] ?? []).entries()) {
            const shown = el("td", "", count(value));
            if (column === index) {
                shown.classList.add("af-trial-confusion-right");
            }
            line.appendChild(shown);
        }
        body.appendChild(line);
    });
    return framed(table);
}

/** A classifier's figures: overall, per class, and the confusion of each side. */
export function classifierBoxes(judged: ClassifierJudged): HTMLElement[] {
    return [
        grid(box(judged, "Overall", OVERALL), perClass(judged)),
        grid(confusion(judged, "active"), confusion(judged, "shadow")),
    ];
}

/** What the figures rest on beyond the labels: the classes seen. */
export function classifierBasis(judged: ClassifierJudged): string {
    return `${count(judged.classes.length)} class${judged.classes.length === 1 ? "" : "es"}`;
}
