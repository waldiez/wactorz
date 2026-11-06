/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Trends drawn from the metrics history: the arithmetic that turns stored
 * samples into points, and the sparkline on each card. The detail panel's
 * charts are the dashboard's line chart (`../charts`).
 *
 * Samples arrive about once a minute. A counter such as messages processed is
 * drawn as a rate per minute, and a stretch with no samples is drawn as a gap
 * rather than a line across it: an agent that was stopped for an hour did not
 * do anything during it, and a straight line would say it did a little.
 */
import type { Sample } from "../../types/agent";
import { SERIES_COLORS, extent, type Point } from "../charts";
import { line, pathData, scales, svg } from "../charts/geometry";

export type { Sample };

/** How far apart two samples may be before the time between them is a gap: a
 *  few missed minutes, not one late write. */
export const GAP_S = 180;

/** The ``[ts, value]`` pairs of one field of history samples, values that are not numbers as null. */
export function samplesOf(rows: readonly Record<string, unknown>[], field: string): Sample[] {
    return rows.map(row => {
        const v = row[field];
        return [Number(row["ts"]), typeof v === "number" && Number.isFinite(v) ? v : null];
    });
}

/** Samples as points, unchanged: for a level such as memory or a temperature. */
export function levels(samples: readonly Sample[]): Point[] {
    return withGaps(samples.map(([ts, v]) => ({ t: ts * 1000, v })));
}

/**
 * A counter's samples as its rate per minute, one point per later sample.
 *
 * A counter that went down was restarted, so the step across it is a gap
 * rather than a negative rate; so is a step across a gap in the samples.
 */
export function perMinute(samples: readonly Sample[]): Point[] {
    const points: Point[] = [];
    for (let i = 1; i < samples.length; i++) {
        const [t0, v0] = samples[i - 1] as Sample;
        const [t1, v1] = samples[i] as Sample;
        const dt = t1 - t0;
        const usable = v0 !== null && v1 !== null && v1 >= v0 && dt > 0 && dt <= GAP_S;
        points.push({ t: t1 * 1000, v: usable ? ((v1 - v0) / dt) * 60 : null });
    }
    return points;
}

/** ``points`` with a null inserted wherever two neighbours are further apart than `GAP_S`. */
export function withGaps(points: readonly Point[]): Point[] {
    const out: Point[] = [];
    points.forEach((p, i) => {
        const prev = points[i - 1];
        if (prev && p.t - prev.t > GAP_S * 1000) {
            out.push({ t: prev.t + 1, v: null });
        }
        out.push(p);
    });
    return out;
}

/**
 * A sparkline: one series, no axes, drawn to fill its box.
 *
 * ``label`` is what a screen reader says for it. Nothing is drawn when there
 * is no value at all, so an empty sparkline reads as nothing rather than as zero.
 */
export function buildSparkline(
    points: readonly Point[],
    label: string,
    color: string = SERIES_COLORS[0],
): SVGSVGElement {
    const width = 100;
    const height = 24;
    const root = svg(width, height, label);
    root.classList.add("af-sparkline");
    const range = extent([points]);
    const first = points[0];
    const last = points[points.length - 1];
    if (!range || !first || !last) {
        return root;
    }
    const { x, y } = scales(first.t, last.t, Math.min(0, range[0]), range[1], width, height, 2);
    root.appendChild(line(pathData(points, x, y), color, "af-trend-line"));
    return root;
}
