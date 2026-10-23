/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Trends drawn from the metrics history: the arithmetic that turns stored
 * samples into points, and the two SVG figures drawn from them — a sparkline
 * for a card and a line chart for the detail panel.
 *
 * Samples arrive about once a minute. A counter such as messages processed is
 * drawn as a rate per minute, and a stretch with no samples is drawn as a gap
 * rather than a line across it: an agent that was stopped for an hour did not
 * do anything during it, and a straight line would say it did a little.
 */
import { el } from "../dom";

/** One point of a trend: a time in milliseconds and a value, or null for a gap. */
export interface Point {
    /** Milliseconds since the epoch. */
    t: number;
    /** The value, or null where nothing was measured. */
    v: number | null;
}

/** One line of a chart: what it is called, the color it is drawn in, and its points. */
export interface Series {
    /** The name a legend and a tooltip give it. */
    name: string;
    /** A CSS color for its line. */
    color: string;
    /** Its points, oldest first. */
    points: Point[];
}

/** A stored sample as the history endpoints serve it: seconds since the epoch, then a value. */
export type Sample = [number, number | null];

/** How far apart two samples may be before the time between them is a gap: a
 *  few missed minutes, not one late write. */
export const GAP_S = 180;

/** The series colors, in the order series are given them. Validated against
 *  the dashboard's dark surface for contrast and color-blind separation. */
export const SERIES_COLORS = ["#3987e5", "#d95926", "#199e70"] as const;

const SVG_NS = "http://www.w3.org/2000/svg";

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

/** Scales mapping a window of time and a range of values onto a box. */
interface Scales {
    x: (t: number) => number;
    y: (v: number) => number;
}

