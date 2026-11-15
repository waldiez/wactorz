/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * A line chart of one or more series sharing one axis: a crosshair and a
 * tooltip listing every series at the time under the pointer, and, where it is
 * asked for, what is marked on it (`marks.ts`), a choice of how its points are
 * drawn (`mode.ts`) and a zoom into a window of its time (`zoom.ts`).
 *
 * A zoom narrows to the points already drawn, unless the chart's owner answers
 * `onZoom` with finer ones: a series read from the server in buckets can ask
 * for the window again and get buckets of it, so the chart never holds more
 * points than it can show, at any zoom.
 *
 * Series names reach the page as text, never as markup. With two or more
 * series, or anything marked, a legend names each beside its stroke or shape,
 * so nothing is told apart by color alone.
 */
import { el } from "../dom";
import {
    CHART_H,
    CHART_PAD,
    CHART_W,
    SVG_NS,
    ceiling,
    extent,
    line,
    modePath,
    nearest,
    plotScales,
    spreadPath,
    svg,
    timeLabel,
    widened,
    type Frame,
    type Scales,
} from "./geometry";
import { drawGrid, timeAxis, timeTicks, valueAxis, valueTicks } from "./axes";
import { drawOver, drawUnder, legendMarks, tooltipExtras } from "./marks";
import { autoMode, modeToggle } from "./mode";
import type { ChartOptions, DrawMode, Format, Series, TimeWindow } from "./types";
import { clamp, windowBetween, wireDrag, zoomButtons, zoomedIn, zoomedOut } from "./zoom";

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

/** The time ``series`` span, or null when they hold no point. */
export function spanOf(series: readonly Series[]): TimeWindow | null {
    const times = series.flatMap(s => s.points.map(p => p.t));
    return times.length ? { from: Math.min(...times), to: Math.max(...times) } : null;
}

/** ``series`` with only the points, and the spread, inside ``window``. */
export function narrowed(series: readonly Series[], window: TimeWindow): Series[] {
    const inside = (t: number): boolean => t >= window.from && t <= window.to;
    return series.map(s => ({
        ...s,
        points: s.points.filter(p => inside(p.t)),
        ...(s.spread ? { spread: s.spread.filter(p => inside(p.t)) } : {}),
    }));
}

/** The window and the range ``series`` are drawn over, or null with nothing to draw. */
export function frameOf(
    series: readonly Series[],
    options: ChartOptions,
    window: TimeWindow | null,
): Frame | null {
    const spreads = series.flatMap(s => (s.spread ?? []).flatMap(p => [p.lo, p.hi]));
    const levels = (options.thresholds ?? []).map(t => t.value);
    const range = widened(widened(extent(series.map(s => s.points)), spreads), levels);
    const span = window ?? spanOf(series);
    if (!range || !span || extent(series.map(s => s.points)) === null) {
        return null;
    }
    const lo = options.fromZero === false ? range[0] : Math.min(0, range[0]);
    return { t0: span.from, t1: span.to, lo, hi: ceiling(lo, range[1]) };
}

/** A line chart that can be given new series, redrawn in another mode, zoomed, and ended. */
export class LineChart {
    /** The chart, to be placed in the page. */
    readonly element: HTMLElement;
    private given: Series[];
    private zoomedSeries: Series[] | null = null;
    private options: ChartOptions;
    private chosen: DrawMode | null = null;
    private window: TimeWindow | null = null;
    private asked = 0;
    /** Why the owner could not answer the last zoom, or "". */
    private failed = "";
    private ended = false;
    private readonly controls = el("div", "af-chart-controls");
    private readonly body = el("div", "af-chart-body");

    constructor(series: readonly Series[], options: ChartOptions) {
        this.given = [...series];
        this.options = options;
        this.element = el("figure", "af-chart");
        const head = el("div", "af-chart-head");
        head.append(el("figcaption", "af-chart-title", options.title), this.controls);
        this.element.append(head, this.body);
        this.render();
    }

    /** Draw ``series`` instead, and ``options`` when given; a zoom in place stays. */
    update(series: readonly Series[], options?: ChartOptions): void {
        this.given = [...series];
        this.zoomedSeries = null;
        if (options) {
            this.options = options;
        }
        this.render();
        if (this.window && this.options.onZoom) {
            void this.zoomTo(this.window);
        }
    }

    /** Take the chart out of the page; an answer to a zoom still on its way is dropped. */
    destroy(): void {
        this.ended = true;
        this.element.remove();
    }

