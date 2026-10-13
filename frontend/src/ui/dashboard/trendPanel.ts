/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The trend panel: an agent's or a node's metrics history as a few small
 * charts, over a window the reader picks. Opened from a card, closed by its
 * button, the backdrop or Escape.
 *
 * Which charts an agent or a node gets is data (`AGENT_CHARTS`, `NODE_CHARTS`),
 * each saying how it is drawn from the stored samples. A chart with nothing to
 * draw — an agent that never spent anything, a node with no temperature
 * sensor — is left out rather than drawn empty.
 */
import { button, el } from "../dom";
import { fetchHistory, type HistoryRow } from "./history";
import {
    buildLineChart,
    downsample,
    levels,
    perMinute,
    samplesOf,
    SERIES_COLORS,
    type Format,
    type Point,
    type Series,
} from "./trend";

/** Whose history the panel shows. */
export type TrendKind = "agents" | "nodes";

/** One chart of the panel: its title, its unit and how its lines come from the samples. */
export interface ChartSpec {
    /** The chart's title. */
    title: string;
    /** Formats its values. */
    format: Format;
    /** Its lines, from the stored samples. */
    series: (rows: readonly HistoryRow[]) => Series[];
    /** Whether zero stays in view; default true. */
    fromZero?: boolean;
}

/** The windows a reader can pick, in hours, with their labels. */
export const WINDOWS: readonly [number, string][] = [
    [1, "1 h"],
    [24, "24 h"],
    [168, "7 d"],
];

/** The window the panel opens on, in hours. */
export const DEFAULT_HOURS = 1;

/** The most points a chart line is drawn with, whatever the window. */
const MAX_POINTS = 240;

/** How many of the latest samples the table under the charts lists. */
const TABLE_ROWS = 15;

