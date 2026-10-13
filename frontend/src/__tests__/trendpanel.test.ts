/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { HistoryRow } from "../ui/dashboard/history";
import {
    AGENT_CHARTS,
    buildCharts,
    buildSampleTable,
    cellText,
    DEFAULT_HOURS,
    keepFocusWithin,
    NODE_CHARTS,
    throttleSummary,
    TrendPanel,
    type HistoryReader,
} from "../ui/dashboard/trendPanel";

const T0 = 1_700_000_000;

/** ``n`` agent samples a minute apart, counters rising. */
function agentRows(n: number, over: (i: number) => Record<string, unknown> = () => ({})): HistoryRow[] {
    return Array.from({ length: n }, (_, i) => ({
        ts: T0 + i * 60,
        messages_processed: i * 3,
        errors: 0,
        memory_mb: 0.5,
        queue_wait_p95_s: 0.01,
        message_p95_s: 0.2,
        task_p95_s: null,
        cost_usd: 0,
        ...over(i),
    }));
}

function nodeRows(n: number, over: (i: number) => Record<string, unknown> = () => ({})): HistoryRow[] {
    return Array.from({ length: n }, (_, i) => ({
        ts: T0 + i * 60,
        cpu_pct: 10 + i,
        mem_used_mb: 800,
        mem_free_mb: 7000,
        load_1m: 0.2,
        disk_free_mb: 400_000,
        temp_c: null,
        agents: 1,
        throttled: [],
        ...over(i),
    }));
}

const titles = (charts: HTMLElement[]): string[] =>
    charts.map(c => c.querySelector("figcaption")?.textContent ?? "");

describe("buildCharts", () => {
    it("draws an agent's activity, timings and state, leaving out spend it never had", () => {
        expect(titles(buildCharts(AGENT_CHARTS, agentRows(5)))).toEqual([
            "Messages per minute",
            "Errors per minute",
            "Time to handle a message (p95)",
            "State size",
        ]);
    });

    it("draws spend per hour once an agent spends", () => {
        const charts = buildCharts(
            AGENT_CHARTS,
            agentRows(3, i => ({ cost_usd: i * 0.001 })),
        );
        expect(titles(charts)).toContain("Spend per hour");
    });

    it("draws only the timing lines that were measured", () => {
        const charts = buildCharts(AGENT_CHARTS, agentRows(3));
        const timings = charts.find(c => c.textContent?.includes("p95"));
        expect([...timings!.querySelectorAll(".af-chart-legend-item")].map(i => i.textContent)).toEqual([
            "waiting",
            "handling",
        ]);
    });

    it("draws a node's machine, leaving out a temperature it cannot read", () => {
        expect(titles(buildCharts(NODE_CHARTS, nodeRows(3)))).toEqual([
            "CPU",
            "Memory",
            "Load (1 min)",
            "Disk free",
            "Agents",
        ]);
        expect(
            titles(
                buildCharts(
                    NODE_CHARTS,
                    nodeRows(3, () => ({ temp_c: 55 })),
                ),
            ),
        ).toContain("CPU temperature");
    });

    it("counts whole numbers without a decimal", () => {
        const charts = buildCharts(
            NODE_CHARTS,
            nodeRows(3, i => ({ agents: i })),
        );
        const agents = charts.find(c => c.querySelector("figcaption")?.textContent === "Agents")!;
        expect([...agents.querySelectorAll(".af-chart-axis span")].map(s => s.textContent)).toEqual([
            "2",
            "0",
        ]);
    });

    it("formats each chart in its own unit", () => {
        const charts = buildCharts(
            NODE_CHARTS,
            nodeRows(3, () => ({ mem_used_mb: 2048, temp_c: 55 })),
        );
        const axis = (title: string): string =>
            charts
                .find(c => c.querySelector("figcaption")?.textContent === title)!
                .querySelector(".af-chart-axis")!.textContent ?? "";
        expect(axis("CPU")).toContain("12%");
        expect(axis("Memory")).toContain("GB");
        expect(axis("CPU temperature")).toContain("55°C");
        const agent = buildCharts(
            AGENT_CHARTS,
            agentRows(3, i => ({ memory_mb: 2, message_p95_s: 1.5, cost_usd: i * 2 })),
        );
        const text = agent.map(c => c.textContent).join(" ");
        expect(text).toContain("2.0 MB");
        expect(text).toContain("1.5 s");
        expect(text).toContain("/h");
    });
});