    /** How the points are drawn now: as chosen, else as the options say. */
    mode(): DrawMode {
        if (this.chosen) {
            return this.chosen;
        }
        const asked = this.options.mode ?? "line";
        return asked === "auto" ? autoMode(this.shown().map(s => s.points)) : asked;
    }

    /** The window zoomed into, or null when the whole of the series is shown. */
    zoomed(): TimeWindow | null {
        return this.window;
    }

    /** Zoom into ``window``, or back to everything with null. */
    async zoomTo(window: TimeWindow | null): Promise<void> {
        const whole = spanOf(this.given);
        this.window = window && whole ? clamp(window, whole) : null;
        this.zoomedSeries = null;
        this.failed = "";
        this.render();
        const ask = this.options.onZoom;
        if (!ask) {
            return;
        }
        const turn = ++this.asked;
        let answer: Series[] | void;
        try {
            answer = await ask(this.window);
        } catch (error) {
            // What is drawn already stands; the reader is told the finer view did not come.
            if (!this.ended && turn === this.asked) {
                this.failed = error instanceof Error ? error.message : String(error);
                this.render();
            }
            return;
        }
        if (this.ended || turn !== this.asked || !answer) {
            return;
        }
        this.zoomedSeries = answer;
        this.render();
    }

    private shown(): Series[] {
        if (this.zoomedSeries) {
            return this.zoomedSeries;
        }
        return this.window ? narrowed(this.given, this.window) : this.given;
    }

    private render(): void {
        this.renderControls();
        const series = this.shown();
        const frame = frameOf(series, this.options, this.window);
        if (!frame) {
            // Empty here may only mean the finer data did not come: the notice says which.
            this.body.replaceChildren(
                el("div", "af-chart-empty", "No samples in this window"),
                ...this.failure(),
            );
            return;
        }
        this.body.replaceChildren(this.plot(series, frame), ...this.failure());
        if (this.options.yLabel) {
            this.body.prepend(el("div", "af-chart-ylabel", this.options.yLabel));
        }
        if (this.options.xLabel) {
            this.body.appendChild(el("div", "af-chart-xlabel", this.options.xLabel));
        }
        const extras = legendMarks(this.options, series, frame);
        if (series.length > 1 || extras.length) {
            const legend = buildLegend(series.length > 1 ? series : []);
            legend.append(...extras);
            this.body.appendChild(legend);
        }
    }

    /** The notice that the owner could not answer the last zoom, or nothing. */
    private failure(): HTMLElement[] {
        if (!this.failed) {
            return [];
        }
        const note = el("p", "af-chart-note", `Could not read finer data for this window: ${this.failed}`);
        note.setAttribute("role", "status");
        return [note];
    }

    private renderControls(): void {
        // The control a keyboard was on is drawn again; focus goes back to it, or,
        // when it is now disabled, to Zoom in, rather than to the page.
        const focused = this.controls.contains(document.activeElement)
            ? document.activeElement?.getAttribute("aria-label") || document.activeElement?.textContent
            : null;
        const parts: HTMLElement[] = [];
        if (this.options.modeToggle) {
            parts.push(modeToggle(this.mode(), mode => this.choose(mode)));
        }
        const whole = spanOf(this.given);
        if (this.options.zoom && whole) {
            parts.push(
                zoomButtons(this.window !== null, {
                    zoomIn: () => void this.zoomTo(zoomedIn(this.window ?? whole, whole)),
                    zoomOut: () => void this.zoomTo(this.window ? zoomedOut(this.window, whole) : null),
                    reset: () => void this.zoomTo(null),
                }),
            );
        }
        this.controls.replaceChildren(...parts);
        if (focused) {
            refocus(this.controls, focused);
        }
    }

    private choose(mode: DrawMode): void {
        this.chosen = mode;
        this.render();
        const checked = this.controls.querySelector<HTMLElement>('[aria-checked="true"]');
        checked?.focus();
    }

    private plot(series: readonly Series[], frame: Frame): HTMLElement {
        const scale = plotScales(frame);
        const plot = el("div", "af-chart-plot");
        const canvas = el("div", "af-chart-canvas");
        const values = valueTicks(frame, this.options.format);
        const times = timeTicks(frame);
        const root = svg(CHART_W, CHART_H, spokenName(this.options));
        drawGrid(root, values.slice(1, -1), times, scale);
        drawUnder(root, this.options, frame, scale);
        drawSeries(root, series, this.mode(), scale);
        const cross = root.appendChild(crosshair());
        const shading = el("div", "af-chart-selection");
        shading.hidden = true;
        canvas.append(root, drawOver(this.options, series, frame, scale), shading);
        if (this.options.zoom) {
            this.wireZoom(canvas, shading, frame);
        }
        const tip = el("div", "af-chart-tip");
        tip.hidden = true;
        plot.append(valueAxis(values, scale), canvas, timeAxis(times, scale), tip);
        wireCrosshair({ plot, canvas, cross, tip, series, options: this.options, frame });
        return plot;
    }

