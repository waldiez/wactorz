/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The arithmetic of a chart: ranges, scales, and the SVG paths a series is
 * drawn as. Nothing here holds state or listens to anything.
 */
import type { DrawMode, Point, Spread } from "./types";

export const SVG_NS = "http://www.w3.org/2000/svg";

/** A plot's drawing box. The SVG stretches to the plot's width; this is its coordinate system. */
export const CHART_W = 320;
export const CHART_H = 90;
export const CHART_PAD = 4;

/** Scales mapping a window of time and a range of values onto a box. */
export interface Scales {
    x: (t: number) => number;
    y: (v: number) => number;
}

/** The window and the range a chart's plot spans. */
export interface Frame {
    t0: number;
    t1: number;
    lo: number;
    hi: number;
}

export function scales(
    t0: number,
    t1: number,
    lo: number,
    hi: number,
    width: number,
    height: number,
    pad: number,
): Scales {
    const span = t1 - t0 || 1;
    // A flat line sits mid-height rather than on an edge.
    const range = hi - lo || Math.abs(hi) || 1;
    const low = hi === lo ? lo - range / 2 : lo;
    return {
        x: t => pad + ((t - t0) / span) * (width - 2 * pad),
        y: v => height - pad - ((v - low) / range) * (height - 2 * pad),
    };
}

/** The plot's scales for ``frame``. */
export function plotScales(frame: Frame): Scales {
    return scales(frame.t0, frame.t1, frame.lo, frame.hi, CHART_W, CHART_H, CHART_PAD);
}

/** The smallest and largest value across every series, or null when there is none. */
export function extent(series: readonly (readonly Point[])[]): [number, number] | null {
    let lo = Infinity;
    let hi = -Infinity;
    for (const points of series) {
        for (const { v } of points) {
            if (v !== null) {
                lo = Math.min(lo, v);
                hi = Math.max(hi, v);
            }
        }
    }
    return lo === Infinity ? null : [lo, hi];
}

/** ``range`` widened to take in ``values`` too: thresholds, the edges of a spread. */
export function widened(range: [number, number] | null, values: readonly number[]): [number, number] | null {
    let found = range;
    for (const v of values) {
        found = found === null ? [v, v] : [Math.min(found[0], v), Math.max(found[1], v)];
    }
    return found;
}

/**
 * The top of a chart's range. A flat series has no range of its own, so it is
 * given one above it: a run of zeros then lies on the baseline under a top
 * label of one, rather than floating mid-height between two zeros.
 */
export function ceiling(lo: number, hi: number): number {
    if (hi > lo) {
        return hi;
    }
    return lo === 0 ? 1 : lo + Math.abs(lo) * 0.2;
}

/** The last value that is not a gap, or null. */
export function lastValue(points: readonly Point[]): number | null {
    for (let i = points.length - 1; i >= 0; i--) {
        const v = (points[i] as Point).v;
        if (v !== null) {
            return v;
        }
    }
    return null;
}

/**
 * At most ``max`` points, each the mean of the points in its slice of time.
 *
 * A week at one a minute is ten thousand points; a chart a few hundred pixels
 * wide shows a few hundred. A slice with no values stays a gap.
 */
export function downsample(points: readonly Point[], max: number): Point[] {
    if (points.length <= max || max < 1) {
        return [...points];
    }
    const size = points.length / max;
    const out: Point[] = [];
    for (let b = 0; b < max; b++) {
        const slice = points.slice(Math.floor(b * size), Math.floor((b + 1) * size));
        const values = slice.map(p => p.v).filter((v): v is number => v !== null);
        const last = slice[slice.length - 1] as Point;
        out.push({
            t: last.t,
            v: values.length ? values.reduce((a, v) => a + v, 0) / values.length : null,
        });
    }
    return out;
}

/**
 * An SVG path through ``points``, broken at every gap.
 *
 * ``x`` and ``y`` map a point to the drawing; a lone point between two gaps
 * is drawn as a dot-length stroke so it is not lost.
 */
export function pathData(
    points: readonly Point[],
    x: (t: number) => number,
    y: (v: number) => number,
): string {
    const parts: string[] = [];
    let open = false;
    for (const p of points) {
        if (p.v === null) {
            open = false;
            continue;
        }
        const px = x(p.t).toFixed(1);
        const py = y(p.v).toFixed(1);
        parts.push(open ? `L${px} ${py}` : `M${px} ${py}h0.1`);
        open = true;
    }
    return parts.join(" ");
}

/** ``points`` without their gaps, so a path through them joins across each. */
function joined(points: readonly Point[]): Point[] {
    return points.filter(p => p.v !== null);
}

/** Every point on its own, so a path through them is a row of dots. */
function apart(points: readonly Point[]): Point[] {
    return points.flatMap(p => [p, { t: p.t, v: null }]);
}

/** The path ``points`` are drawn as in ``mode``. */
export function modePath(points: readonly Point[], mode: DrawMode, scale: Scales): string {
    const shaped = mode === "joined" ? joined(points) : mode === "points" ? apart(points) : points;
    return pathData(shaped, scale.x, scale.y);
}

/**
 * ``spread`` cut where a step between neighbours is well over the usual one:
 * a stretch with nothing in it, which the band must not bridge.
 */
export function spreadRuns(spread: readonly Spread[]): Spread[][] {
    const steps = spread.slice(1).map((s, i) => s.t - (spread[i] as Spread).t);
    const usual = steps.length ? [...steps].sort((a, b) => a - b)[Math.floor(steps.length / 2)] : 0;
    const runs: Spread[][] = [];
    spread.forEach((s, i) => {
        const step = steps[i - 1];
        if (i === 0 || step === undefined || (usual !== undefined && step > usual * 1.5)) {
            runs.push([]);
        }
        (runs[runs.length - 1] as Spread[]).push(s);
    });
    return runs;
}

/** The band between each point's least and most, as one closed shape per run of points. */
export function spreadPath(spread: readonly Spread[], scale: Scales): string {
    return spreadRuns(spread)
        .map(run => {
            const top = run.map(s => `${scale.x(s.t).toFixed(1)} ${scale.y(s.hi).toFixed(1)}`);
            const bottom = [...run]
                .reverse()
                .map(s => `${scale.x(s.t).toFixed(1)} ${scale.y(s.lo).toFixed(1)}`);
            return `M${top.join(" L")} L${bottom.join(" L")} Z`;
        })
        .join(" ");
}

/** The point of ``points`` nearest in time to ``t``, or undefined when there are none. */
export function nearest(points: readonly Point[], t: number): Point | undefined {
    let best: Point | undefined;
    for (const p of points) {
        if (!best || Math.abs(p.t - t) < Math.abs(best.t - t)) {
            best = p;
        }
    }
    return best;
}

/** A time of day, or a day and a time for a window longer than one. */
export function timeLabel(t: number, spanMs: number): string {
    const d = new Date(t);
    const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    if (spanMs <= 24 * 3600_000) {
        return hm;
    }
    return `${d.getDate()}/${d.getMonth() + 1} ${hm}`;
}

export function svg(width: number, height: number, label: string): SVGSVGElement {
    const root = document.createElementNS(SVG_NS, "svg");
    root.setAttribute("viewBox", `0 0 ${width} ${height}`);
    root.setAttribute("preserveAspectRatio", "none");
    root.setAttribute("role", "img");
    root.setAttribute("aria-label", label);
    return root;
}

export function line(d: string, color: string, cls: string): SVGPathElement {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("class", cls);
    path.setAttribute("stroke", color);
    return path;
}