describe("throttleSummary", () => {
    it("counts the samples each flag was raised in", () => {
        const rows = nodeRows(4, i => ({ throttled: i < 2 ? ["under_voltage"] : i === 2 ? null : [] }));
        expect(throttleSummary(rows)).toBe("Throttled: under voltage in 2 of 4 samples");
    });

    it("says nothing for a node never held back", () => {
        expect(throttleSummary(nodeRows(3))).toBe("");
    });
});

describe("buildSampleTable", () => {
    it("lists the latest samples first, lists as text, gaps as a dash", () => {
        const table = buildSampleTable(
            nodeRows(20, i => ({ throttled: i === 19 ? ["throttled"] : [] })),
            ["cpu_pct", "temp_c", "throttled"],
        );
        const rows = table.querySelectorAll("tbody tr");
        expect(rows.length).toBe(15);
        const first = [...rows[0]!.querySelectorAll("td")].map(td => td.textContent);
        expect(first.slice(1)).toEqual(["29", "—", "throttled"]);
    });
});

describe("TrendPanel", () => {
    let read: ReturnType<typeof vi.fn<HistoryReader>>;
    let panel: TrendPanel;

    beforeEach(() => {
        document.body.innerHTML = "";
        read = vi.fn<HistoryReader>(async () => agentRows(5));
        panel = new TrendPanel(read);
    });

    afterEach(() => panel.destroy());

    it("opens on the last hour of an agent, as a labelled dialog", async () => {
        await panel.open("agents", "weather");

        expect(read).toHaveBeenCalledWith("agents", "weather", DEFAULT_HOURS);
        const dialog = document.querySelector<HTMLElement>("[role=dialog]")!;
        expect(dialog.getAttribute("aria-label")).toBe("History of weather");
        expect(dialog.querySelector("h3")?.textContent).toBe("weather");
        expect(dialog.querySelectorAll(".af-chart").length).toBe(4);
        expect(dialog.querySelector(".af-trend-table")).not.toBeNull();
        expect(panel.isOpen).toBe(true);
    });

    it("reads another window when one is picked, and marks it pressed", async () => {
        await panel.open("agents", "weather");
        const week = document.querySelector<HTMLButtonElement>('.af-trend-window[data-hours="168"]')!;

        week.click();
        await vi.waitFor(() => expect(read).toHaveBeenLastCalledWith("agents", "weather", 168));

        expect(week.getAttribute("aria-pressed")).toBe("true");
        expect(document.querySelector('.af-trend-window[data-hours="1"]')!.getAttribute("aria-pressed")).toBe(
            "false",
        );
    });

    it("keeps the previous charts, dimmed, while the next window loads", async () => {
        await panel.open("agents", "weather");
        let release: (rows: HistoryRow[]) => void = () => {};
        read.mockImplementationOnce(() => new Promise(resolve => (release = resolve)));

        document.querySelector<HTMLButtonElement>('.af-trend-window[data-hours="24"]')!.click();
        const body = document.querySelector(".af-trend-body")!;
        expect(body.classList.contains("af-trend-loading")).toBe(true);
        expect(body.querySelectorAll(".af-chart").length).toBe(4);

        release(agentRows(2));
        await vi.waitFor(() => expect(body.classList.contains("af-trend-loading")).toBe(false));
    });

    it("drops an answer that arrives after another node was opened", async () => {
        let release: (rows: HistoryRow[]) => void = () => {};
        read.mockImplementationOnce(() => new Promise(resolve => (release = resolve)));
        const first = panel.open("nodes", "slow");
        read.mockResolvedValueOnce(nodeRows(3));
        await panel.open("nodes", "fast");

        release(nodeRows(3, () => ({ cpu_pct: 99 })));
        await first;

        expect(document.querySelector("h3")?.textContent).toBe("fast");
        expect(document.querySelectorAll(".af-trend-overlay").length).toBe(1);
    });

    it("says a node was throttled above its charts", async () => {
        read.mockResolvedValueOnce(nodeRows(3, () => ({ throttled: ["under_voltage"] })));
        await panel.open("nodes", "rpi");
        expect(document.querySelector(".af-trend-warning")?.textContent).toBe(
            "⚠ Throttled: under voltage in 3 of 3 samples",
        );
        expect(document.querySelector(".af-trend-kind")?.textContent).toBe("node");
    });

    it("says when there is no history, or none in the window", async () => {
        read.mockResolvedValueOnce(null);
        await panel.open("agents", "a");
        expect(document.querySelector(".af-trend-body")?.textContent).toBe("The history is not available.");
        read.mockResolvedValueOnce([]);
        await panel.open("agents", "a");
        expect(document.querySelector(".af-trend-body")?.textContent).toBe("No samples in this window yet.");
    });

    it("closes from its button, the backdrop and Escape, but not a click inside", async () => {
        await panel.open("agents", "a");
        document.querySelector<HTMLElement>(".af-trend-panel")!.click();
        expect(panel.isOpen).toBe(true);
        document.querySelector<HTMLButtonElement>(".af-trend-close")!.click();
        expect(panel.isOpen).toBe(false);

        await panel.open("agents", "a");
        document.querySelector<HTMLElement>(".af-trend-overlay")!.click();
        expect(panel.isOpen).toBe(false);

        await panel.open("agents", "a");
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
        expect(panel.isOpen).toBe(true);
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
        expect(panel.isOpen).toBe(false);
        expect(document.querySelector(".af-trend-overlay")).toBeNull();
    });

    it("loads nothing once closed", async () => {
        await panel.open("agents", "a");
        panel.close();
        read.mockClear();
        await (panel as unknown as { _load(): Promise<void> })._load();
        expect(read).not.toHaveBeenCalled();
    });
});

