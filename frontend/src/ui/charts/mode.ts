/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * How a chart draws its points, and the control that switches it.
 *
 * A line broken at each gap is honest about dense data and turns sparse data
 * into stray dots; points are honest about sparse data and noisy for dense.
 * Left to choose, a chart draws as points when most of its slots are empty, and
 * joins across lone empty slots that only say there were more slots than readings.
 */
import { button, el } from "../dom";
import type { DrawMode, Point } from "./types";

/** The modes offered, in the order they are offered, with their words. */
export const MODES: readonly [DrawMode, string][] = [
    ["line", "Line"],
    ["points", "Points"],
    ["joined", "Joined"],
];

/** Under this share of slots holding a value, `auto` draws points. */
export const SPARSE_BELOW = 1 / 3;

/** Over this share of slots being lone gaps, with no longer run, `auto` joins across them. */
export const LONE_GAPS_OVER = 1 / 5;

/** How many of ``points``' empty slots stand alone between two values, and whether any run is longer. */
function gapRuns(points: readonly Point[]): { lone: number; longer: boolean } {
    let lone = 0;
    let longer = false;
    let run = 0;
    let seen = false;
    for (const p of points) {
        if (p.v === null) {
            run += 1;
            continue;
        }
        if (seen && run === 1) {
            lone += 1;
        } else if (seen && run > 1) {
            longer = true;
        }
        run = 0;
        seen = true;
    }
    return { lone, longer };
}

/**
 * The mode `auto` comes to for ``series``: points when most slots are empty;
 * joined when the empty slots are lone ones between values, many of them and
 * no longer run, which is more slots than readings rather than a stretch
 * without any; a line otherwise.
 */
export function autoMode(series: readonly (readonly Point[])[]): DrawMode {
    const all = series.flat();
    if (all.length === 0) {
        return "line";
    }
    const filled = all.filter(p => p.v !== null).length;
    if (filled / all.length < SPARSE_BELOW) {
        return "points";
    }
    const runs = series.map(gapRuns);
    const lone = runs.reduce((sum, r) => sum + r.lone, 0);
    const aliased = lone / all.length > LONE_GAPS_OVER && !runs.some(r => r.longer);
    return aliased ? "joined" : "line";
}

/**
 * A radio group of the modes, ``current`` checked; choosing one calls ``choose``.
 * Arrow keys move between them as in any radio group.
 */
export function modeToggle(current: DrawMode, choose: (mode: DrawMode) => void): HTMLElement {
    const group = el("div", "af-chart-modes");
    group.setAttribute("role", "radiogroup");
    group.setAttribute("aria-label", "Draw as");
    const options = MODES.map(([mode, words]) => {
        const option = button("af-chart-mode", words);
        option.setAttribute("role", "radio");
        option.setAttribute("aria-checked", String(mode === current));
        option.tabIndex = mode === current ? 0 : -1;
        option.addEventListener("click", () => choose(mode));
        return option;
    });
    group.addEventListener("keydown", e => {
        const step =
            e.key === "ArrowRight" || e.key === "ArrowDown"
                ? 1
                : e.key === "ArrowLeft" || e.key === "ArrowUp"
                  ? -1
                  : 0;
        if (step === 0) {
            return;
        }
        e.preventDefault();
        const at = MODES.findIndex(([mode]) => mode === current);
        const next = MODES[(at + step + MODES.length) % MODES.length] as [DrawMode, string];
        choose(next[0]);
    });
    group.append(...options);
    return group;
}
