/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * What is marked on a chart besides its lines: thresholds across it, bands of
 * time shaded on it, and marks at points in time.
 *
 * Thresholds and bands are drawn in the plot's SVG. Marks and band labels are
 * HTML laid over it at the same place: the SVG stretches to the plot's width
 * without keeping its proportions, which would squash a square into a bar and
 * a word into a smear. Each kind of mark has a shape as well as a color, and
 * the legend names it, so none is told apart by color alone.
 */
import { el } from "../dom";
import { CHART_H, CHART_PAD, CHART_W, SVG_NS, type Frame, type Scales } from "./geometry";
import type { Band, ChartOptions, Mark, MarkShape, Series, Threshold } from "./types";

/** A threshold's color when it names none: warm, apart from the series colors. */
export const THRESHOLD_COLOR = "#e5484d";

/** How close in time a mark is to the pointer to be listed, as a share of the window. */
const NEAR = 0.015;

/** ``value`` as a share of the plot's width or height, for an element laid over it. */
function percentX(px: number): string {
    return `${((px / CHART_W) * 100).toFixed(2)}%`;
}

function percentY(py: number): string {
    return `${((py / CHART_H) * 100).toFixed(2)}%`;
}

/** The SVG for ``bands`` and ``thresholds``, under the lines. */
export function drawUnder(root: SVGSVGElement, options: ChartOptions, frame: Frame, scale: Scales): void {
    for (const band of visibleBands(options.bands ?? [], frame)) {
        const rect = document.createElementNS(SVG_NS, "rect");
        const x0 = scale.x(Math.max(band.from, frame.t0));
        const x1 = scale.x(Math.min(band.to, frame.t1));
        rect.setAttribute("x", x0.toFixed(1));
        rect.setAttribute("y", "0");
        rect.setAttribute("width", Math.max(x1 - x0, 1).toFixed(1));
        rect.setAttribute("height", String(CHART_H));
        rect.setAttribute("class", `af-chart-band ${band.tone ?? "info"}`);
        root.appendChild(rect);
    }
    for (const threshold of options.thresholds ?? []) {
        const rule = document.createElementNS(SVG_NS, "line");
        const y = scale.y(threshold.value).toFixed(1);
        rule.setAttribute("x1", String(CHART_PAD));
        rule.setAttribute("x2", String(CHART_W - CHART_PAD));
        rule.setAttribute("y1", y);
        rule.setAttribute("y2", y);
        rule.setAttribute("class", "af-chart-threshold");
        rule.setAttribute("stroke", threshold.color ?? THRESHOLD_COLOR);
        root.appendChild(rule);
    }
}

/** The bands that overlap the window shown. */
export function visibleBands(bands: readonly Band[], frame: Frame): Band[] {
    return bands.filter(b => b.to >= frame.t0 && b.from <= frame.t1);
}

/** The marks inside the window shown. */
export function visibleMarks(marks: readonly Mark[], frame: Frame): Mark[] {
    return marks.filter(m => m.t >= frame.t0 && m.t <= frame.t1);
}

/** The HTML laid over the plot: each band's label and each mark, placed as the SVG places them. */
export function drawOver(
    options: ChartOptions,
    series: readonly Series[],
    frame: Frame,
    scale: Scales,
): HTMLElement {
    const layer = el("div", "af-chart-over");
    layer.setAttribute("aria-hidden", "true");
    for (const band of visibleBands(options.bands ?? [], frame)) {
        const label = el("span", `af-chart-band-label ${band.tone ?? "info"}`, band.label);
        label.style.left = percentX(scale.x(Math.max(band.from, frame.t0)));
        layer.appendChild(label);
    }
    const fallback = series[0]?.color ?? THRESHOLD_COLOR;
    for (const mark of visibleMarks(options.marks ?? [], frame)) {
        const shape = el("span", `af-chart-mark ${mark.shape ?? "dot"}`);
        shape.style.left = percentX(scale.x(mark.t));
        shape.style.top = percentY(mark.v === undefined ? CHART_PAD : scale.y(mark.v));
        shape.style.color = mark.color ?? fallback;
        layer.appendChild(shape);
    }
    return layer;
}

/** One legend entry for each threshold, and one for each kind of mark with how many are shown. */
export function legendMarks(options: ChartOptions, series: readonly Series[], frame: Frame): HTMLElement[] {
    const items = (options.thresholds ?? []).map(t => thresholdItem(t));
    const kinds = new Map<string, { shape: MarkShape; color: string; count: number }>();
    const fallback = series[0]?.color ?? THRESHOLD_COLOR;
    for (const mark of visibleMarks(options.marks ?? [], frame)) {
        const kind = kinds.get(mark.label);
        if (kind) {
            kind.count += 1;
        } else {
            kinds.set(mark.label, { shape: mark.shape ?? "dot", color: mark.color ?? fallback, count: 1 });
        }
    }
    for (const [label, kind] of kinds) {
        const item = el("span", "af-chart-legend-item");
        const key = el("span", `af-chart-mark-key ${kind.shape}`);
        key.style.color = kind.color;
        item.append(key, el("span", "", `${label} (${kind.count})`));
        items.push(item);
    }
    return items;
}

function thresholdItem(threshold: Threshold): HTMLElement {
    const item = el("span", "af-chart-legend-item");
    const key = el("span", "af-chart-key dashed");
    key.style.borderColor = threshold.color ?? THRESHOLD_COLOR;
    item.append(key, el("span", "", threshold.label));
    return item;
}

/** What the tooltip adds at time ``t``: the band it falls in, and the marks near it. */
export function tooltipExtras(options: ChartOptions, frame: Frame, t: number): string[] {
    const within = (options.bands ?? []).filter(b => t >= b.from && t <= b.to).map(b => b.label);
    const reach = (frame.t1 - frame.t0) * NEAR;
    const near = (options.marks ?? []).filter(m => Math.abs(m.t - t) <= reach).map(m => m.label);
    return [...within, ...new Set(near)];
}