    private wireZoom(canvas: HTMLElement, shading: HTMLElement, frame: Frame): void {
        const shown = { from: frame.t0, to: frame.t1 };
        wireDrag(canvas, shading, (a, b) => void this.zoomTo(windowBetween(a, b, shown)));
        canvas.addEventListener("dblclick", () => {
            if (this.window) {
                void this.zoomTo(null);
            }
        });
    }
}

/** A line chart of ``series`` as an element, for a caller that only places it. */
export function buildLineChart(series: readonly Series[], options: ChartOptions): HTMLElement {
    return new LineChart(series, options).element;
}

/** Focus the control of ``controls`` named ``name``, or Zoom in when that one is disabled now. */
function refocus(controls: HTMLElement, name: string): void {
    const buttons = [...controls.querySelectorAll<HTMLButtonElement>("button")];
    const named = (b: HTMLButtonElement) => (b.getAttribute("aria-label") || b.textContent) === name;
    const same = buttons.find(named);
    const target =
        same && !same.disabled ? same : buttons.find(b => b.getAttribute("aria-label") === "Zoom in");
    target?.focus();
}

/** Each series' spread, then its points, in ``mode``. */
function drawSeries(root: SVGSVGElement, series: readonly Series[], mode: DrawMode, scale: Scales): void {
    for (const s of series) {
        if (s.spread?.length) {
            const band = line(spreadPath(s.spread, scale), "none", "af-chart-spread");
            band.setAttribute("fill", s.color);
            root.appendChild(band);
        }
        root.appendChild(line(modePath(s.points, mode, scale), s.color, `af-trend-line ${mode}`));
    }
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

/** The chart's name for a screen reader: its title, and what its axes are where it says. */
export function spokenName(options: ChartOptions): string {
    if (!options.yLabel) {
        return options.title;
    }
    return `${options.title}: ${options.yLabel} over ${options.xLabel ?? "time"}`;
}

/** What the crosshair follows, and what its tooltip reads. */
interface Pointer {
    plot: HTMLElement;
    canvas: HTMLElement;
    cross: SVGLineElement;
    tip: HTMLElement;
    series: readonly Series[];
    options: ChartOptions;
    frame: Frame;
}

function tipContent(at: Pointer, t: number): HTMLElement[] {
    const rows = [el("div", "af-chart-tip-time", timeLabel(t, at.frame.t1 - at.frame.t0))];
    for (const [name, color, value] of tooltipRows(at.series, t, at.options.format)) {
        const row = el("div", "af-chart-tip-row");
        const key = el("span", "af-chart-key");
        key.style.background = color;
        row.append(key, el("strong", "", value), el("span", "", name));
        rows.push(row);
    }
    for (const said of tooltipExtras(at.options, at.frame, t)) {
        rows.push(el("div", "af-chart-tip-note", said));
    }
    return rows;
}

function wireCrosshair(at: Pointer): void {
    const show = (fraction: number): void => {
        const clamped = Math.max(0, Math.min(1, fraction));
        const t = at.frame.t0 + clamped * (at.frame.t1 - at.frame.t0);
        const px = CHART_PAD + clamped * (CHART_W - 2 * CHART_PAD);
        at.cross.setAttribute("x1", px.toFixed(1));
        at.cross.setAttribute("x2", px.toFixed(1));
        at.cross.style.display = "";
        at.tip.replaceChildren(...tipContent(at, t));
        // Over the crosshair, measured from the plot, which the gutter widens.
        const box = at.canvas.getBoundingClientRect();
        const offset = box.left - at.plot.getBoundingClientRect().left;
        at.tip.style.left = `${(offset + clamped * box.width).toFixed(1)}px`;
        at.tip.hidden = false;
    };
    const hide = (): void => {
        at.cross.style.display = "none";
        at.tip.hidden = true;
    };
    at.plot.addEventListener("pointermove", e => {
        const box = at.canvas.getBoundingClientRect();
        show(box.width > 0 ? (e.clientX - box.left) / box.width : 1);
    });
    at.plot.addEventListener("pointerleave", hide);
    // The same readout from the keyboard: focus shows the latest time.
    at.plot.tabIndex = 0;
    at.plot.addEventListener("focus", () => show(1));
    at.plot.addEventListener("blur", hide);
}