function scales(
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

function svg(width: number, height: number, label: string): SVGSVGElement {
    const root = document.createElementNS(SVG_NS, "svg");
    root.setAttribute("viewBox", `0 0 ${width} ${height}`);
    root.setAttribute("preserveAspectRatio", "none");
    root.setAttribute("role", "img");
    root.setAttribute("aria-label", label);
    return root;
}

function line(d: string, color: string, cls: string): SVGPathElement {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("class", cls);
    path.setAttribute("stroke", color);
    return path;
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

/** How a line chart says its values: the unit it formats them in. */
export type Format = (v: number) => string;

/** What a line chart is told besides its series. */
export interface ChartOptions {
    /** Its title, which names it for a screen reader too. */
    title: string;
    /** Formats a value for the axis labels and the tooltip. */
    format: Format;
    /** Whether zero is always in view, as for a count or a rate. Default true. */
    fromZero?: boolean;
}

const CHART_W = 320;
const CHART_H = 90;
const CHART_PAD = 4;

/** A time of day, or a day and a time for a window longer than one. */
export function timeLabel(t: number, spanMs: number): string {
    const d = new Date(t);
    const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    if (spanMs <= 24 * 3600_000) {
        return hm;
    }
    return `${d.getDate()}/${d.getMonth() + 1} ${hm}`;
}

/**
 * A line chart of one or more series sharing one axis, with a crosshair and a
 * tooltip that lists every series at the time under the pointer.
 *
 * Series names reach the page as text, never as markup. With two or more
 * series a legend names each beside a short stroke of its color, so a line is
 * never told apart by color alone.
 */
export function buildLineChart(series: readonly Series[], options: ChartOptions): HTMLElement {
    const figure = el("figure", "af-chart");
    figure.appendChild(el("figcaption", "af-chart-title", options.title));
    const all = series.map(s => s.points);
    const range = extent(all);
    const times = all.flat().map(p => p.t);
    if (!range || times.length === 0) {
        figure.appendChild(el("div", "af-chart-empty", "No samples in this window"));
        return figure;
    }
    const t0 = Math.min(...times);
    const t1 = Math.max(...times);
    const lo = options.fromZero === false ? range[0] : Math.min(0, range[0]);
    figure.appendChild(buildPlot(series, options, { t0, t1, lo, hi: ceiling(lo, range[1]) }));
    if (series.length > 1) {
        figure.appendChild(buildLegend(series));
    }
    return figure;
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

/** The window and the range a chart's plot spans. */
interface Frame {
    t0: number;
    t1: number;
    lo: number;
    hi: number;
}

/** The hairline that follows the pointer across a chart, hidden until it does. */
function crosshair(): SVGLineElement {
    const cross = document.createElementNS(SVG_NS, "line");
    cross.setAttribute("class", "af-chart-crosshair");
    cross.setAttribute("y1", "0");
    cross.setAttribute("y2", String(CHART_H));
    cross.style.display = "none";
    return cross;
}

/** A chart's plot: its lines, the crosshair, the axis range and the tooltip. */
function buildPlot(series: readonly Series[], options: ChartOptions, frame: Frame): HTMLElement {
    const { x, y } = scales(frame.t0, frame.t1, frame.lo, frame.hi, CHART_W, CHART_H, CHART_PAD);
    const plot = el("div", "af-chart-plot");
    const root = svg(CHART_W, CHART_H, options.title);
    for (const s of series) {
        root.appendChild(line(pathData(s.points, x, y), s.color, "af-trend-line"));
    }
    const cross = crosshair();
    root.appendChild(cross);

    // The range in a gutter of its own, and the window's ends under the plot,
    // so no label is drawn over a line.
    const axis = el("div", "af-chart-axis");
    axis.append(el("span", "", options.format(frame.hi)), el("span", "", options.format(frame.lo)));
    const ends = el("div", "af-chart-times");
    const span = frame.t1 - frame.t0;
    ends.append(el("span", "", timeLabel(frame.t0, span)), el("span", "", timeLabel(frame.t1, span)));
    const tip = el("div", "af-chart-tip");
    tip.hidden = true;
    plot.append(axis, root, ends, tip);
    wireCrosshair(plot, root, cross, tip, series, options.format, frame.t0, frame.t1);
    return plot;
}

/** The legend: each series' name beside a short stroke of its color. */
export function buildLegend(series: readonly Series[]): HTMLElement {
    const legend = el("div", "af-chart-legend");
    for (const s of series) {
        const item = el("span", "af-chart-legend-item");
        const key = el("span", "af-chart-key");
        key.style.background = s.color;
        item.append(key, el("span", "", s.name));
        legend.appendChild(item);
    }
    return legend;
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

/** The rows a tooltip shows at time ``t``: each series' value there, or a dash for a gap. */
export function tooltipRows(
    series: readonly Series[],
    t: number,
    format: Format,
): [string, string, string][] {
    return series.map(s => {
        const p = nearest(s.points, t);
        return [s.name, s.color, p && p.v !== null ? format(p.v) : "—"];
    });
}

function wireCrosshair(
    plot: HTMLElement,
    root: SVGSVGElement,
    cross: SVGLineElement,
    tip: HTMLElement,
    series: readonly Series[],
    format: Format,
    t0: number,
    t1: number,
): void {
    const show = (fraction: number): void => {
        const clamped = Math.max(0, Math.min(1, fraction));
        const t = t0 + clamped * (t1 - t0);
        const px = CHART_PAD + clamped * (CHART_W - 2 * CHART_PAD);
        cross.setAttribute("x1", px.toFixed(1));
        cross.setAttribute("x2", px.toFixed(1));
        cross.style.display = "";
        tip.replaceChildren(el("div", "af-chart-tip-time", timeLabel(t, t1 - t0)));
        for (const [name, color, value] of tooltipRows(series, t, format)) {
            const row = el("div", "af-chart-tip-row");
            const key = el("span", "af-chart-key");
            key.style.background = color;
            row.append(key, el("strong", "", value), el("span", "", name));
            tip.appendChild(row);
        }
        // Over the crosshair, measured from the plot, which the gutter widens.
        const box = root.getBoundingClientRect();
        const offset = box.left - plot.getBoundingClientRect().left;
        tip.style.left = `${(offset + clamped * box.width).toFixed(1)}px`;
        tip.hidden = false;
    };
    const hide = (): void => {
        cross.style.display = "none";
        tip.hidden = true;
    };
    plot.addEventListener("pointermove", e => {
        const box = root.getBoundingClientRect();
        show(box.width > 0 ? (e.clientX - box.left) / box.width : 1);
    });
    plot.addEventListener("pointerleave", hide);
    // The same readout from the keyboard: focus shows the latest time.
    plot.tabIndex = 0;
    plot.addEventListener("focus", () => show(1));
    plot.addEventListener("blur", hide);
}
