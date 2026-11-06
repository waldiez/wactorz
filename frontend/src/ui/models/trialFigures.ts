/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The pieces every kind's judged trial is drawn with: how a figure reads, and a
 * box of figures with a column for each side.
 */
import type { RateInterval, TrialSide } from "../../types/models";
import { el } from "../dom";

/** What no value reads as. */
export const NONE = "–";

/** The p below which the server calls a difference real. */
export const SIGNIFICANT = 0.05;

/** A rate or a score between 0 and 1. */
export function rate(value: number | null): string {
    return value === null ? NONE : value.toFixed(2);
}

/** A rate's interval, `0.40–0.89`; empty with none. */
export function span(interval: RateInterval): string {
    return interval === null ? "" : `${interval[0].toFixed(2)}–${interval[1].toFixed(2)}`;
}

export function count(value: number): string {
    return value.toLocaleString();
}

/** A test's p as the verdicts say it: `p = 0.021`, or `p < 0.001` where three decimals would read as zero. */
export function pValue(p: number): string {
    return p < 0.001 ? "p < 0.001" : `p = ${p.toFixed(3)}`;
}

/** `1 input`, `3 inputs`. */
export function inputs(value: number): string {
    return `${count(value)} input${value === 1 ? "" : "s"}`;
}

/** A quantity in the model's own unit, with as many decimals as its size needs. */
export function amount(value: number | null, signed = false): string {
    if (value === null) {
        return NONE;
    }
    if (value === 0) {
        return "0";
    }
    const size = Math.abs(value);
    const digits = size >= 100 ? 0 : size >= 1 ? 2 : 3;
    const shown = value.toLocaleString(undefined, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
    });
    if (!signed) {
        return shown;
    }
    // A sign either way, the minus as a minus rather than a hyphen.
    return value > 0 ? `+${shown}` : shown.replace("-", "−");
}

/** One figure for both sides: a cell each, with its interval under it when it has one. */
export interface Row<S> {
    name: string;
    /** What the figure means, for a reader who has not met it. */
    hint: string;
    value(side: S): string;
    interval?(side: S): RateInterval;
}

function cell<S>(row: Row<S>, side: S): HTMLElement {
    const shown = el("td", "", row.value(side));
    const interval = row.interval ? span(row.interval(side)) : "";
    if (interval) {
        shown.appendChild(el("span", "af-trial-interval", interval));
    }
    return shown;
}

/** A table under a caption, framed as one box. */
export function framed(table: HTMLElement): HTMLElement {
    const shown = el("div", "af-trial-judged-box");
    shown.appendChild(table);
    return shown;
}

/** A table with `title` as its caption and a header row of `columns`. */
export function captioned(
    title: string,
    columns: readonly string[],
): { table: HTMLElement; body: HTMLElement } {
    const table = el("table", "af-trial-judged-table");
    table.appendChild(el("caption", "", title));
    const head = el("tr");
    for (const column of columns) {
        head.appendChild(el("th", "", column));
    }
    table.appendChild(el("thead")).appendChild(head);
    return { table, body: table.appendChild(el("tbody")) };
}

/** A header cell naming a row, with what it means on hover. */
export function rowHeader(name: string, hint = ""): HTMLElement {
    const shown = el("th", "", name);
    shown.scope = "row";
    if (hint) {
        shown.title = hint;
    }
    return shown;
}

/** One box of figures: a table of `rows`, a column for each side. */
export function box<S>(judged: TrialSide<S>, title: string, rows: readonly Row<S>[]): HTMLElement {
    const { table, body } = captioned(title, ["Figure", "Active", "Shadow"]);
    for (const row of rows) {
        const line = el("tr");
        line.append(rowHeader(row.name, row.hint), cell(row, judged.active), cell(row, judged.shadow));
        body.appendChild(line);
    }
    return framed(table);
}

/** Boxes side by side where the width allows, one under the other where it does not. */
export function grid(...boxes: HTMLElement[]): HTMLElement {
    const shown = el("div", "af-trial-judged-grid");
    shown.append(...boxes);
    return shown;
}
