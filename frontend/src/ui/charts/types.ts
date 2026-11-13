/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * What a chart is drawn from: its series, and what is marked on them.
 *
 * Every chart in the dashboard draws from these shapes, and so does a
 * package's tab through `host.chart`, so they are plain data: times in
 * milliseconds since the epoch, values as numbers, a gap as null.
 */

/** One point of a series: a time in milliseconds and a value, or null for a gap. */
export interface Point {
    /** Milliseconds since the epoch. */
    t: number;
    /** The value, or null where nothing was measured. */
    v: number | null;
}

/** The least and most of a stretch a point stands for, drawn as a band around the line. */
export interface Spread {
    /** Milliseconds since the epoch, as the point it belongs to. */
    t: number;
    /** The least value in the stretch. */
    lo: number;
    /** The most value in the stretch. */
    hi: number;
}

/** One line of a chart: what it is called, the color it is drawn in, and its points. */
export interface Series {
    /** The name a legend and a tooltip give it. */
    name: string;
    /** A CSS color for its line. */
    color: string;
    /** Its points, oldest first. */
    points: Point[];
    /** Each point's least and most, where the points stand for stretches; drawn as a band. */
    spread?: Spread[];
}

/** How a line chart says its values: the unit it formats them in. */
export type Format = (v: number) => string;

/** A level the values are held against, drawn across the chart: an alarm threshold. */
export interface Threshold {
    value: number;
    /** What it is, in the legend: "threshold". */
    label: string;
    /** A CSS color; a warm one when left out. */
    color?: string;
}

/** A stretch of time shaded across the chart: a known fault, a maintenance window. */
export interface Band {
    /** Milliseconds since the epoch. */
    from: number;
    to: number;
    /** Shown at the top of the band, and in the tooltip inside it. */
    label: string;
    /** `warn` for a fault, `info` for anything else. Default `info`. */
    tone?: "warn" | "info";
}

/** The shapes a mark is drawn in, so marks are told apart by more than color. */
export type MarkShape = "dot" | "square" | "triangle" | "diamond";

/** A point in time marked on a chart: an alert, a stop, a label written. */
export interface Mark {
    /** Milliseconds since the epoch. */
    t: number;
    /** The height it is drawn at; at the top of the chart when left out. */
    v?: number;
    /** What kind of mark it is, in the legend and the tooltip: "AE alert". */
    label: string;
    /** Default `dot`. */
    shape?: MarkShape;
    /** A CSS color; the first series' when left out. */
    color?: string;
}

/** How a chart draws its points: joined, but broken at gaps; as points; or joined across gaps. */
export type DrawMode = "line" | "points" | "joined";

/** A window of time a chart is zoomed into, in milliseconds since the epoch. */
export interface TimeWindow {
    from: number;
    to: number;
}

/**
 * What a zoom asks of whoever owns the chart: series for ``window`` (finer
 * buckets of it, from the server), or nothing to narrow what is drawn already.
 * ``null`` is the reset: the series the chart was given.
 */
export type ZoomHandler = (window: TimeWindow | null) => Promise<Series[] | void> | void;

/** What a line chart is told besides its series. */
export interface ChartOptions {
    /** Its title, which names it for a screen reader too. */
    title: string;
    /** Formats a value for the axis labels and the tooltip. */
    format: Format;
    /** Whether zero is always in view, as for a count or a rate. Default true. */
    fromZero?: boolean;
    /** What the values are, beside the value axis: "reconstruction error", "ms". */
    yLabel?: string;
    /** What runs along the bottom, under the time axis; it is time unless said otherwise. */
    xLabel?: string;
    /** Levels drawn across the chart. */
    thresholds?: Threshold[];
    /** Stretches of time shaded across it. */
    bands?: Band[];
    /** Points in time marked on it. */
    marks?: Mark[];
    /** How the points are drawn: `auto` draws a sparse chart as points. Default `line`. */
    mode?: DrawMode | "auto";
    /** Offer Line / Points / Joined in the chart's header. */
    modeToggle?: boolean;
    /** Offer zoom: drag across the plot, and buttons for a keyboard and a phone. */
    zoom?: boolean;
    /** Asked for the series of a window zoomed into; see {@link ZoomHandler}. */
    onZoom?: ZoomHandler;
}

/** The series colors, in the order series are given them. Validated against
 *  the dashboard's dark surface for contrast and color-blind separation. */
export const SERIES_COLORS = ["#3987e5", "#d95926", "#199e70"] as const;
