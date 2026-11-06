/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * A chart's axes: round values up the side and round times along the bottom,
 * each with a faint gridline across the plot, worked out again for whatever
 * window is shown, so a zoom gets ticks of its own.
 *
 * The labels are HTML beside the plot and the gridlines SVG inside it: the SVG
 * stretches to the plot's width without keeping its proportions, which would
 * smear a word. A tick too near an end, or that formats as its neighbour does,
 * is left out, so the labels never crowd or repeat.
 */
import { el } from "../dom";
import { CHART_H, CHART_PAD, CHART_W, SVG_NS, timeLabel, type Frame, type Scales } from "./geometry";
import type { Format } from "./types";

/** How many value ticks a chart aims for between its top and bottom. */
export const VALUE_TICKS = 4;

/** How many time ticks a chart aims for along its bottom; a narrow one shows every other. */
export const TIME_TICKS = 6;

/** How near an end, as a share of the range, a value tick may be before it is left out. */
const END_ROOM = 0.2;

/** A labelled tick: where it is, and what it says. */
export interface Tick {
    at: number;
    label: string;
}

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const MONTH = 30 * DAY;
const YEAR = 365 * DAY;

/**
 * A step time ticks are spaced by. Below a day it is a fixed length; from a day
 * up it counts days, months or years of the calendar, so a tick stays on a
 * midnight, the first of a month or the first of January however long the
 * months are and wherever the clocks change. ``ms`` is its length, near enough
 * to choose by.
 */
export interface TimeStep {
    unit: "ms" | "day" | "month" | "year";
    count: number;
    ms: number;
}

const fixed = (ms: number): TimeStep => ({ unit: "ms", count: ms, ms });
const days = (count: number): TimeStep => ({ unit: "day", count, ms: count * DAY });
const months = (count: number): TimeStep => ({ unit: "month", count, ms: count * MONTH });
const years = (count: number): TimeStep => ({ unit: "year", count, ms: count * YEAR });

/** The steps time ticks are spaced by, finest first. */
export const TIME_STEPS: readonly TimeStep[] = [
    ...[1, 2, 5, 10, 15, 30].map(n => fixed(n * SECOND)),
    ...[1, 2, 5, 10, 15, 30].map(n => fixed(n * MINUTE)),
    ...[1, 2, 3, 6, 12].map(n => fixed(n * HOUR)),
    ...[1, 2, 7, 14].map(days),
    ...[1, 3, 6].map(months),
    ...[1, 2, 5, 10, 25, 50, 100].map(years),
];

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** A round step, of 1, 2, 2.5 or 5 times a power of ten, near ``rough``. */
export function niceStep(rough: number): number {
    if (!(rough > 0) || !Number.isFinite(rough)) {
        return 1;
    }
    const power = 10 ** Math.floor(Math.log10(rough));
    const scaled = rough / power;
    const nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 2.5 ? 2.5 : scaled <= 5 ? 5 : 10;
    return nice * power;
}

/**
 * The value labels of ``frame``, top first: its top, the round values between
 * that keep their distance from the ends, and its bottom. One that reads as the
 * label before it is left out.
 */
export function valueTicks(frame: Frame, format: Format, count = VALUE_TICKS): Tick[] {
    const range = frame.hi - frame.lo;
    const inside: Tick[] = [];
    if (range > 0) {
        const step = niceStep(range / count);
        const room = range * END_ROOM;
        for (let v = Math.ceil(frame.lo / step) * step; v < frame.hi; v += step) {
            if (v - frame.lo >= room && frame.hi - v >= room) {
                inside.push({ at: v, label: format(v) });
            }
        }
    }
    const all = [
        { at: frame.hi, label: format(frame.hi) },
        ...inside.reverse(),
        { at: frame.lo, label: format(frame.lo) },
    ];
    return all.filter((tick, i) => i === 0 || tick.label !== (all[i - 1] as Tick).label);
}

/** The step time ticks of ``frame`` are spaced by: the finest that gives no more than ``count``. */
export function timeStep(span: number, count = TIME_TICKS): TimeStep {
    return (
        TIME_STEPS.find(step => span / step.ms <= count) ?? (TIME_STEPS[TIME_STEPS.length - 1] as TimeStep)
    );
}