const count: Format = v => (v >= 10 || Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1));
const percent: Format = v => `${v.toFixed(0)}%`;
const megabytes: Format = v => (v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${v.toFixed(0)} MB`);
const stateSize: Format = v => (v >= 1 ? `${v.toFixed(1)} MB` : `${(v * 1024).toFixed(0)} KB`);
const millis: Format = v => (v >= 1 ? `${v.toFixed(1)} s` : `${(v * 1000).toFixed(0)} ms`);
const dollars: Format = v => `$${v.toFixed(v >= 1 ? 2 : 4)}/h`;
const celsius: Format = v => `${v.toFixed(0)}°C`;

/** A single-line series of one field. */
function one(name: string, points: Point[]): Series[] {
    return [{ name, color: SERIES_COLORS[0], points }];
}

/** Several fields as lines in series order, leaving out a field never measured. */
function several(rows: readonly HistoryRow[], fields: readonly [string, string][]): Series[] {
    return fields
        .map(([field, name], i): Series => ({
            name,
            color: SERIES_COLORS[i % SERIES_COLORS.length] as string,
            points: levels(samplesOf(rows, field)),
        }))
        .filter(s => s.points.some(p => p.v !== null));
}

/** The charts an agent's history is drawn as. */
export const AGENT_CHARTS: readonly ChartSpec[] = [
    {
        title: "Messages per minute",
        format: count,
        series: rows => one("messages", perMinute(samplesOf(rows, "messages_processed"))),
    },
    {
        title: "Errors per minute",
        format: count,
        series: rows => one("errors", perMinute(samplesOf(rows, "errors"))),
    },
    {
        title: "Time to handle a message (p95)",
        format: millis,
        series: rows =>
            several(rows, [
                ["queue_wait_p95_s", "waiting"],
                ["message_p95_s", "handling"],
                ["task_p95_s", "task"],
            ]),
    },
    {
        title: "State size",
        format: stateSize,
        series: rows => one("state", levels(samplesOf(rows, "memory_mb"))),
    },
    {
        title: "Spend per hour",
        format: dollars,
        series: rows => {
            const spend = perMinute(samplesOf(rows, "cost_usd"));
            // Most agents never spend anything; a flat zero says nothing.
            if (!spend.some(p => p.v !== null && p.v > 0)) {
                return [];
            }
            return one(
                "spend",
                spend.map(p => ({ t: p.t, v: p.v === null ? null : p.v * 60 })),
            );
        },
    },
];

/** The charts a node's history is drawn as. */
export const NODE_CHARTS: readonly ChartSpec[] = [
    { title: "CPU", format: percent, series: rows => one("cpu", levels(samplesOf(rows, "cpu_pct"))) },
    {
        title: "Memory",
        format: megabytes,
        series: rows =>
            several(rows, [
                ["mem_used_mb", "used"],
                ["mem_free_mb", "free"],
            ]),
    },
    { title: "Load (1 min)", format: count, series: rows => one("load", levels(samplesOf(rows, "load_1m"))) },
    {
        title: "Disk free",
        format: megabytes,
        series: rows => one("disk free", levels(samplesOf(rows, "disk_free_mb"))),
    },
    {
        title: "CPU temperature",
        format: celsius,
        fromZero: false,
        series: rows => one("temperature", levels(samplesOf(rows, "temp_c"))),
    },
    { title: "Agents", format: count, series: rows => one("agents", levels(samplesOf(rows, "agents"))) },
];

/**
 * How often a node was held back in a window, in words, or "" when it never was.
 *
 * Throttling is a state rather than a level, so it is said rather than drawn:
 * how many of the samples had each flag.
 */
export function throttleSummary(rows: readonly HistoryRow[]): string {
    const counts = new Map<string, number>();
    for (const row of rows) {
        const flags = row["throttled"];
        if (Array.isArray(flags)) {
            for (const flag of flags) {
                if (typeof flag === "string") {
                    counts.set(flag, (counts.get(flag) ?? 0) + 1);
                }
            }
        }
    }
    if (counts.size === 0) {
        return "";
    }
    const parts = [...counts].map(
        ([flag, n]) => `${flag.replace(/_/g, " ")} in ${n} of ${rows.length} samples`,
    );
    return `Throttled: ${parts.join(", ")}`;
}

/** The charts of one history, built; a chart with nothing to draw is left out. */
export function buildCharts(specs: readonly ChartSpec[], rows: readonly HistoryRow[]): HTMLElement[] {
    const charts: HTMLElement[] = [];
    for (const spec of specs) {
        const series = spec
            .series(rows)
            .map(s => ({ ...s, points: downsample(s.points, MAX_POINTS) }))
            .filter(s => s.points.some(p => p.v !== null));
        if (series.length > 0) {
            charts.push(
                buildLineChart(series, {
                    title: spec.title,
                    format: spec.format,
                    ...(spec.fromZero !== undefined && { fromZero: spec.fromZero }),
                }),
            );
        }
    }
    return charts;
}

/** One stored value as a table cell says it: a list joined, a gap as a dash. */
export function cellText(value: unknown): string {
    if (Array.isArray(value)) {
        return value.map(cellText).join(", ");
    }
    if (typeof value === "number" || typeof value === "string" || typeof value === "boolean") {
        return String(value);
    }
    return "—";
}

/** The latest samples as a table: the same values as the charts, without hovering. */
export function buildSampleTable(rows: readonly HistoryRow[], fields: readonly string[]): HTMLElement {
    const details = el("details", "af-trend-table");
    details.appendChild(el("summary", "", "Latest samples"));
    const table = el("table");
    const head = el("tr");
    head.appendChild(el("th", "", "time"));
    for (const f of fields) {
        head.appendChild(el("th", "", f));
    }
    table.appendChild(el("thead")).appendChild(head);
    const body = el("tbody");
    for (const row of rows.slice(-TABLE_ROWS).reverse()) {
        const tr = el("tr");
        tr.appendChild(el("td", "", new Date(row.ts * 1000).toLocaleTimeString()));
        for (const f of fields) {
            tr.appendChild(el("td", "", cellText(row[f])));
        }
        body.appendChild(tr);
    }
    table.appendChild(body);
    details.appendChild(table);
    return details;
}

const AGENT_FIELDS = ["messages_processed", "errors", "message_p95_s", "memory_mb", "cost_usd"];
const NODE_FIELDS = ["cpu_pct", "mem_free_mb", "load_1m", "disk_free_mb", "temp_c", "throttled"];

/** What can take focus inside the panel, in order. */
const FOCUSABLE = "button:not([disabled]), summary, [tabindex='0']";

/**
 * Keep Tab inside ``container`` while a dialog is open: from the last thing
 * that takes focus back to the first, and Shift+Tab the other way. Focus that
 * has wandered outside is brought back to the first.
 */
export function keepFocusWithin(container: HTMLElement, e: KeyboardEvent): void {
    const items = [...container.querySelectorAll<HTMLElement>(FOCUSABLE)];
    const first = items[0];
    const last = items[items.length - 1];
    if (!first || !last) {
        return;
    }
    const active = document.activeElement;
    const inside = active instanceof Node && container.contains(active);
    if (!inside || (e.shiftKey && active === first)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
    } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
    }
}

/** Reads a history; replaced in tests. */
export type HistoryReader = typeof fetchHistory;

/** The panel itself. One per dashboard; `open` replaces whatever it showed. */
export class TrendPanel {
    private _overlay: HTMLElement | null = null;
    private _kind: TrendKind = "agents";
    private _name = "";
    private _hours = DEFAULT_HOURS;
    /** Which open is current, so an answer that arrives late for an earlier one is dropped. */
    private _generation = 0;
    /** What had focus when the panel opened, given it back when it closes. */
    private _returnTo: HTMLElement | null = null;
    private readonly _onKey = (e: KeyboardEvent): void => {
        if (e.key === "Escape") {
            this.close();
        } else if (e.key === "Tab" && this._overlay) {
            keepFocusWithin(this._overlay, e);
        }
    };

    constructor(private readonly _read: HistoryReader = fetchHistory) {}

    /** Whether the panel is showing. */
    get isOpen(): boolean {
        return this._overlay !== null;
    }

    /** Show ``name``'s history, an agent's or a node's, over the last hour. */
    async open(kind: TrendKind, name: string): Promise<void> {
        this.close();
        const active = document.activeElement;
        this._returnTo = active instanceof HTMLElement && active !== document.body ? active : null;
        this._kind = kind;
        this._name = name;
        this._hours = DEFAULT_HOURS;
        this._overlay = this._build();
        document.body.appendChild(this._overlay);
        document.addEventListener("keydown", this._onKey);
        this._overlay.querySelector<HTMLButtonElement>(".af-trend-close")?.focus();
        await this._load();
    }

    /** Close the panel, if it is open. */
    close(): void {
        this._generation++;
        document.removeEventListener("keydown", this._onKey);
        if (this._overlay) {
            this._overlay.remove();
            this._overlay = null;
            // Back where the reader was -- the History button, typically --
            // unless that went away while the panel was open.
            if (this._returnTo?.isConnected) {
                this._returnTo.focus();
            }
        }
        this._returnTo = null;
    }

    /** Close and let go of everything; the panel is not used again. */
    destroy(): void {
        this.close();
    }

    private _build(): HTMLElement {
        const overlay = el("div", "af-trend-overlay");
        overlay.addEventListener("click", e => {
            if (e.target === overlay) {
                this.close();
            }
        });
        const panel = el("div", "af-trend-panel");
        panel.setAttribute("role", "dialog");
        panel.setAttribute("aria-modal", "true");
        panel.setAttribute("aria-label", `History of ${this._name}`);
        const body = el("div", "af-trend-body");
        body.appendChild(el("div", "af-chart-empty", "Loading…"));
        panel.append(this._buildHead(), this._buildWindows(), body);
        overlay.appendChild(panel);
        return overlay;
    }

    /** The name, whether it is an agent or a node, and the close button. */
    private _buildHead(): HTMLElement {
        const head = el("div", "af-trend-head");
        head.append(
            el("h3", "", this._name),
            el("span", "af-trend-kind", this._kind === "agents" ? "agent" : "node"),
        );
        const close = button("af-trend-close", "×");
        close.setAttribute("aria-label", "Close");
        close.addEventListener("click", () => this.close());
        head.appendChild(close);
        return head;
    }

    /** One button per window; the pressed one is the window shown. */
    private _buildWindows(): HTMLElement {
        const windows = el("div", "af-trend-windows");
        for (const [hours, label] of WINDOWS) {
            const b = button("af-mini-btn af-trend-window", label);
            b.dataset["hours"] = String(hours);
            b.setAttribute("aria-pressed", String(hours === this._hours));
            b.addEventListener("click", () => {
                this._hours = hours;
                windows
                    .querySelectorAll<HTMLButtonElement>(".af-trend-window")
                    .forEach(w => w.setAttribute("aria-pressed", String(w === b)));
                void this._load();
            });
            windows.appendChild(b);
        }
        return windows;
    }

    private async _load(): Promise<void> {
        const generation = ++this._generation;
        const body = this._overlay?.querySelector<HTMLElement>(".af-trend-body");
        if (!body) {
            return;
        }
        // The previous charts stay, dimmed, until the new ones are ready.
        body.classList.add("af-trend-loading");
        const rows = await this._read(this._kind, this._name, this._hours);
        if (generation !== this._generation) {
            return;
        }
        body.classList.remove("af-trend-loading");
        body.replaceChildren(...this._content(rows));
    }

    private _content(rows: HistoryRow[] | null): HTMLElement[] {
        if (rows === null) {
            return [el("div", "af-chart-empty", "The history is not available.")];
        }
        if (rows.length === 0) {
            return [el("div", "af-chart-empty", "No samples in this window yet.")];
        }
        const nodes = this._kind === "nodes";
        const content: HTMLElement[] = [];
        const throttled = nodes ? throttleSummary(rows) : "";
        if (throttled) {
            content.push(el("div", "af-trend-warning", `⚠ ${throttled}`));
        }
        const grid = el("div", "af-trend-charts");
        grid.append(...buildCharts(nodes ? NODE_CHARTS : AGENT_CHARTS, rows));
        content.push(grid, buildSampleTable(rows, nodes ? NODE_FIELDS : AGENT_FIELDS));
        return content;
    }
}