describe("cellText", () => {
    it("says a value as text, a list joined, anything else as a dash", () => {
        expect(cellText(3)).toBe("3");
        expect(cellText(true)).toBe("true");
        expect(cellText(["a", 2, { x: 1 }])).toBe("a, 2, —");
        expect(cellText(null)).toBe("—");
        expect(cellText({ x: 1 })).toBe("—");
    });
});

describe("focus in the panel", () => {
    let panel: TrendPanel;

    beforeEach(() => {
        document.body.innerHTML = "";
        panel = new TrendPanel(vi.fn<HistoryReader>(async () => agentRows(3)));
    });

    afterEach(() => panel.destroy());

    const tab = (shiftKey = false): KeyboardEvent => {
        const e = new KeyboardEvent("keydown", { key: "Tab", shiftKey, cancelable: true });
        document.dispatchEvent(e);
        return e;
    };

    it("starts on the close button and goes back to what opened it", async () => {
        const opener = document.body.appendChild(document.createElement("button"));
        opener.focus();

        await panel.open("agents", "a");
        expect(document.activeElement?.className).toContain("af-trend-close");

        panel.close();
        expect(document.activeElement).toBe(opener);
    });

    it("gives focus back to nothing that has gone away", async () => {
        const opener = document.body.appendChild(document.createElement("button"));
        opener.focus();
        await panel.open("agents", "a");
        opener.remove();

        expect(() => panel.close()).not.toThrow();
    });

    it("keeps Tab inside the dialog, both ways", async () => {
        await panel.open("agents", "a");
        const items = [
            ...document.querySelectorAll<HTMLElement>(
                ".af-trend-overlay button, .af-trend-overlay summary, .af-trend-overlay [tabindex='0']",
            ),
        ];
        const first = items[0]!;
        const last = items[items.length - 1]!;

        last.focus();
        expect(tab().defaultPrevented).toBe(true);
        expect(document.activeElement).toBe(first);

        expect(tab(true).defaultPrevented).toBe(true);
        expect(document.activeElement).toBe(last);

        items[1]!.focus();
        expect(tab().defaultPrevented).toBe(false);
    });

    it("brings focus that wandered outside back in", async () => {
        const outside = document.body.appendChild(document.createElement("button"));
        await panel.open("agents", "a");
        outside.focus();

        tab();

        expect(document.activeElement?.className).toContain("af-trend-close");
    });

    it("does nothing in a container with nothing to focus", () => {
        const empty = document.createElement("div");
        const e = new KeyboardEvent("keydown", { key: "Tab", cancelable: true });
        keepFocusWithin(empty, e);
        expect(e.defaultPrevented).toBe(false);
    });
});