/** The first moment at or after ``t`` on ``step`` in local time: a whole hour, a midnight, a first of the month. */
export function firstOnStep(t: number, step: TimeStep): number {
    if (step.unit === "ms") {
        const offset = new Date(t).getTimezoneOffset() * MINUTE;
        return Math.ceil((t - offset) / step.count) * step.count + offset;
    }
    const start = new Date(t);
    start.setHours(0, 0, 0, 0);
    if (step.unit !== "day") {
        start.setDate(1);
        const month = step.unit === "year" ? 0 : start.getMonth() - (start.getMonth() % step.count);
        start.setMonth(month);
        if (step.unit === "year") {
            start.setFullYear(start.getFullYear() - (start.getFullYear() % step.count));
        }
    }
    // A day step starts at the next midnight; months and years at the next one on their multiple.
    const by = step.unit === "day" ? days(1) : step;
    while (start.getTime() < t) {
        advance(start, by);
    }
    return start.getTime();
}

/** ``date`` moved one ``step`` on, along the calendar for days, months and years. */
function advance(date: Date, step: TimeStep): void {
    if (step.unit === "ms") {
        date.setTime(date.getTime() + step.count);
    } else if (step.unit === "day") {
        date.setDate(date.getDate() + step.count);
    } else if (step.unit === "month") {
        date.setMonth(date.getMonth() + step.count);
    } else {
        date.setFullYear(date.getFullYear() + step.count);
    }
}

/** How a tick on ``step`` reads: seconds when the step is under a minute, the year alone for years. */
export function tickLabel(t: number, step: TimeStep, span: number): string {
    const d = new Date(t);
    if (step.unit === "year") {
        return String(d.getFullYear());
    }
    if (step.unit === "month") {
        return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
    }
    if (step.unit === "day") {
        return `${d.getDate()}/${d.getMonth() + 1}`;
    }
    if (step.count < MINUTE) {
        return `${timeLabel(t, span)}:${String(d.getSeconds()).padStart(2, "0")}`;
    }
    return timeLabel(t, span);
}

/** The time labels of ``frame``, oldest first, at round local times; its two ends when none falls inside. */
export function timeTicks(frame: Frame, count = TIME_TICKS): Tick[] {
    const span = frame.t1 - frame.t0;
    const step = timeStep(span, count);
    const ticks: Tick[] = [];
    const at = new Date(firstOnStep(frame.t0, step));
    while (at.getTime() <= frame.t1) {
        ticks.push({ at: at.getTime(), label: tickLabel(at.getTime(), step, span) });
        advance(at, step);
    }
    if (ticks.length > 0) {
        return ticks;
    }
    const finest = TIME_STEPS[0] as TimeStep;
    return [
        { at: frame.t0, label: tickLabel(frame.t0, finest, span) },
        { at: frame.t1, label: tickLabel(frame.t1, finest, span) },
    ];
}

/** The faint lines across the plot at each tick, under everything else drawn. */
export function drawGrid(
    root: SVGSVGElement,
    values: readonly Tick[],
    times: readonly Tick[],
    scale: Scales,
): void {
    for (const tick of values) {
        root.appendChild(gridLine(CHART_PAD, CHART_W - CHART_PAD, scale.y(tick.at), scale.y(tick.at)));
    }
    for (const tick of times) {
        root.appendChild(gridLine(scale.x(tick.at), scale.x(tick.at), 0, CHART_H));
    }
}

function gridLine(x1: number, x2: number, y1: number, y2: number): SVGLineElement {
    const rule = document.createElementNS(SVG_NS, "line");
    rule.setAttribute("x1", x1.toFixed(1));
    rule.setAttribute("x2", x2.toFixed(1));
    rule.setAttribute("y1", y1.toFixed(1));
    rule.setAttribute("y2", y2.toFixed(1));
    rule.setAttribute("class", "af-chart-grid");
    return rule;
}

/** The value labels in a gutter of their own, each at its height, so none is drawn over a line. */
export function valueAxis(values: readonly Tick[], scale: Scales): HTMLElement {
    const gutter = el("div", "af-chart-axis");
    for (const tick of values) {
        const label = el("span", "", tick.label);
        // The gutter is as tall as the plot's drawing, whose units it shares.
        label.style.top = `${((scale.y(tick.at) / CHART_H) * 100).toFixed(2)}%`;
        gutter.appendChild(label);
    }
    return gutter;
}

/** The time labels under the plot, each under its tick; every other one is marked, for a narrow chart to hide. */
export function timeAxis(times: readonly Tick[], scale: Scales): HTMLElement {
    const axis = el("div", "af-chart-times");
    times.forEach((tick, i) => {
        const label = el("span", i % 2 === 1 ? "af-chart-tick odd" : "af-chart-tick", tick.label);
        label.style.left = `${((scale.x(tick.at) / CHART_W) * 100).toFixed(2)}%`;
        axis.appendChild(label);
    });
    return axis;
}
