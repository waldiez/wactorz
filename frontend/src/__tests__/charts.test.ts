/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The line chart's additions as a person meets them: what is marked on it,
 * how its points are drawn and the control that switches it, and zooming into
 * a window of its time, by drag, by button, and by asking its owner again.
 */
import { describe, expect, it, vi } from "vitest";

import { LineChart, frameOf, narrowed, spanOf, type ChartOptions, type Series } from "../ui/charts";
import { modePath, plotScales, spreadPath, spreadRuns, widened } from "../ui/charts/geometry";
import { drawOver, legendMarks, tooltipExtras, visibleBands } from "../ui/charts/marks";
import { autoMode, modeToggle } from "../ui/charts/mode";
import { clamp, windowBetween, wireDrag, zoomedIn, zoomedOut } from "../ui/charts/zoom";
import { allByRole, byRole } from "./queries";

const T0 = 1_760_000_000_000;
const MIN = 60_000;

const OPTIONS: ChartOptions = { title: "Score", format: v => v.toFixed(2) };

/** One series of ``values`` a minute apart, null for a gap. */
function series(values: (number | null)[], name = "active", color = "#3987e5"): Series {
    return { name, color, points: values.map((v, i) => ({ t: T0 + i * MIN, v })) };
}

/** ``node``'s box as a layout engine would give it, which happy-dom does not. */
function boxed(node: Element, left = 0, width = 200): void {
    node.getBoundingClientRect = () =>
        ({ left, width, top: 0, height: 90, right: left + width, bottom: 90, x: left, y: 0 }) as DOMRect;
}

function pointer(type: string, clientX: number, pointerType = "mouse"): Event {
    const event = new MouseEvent(type, { clientX, button: 0, bubbles: true });
    Object.defineProperty(event, "pointerType", { value: pointerType });
    Object.defineProperty(event, "pointerId", { value: 1 });
    return event;
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

// ── Geometry ─────────────────────────────────────────────────────────────────

describe("the geometry", () => {
    const frame = { t0: T0, t1: T0 + 4 * MIN, lo: 0, hi: 4 };
    const scale = plotScales(frame);

    it("widens a range to take in thresholds and spreads", () => {
        expect(widened([1, 2], [0.5, 3])).toEqual([0.5, 3]);
        expect(widened(null, [2])).toEqual([2, 2]);
        expect(widened(null, [])).toBeNull();
    });

    it("draws a line broken at gaps, points apart, or joined across gaps", () => {
        const points = series([1, 2, null, 3]).points;

        expect(modePath(points, "line", scale).match(/M/g)).toHaveLength(2);
        expect(modePath(points, "points", scale).match(/M/g)).toHaveLength(3);
        expect(modePath(points, "joined", scale).match(/M/g)).toHaveLength(1);
    });

    it("cuts a spread where a step is well over the usual one", () => {
        const spread = [0, 1, 2, 6, 7].map(i => ({ t: T0 + i * MIN, lo: 0, hi: 1 }));

        expect(spreadRuns(spread).map(run => run.length)).toEqual([3, 2]);
        expect(spreadPath(spread, scale).match(/Z/g)).toHaveLength(2);
        expect(spreadPath([], scale)).toBe("");
    });
});

// ── Marks ────────────────────────────────────────────────────────────────────

describe("what is marked on a chart", () => {
    const marked: ChartOptions = {
        ...OPTIONS,
        thresholds: [{ value: 3, label: "threshold" }],
        bands: [
            { from: T0 + MIN, to: T0 + 2 * MIN, label: "jul23", tone: "warn" },
            { from: T0 + 100 * MIN, to: T0 + 101 * MIN, label: "later" },
        ],
        marks: [
            { t: T0 + 2 * MIN, v: 4, label: "AE alert", shape: "square" },
            { t: T0 + 3 * MIN, label: "AE alert", shape: "square" },
            { t: T0 + 3 * MIN, label: "cycling", shape: "triangle", color: "#f5c542" },
        ],
    };

    it("draws thresholds and bands under the lines, and only the bands in the window", () => {
        const chart = new LineChart([series([1, 2, 1, 2, 1])], marked);
        const drawing = chart.element.querySelector("svg") as SVGSVGElement;

        expect(drawing.querySelectorAll(".af-chart-threshold")).toHaveLength(1);
        expect(drawing.querySelectorAll("rect.af-chart-band.warn")).toHaveLength(1);
        expect(chart.element.querySelector(".af-chart-band-label")?.textContent).toBe("jul23");
    });

    it("keeps a threshold above every value in view", () => {
        expect(frameOf([series([1, 2])], marked, null)?.hi).toBe(3);
    });

    it("places each mark in its shape over the plot, hidden from a screen reader that reads the legend", () => {
        const chart = new LineChart([series([1, 2, 1, 2, 1])], marked);
        const over = chart.element.querySelector(".af-chart-over") as HTMLElement;

        expect(over.getAttribute("aria-hidden")).toBe("true");
        expect([...over.querySelectorAll(".af-chart-mark")].map(m => m.className)).toEqual([
            "af-chart-mark square",
            "af-chart-mark square",
            "af-chart-mark triangle",
        ]);
        const legend = chart.element.querySelector(".af-chart-legend")?.textContent;
        expect(legend).toContain("threshold");
        expect(legend).toContain("AE alert (2)");
        expect(legend).toContain("cycling (1)");
    });

    it("gives a lone series a legend when something is marked, and none when nothing is", () => {
        expect(new LineChart([series([1, 2])], OPTIONS).element.querySelector(".af-chart-legend")).toBeNull();
    });

    it("says in the tooltip the band and the marks near the time under the pointer", () => {
        const frame = { t0: T0, t1: T0 + 4 * MIN, lo: 0, hi: 4 };

        expect(tooltipExtras(marked, frame, T0 + 1.5 * MIN)).toEqual(["jul23"]);
        expect(tooltipExtras(marked, frame, T0 + 3 * MIN)).toEqual(["AE alert", "cycling"]);
        expect(tooltipExtras({ ...OPTIONS }, frame, T0)).toEqual([]);
    });

    it("draws a mark with no height at the top, and in the first series' color", () => {
        const frame = { t0: T0, t1: T0 + 4 * MIN, lo: 0, hi: 4 };
        const over = drawOver(marked, [series([1])], frame, plotScales(frame));
        const top = over.querySelectorAll<HTMLElement>(".af-chart-mark")[1] as HTMLElement;

        expect(top.style.top).toBe("4.44%");
        expect(top.style.color).toBe("#3987e5");
        expect(legendMarks({ ...OPTIONS, marks: [] }, [], frame)).toEqual([]);
        expect(visibleBands(marked.bands ?? [], frame).map(b => b.label)).toEqual(["jul23"]);
    });

    it("draws each series' spread as a band of its color", () => {
        const spread = [0, 1, 2].map(i => ({ t: T0 + i * MIN, lo: 0.5, hi: 2.5 }));
        const chart = new LineChart([{ ...series([1, 2, 1]), spread }], OPTIONS);
        const band = chart.element.querySelector(".af-chart-spread");

        expect(band?.getAttribute("fill")).toBe("#3987e5");
        expect(frameOf([{ ...series([1, 2, 1]), spread }], OPTIONS, null)?.hi).toBe(2.5);
    });
});

// ── How points are drawn ─────────────────────────────────────────────────────

describe("how a chart's points are drawn", () => {
    it("comes to points when most slots are empty, and a line otherwise", () => {
        expect(autoMode([[...series([1, null, null, null]).points]])).toBe("points");
        expect(autoMode([[...series([1, 2, null]).points]])).toBe("line");
        expect(autoMode([])).toBe("line");
    });

    it("joins across lone empty slots that come of more slots than readings, but not across a real gap", () => {
        const aliased = [1, null, 2, 3, null, 4, null, 5, 6, null, 7];
        const stopped = [1, null, 2, 3, null, null, null, 5, 6, null, 7];

        expect(autoMode([[...series(aliased).points]])).toBe("joined");
        expect(autoMode([[...series(stopped).points]])).toBe("line");
        expect(autoMode([[...series([1, 2, 3, null, 4, 5, 6, 7, 8, 9]).points]])).toBe("line");
    });

    it("offers the modes as a radio group, the current one checked and the only one in the Tab order", () => {
        const chosen = vi.fn();
        const group = modeToggle("points", chosen);

        expect(group.getAttribute("role")).toBe("radiogroup");
        const radios = allByRole(group, "radio");
        expect(radios.map(r => [r.textContent, r.getAttribute("aria-checked"), r.tabIndex])).toEqual([
            ["Line", "false", -1],
            ["Points", "true", 0],
            ["Joined", "false", -1],
        ]);
        byRole(group, "radio", "Joined").click();
        expect(chosen).toHaveBeenLastCalledWith("joined");
    });

    it("moves between the modes with the arrow keys, round the ends", () => {
        const chosen = vi.fn();
        const group = modeToggle("line", chosen);

        group.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
        expect(chosen).toHaveBeenLastCalledWith("joined");
        group.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
        expect(chosen).toHaveBeenLastCalledWith("points");
        group.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        expect(chosen).toHaveBeenCalledTimes(2);
    });

    it("draws a sparse chart as points when left to choose, and redraws as chosen", () => {
        const chart = new LineChart([series([1, null, null, null, null, 2, null])], {
            ...OPTIONS,
            mode: "auto",
            modeToggle: true,
        });
        const drawn = () => chart.element.querySelector(".af-trend-line")?.getAttribute("class");

        expect(chart.mode()).toBe("points");
        expect(drawn()).toBe("af-trend-line points");
        byRole(chart.element, "radio", "Joined").click();
        expect(chart.mode()).toBe("joined");
        expect(drawn()).toBe("af-trend-line joined");
    });

    it("has no controls where none were asked for", () => {
        const chart = new LineChart([series([1, 2])], OPTIONS);

        expect(chart.element.querySelector(".af-chart-controls")?.children).toHaveLength(0);
        expect(chart.mode()).toBe("line");
    });
});

// ── Zoom ─────────────────────────────────────────────────────────────────────

describe("the zoom arithmetic", () => {
    const whole = { from: 0, to: 1_000_000 };

    it("keeps a window inside the whole, and no narrower than a thousandth of it or a second", () => {
        const day = { from: 0, to: 86_400_000 };

        expect(clamp({ from: -50, to: 2_000_000 }, whole)).toEqual(whole);
        expect(clamp({ from: 500, to: 500 }, { from: 0, to: 1000 })).toEqual({ from: 0, to: 1000 });
        expect(clamp({ from: 43_200_000, to: 43_200_000 }, day)).toEqual({
            from: 43_156_800,
            to: 43_243_200,
        });
        expect(clamp({ from: 5_000, to: 5_000 }, { from: 0, to: 600_000 })).toEqual({
            from: 4_500,
            to: 5_500,
        });
    });

    it("zooms in to the middle half and out to twice as wide, and says when out reaches the whole", () => {
        expect(zoomedIn(whole, whole)).toEqual({ from: 250_000, to: 750_000 });
        expect(zoomedOut({ from: 400_000, to: 600_000 }, whole)).toEqual({ from: 300_000, to: 700_000 });
        expect(zoomedOut({ from: 250_000, to: 750_000 }, whole)).toBeNull();
    });

    it("turns two shares of the width, in either order, into a window", () => {
        expect(windowBetween(0.75, 0.25, whole)).toEqual({ from: 250_000, to: 750_000 });
    });
});

describe("zooming a chart", () => {
    const options: ChartOptions = { ...OPTIONS, zoom: true };
    const values = [1, 2, 3, 4, 5, 6, 7, 8, 9];

    it("narrows to the points inside the window, and the buttons follow whether it is zoomed", async () => {
        const chart = new LineChart([series(values)], options);
        const reset = () => byRole(chart.element, "button", "Reset zoom") as HTMLButtonElement;
        expect(reset().disabled).toBe(true);

        byRole(chart.element, "button", "Zoom in").click();
        await flush();

        expect(chart.zoomed()).toEqual({ from: T0 + 2 * MIN, to: T0 + 6 * MIN });
        expect(chart.element.querySelector(".af-chart-axis")?.textContent).toBe("7.004.002.000.00");
        expect(reset().disabled).toBe(false);
        byRole(chart.element, "button", "Zoom out").click();
        await flush();
        expect(chart.zoomed()).toBeNull();
    });

    it("selects a window with a mouse drag, and a double click goes back to everything", async () => {
        const chart = new LineChart([series(values)], options);
        const canvas = chart.element.querySelector(".af-chart-canvas") as HTMLElement;
        boxed(canvas);

        canvas.dispatchEvent(pointer("pointerdown", 50));
        canvas.dispatchEvent(pointer("pointermove", 150));
        expect((chart.element.querySelector(".af-chart-selection") as HTMLElement).hidden).toBe(false);
        canvas.dispatchEvent(pointer("pointerup", 150));
        await flush();

        expect(chart.zoomed()).toEqual({ from: T0 + 2 * MIN, to: T0 + 6 * MIN });
        chart.element.querySelector(".af-chart-canvas")?.dispatchEvent(new MouseEvent("dblclick"));
        await flush();
        expect(chart.zoomed()).toBeNull();
    });

    it("leaves a touch to scroll, and takes a short drag as a click", () => {
        const canvas = document.createElement("div");
        const shading = document.createElement("div");
        const select = vi.fn();
        boxed(canvas);
        wireDrag(canvas, shading, select);

        canvas.dispatchEvent(pointer("pointerdown", 20, "touch"));
        canvas.dispatchEvent(pointer("pointermove", 180, "touch"));
        canvas.dispatchEvent(pointer("pointerup", 180, "touch"));
        canvas.dispatchEvent(pointer("pointerdown", 100));
        canvas.dispatchEvent(pointer("pointerup", 101));

        expect(select).not.toHaveBeenCalled();
    });

    it("asks its owner for the window, draws the answer, and drops an answer a later zoom overtook", async () => {
        const answers: ((s: Series[]) => void)[] = [];
        const onZoom = vi.fn(
            () =>
                new Promise<Series[]>(resolve => {
                    answers.push(resolve);
                }),
        );
        const chart = new LineChart([series(values)], { ...options, onZoom });

        const first = chart.zoomTo({ from: T0, to: T0 + 4 * MIN });
        const second = chart.zoomTo({ from: T0 + 4 * MIN, to: T0 + 8 * MIN });
        answers[1]?.([series([40, 50], "finer")]);
        await second;
        answers[0]?.([series([10, 20], "stale")]);
        await first;

        expect(onZoom).toHaveBeenCalledTimes(2);
        expect(chart.element.querySelector(".af-chart-axis")?.textContent).toBe("50.0040.0020.000.00");
    });

    it("keeps its zoom when given new series, and asks again for the window", async () => {
        const onZoom = vi.fn(async () => undefined);
        const chart = new LineChart([series(values)], { ...options, onZoom });
        await chart.zoomTo({ from: T0, to: T0 + 2 * MIN });

        chart.update([series([9, 8, 7, 6])]);
        await flush();

        expect(chart.zoomed()).toEqual({ from: T0, to: T0 + 2 * MIN });
        expect(onZoom).toHaveBeenCalledTimes(2);
    });

    it("drops an answer that comes after it was ended", async () => {
        let answer: (s: Series[]) => void = () => undefined;
        const chart = new LineChart([series(values)], {
            ...options,
            onZoom: () => new Promise<Series[]>(resolve => (answer = resolve)),
        });
        document.body.appendChild(chart.element);

        const zooming = chart.zoomTo({ from: T0, to: T0 + 2 * MIN });
        chart.destroy();
        answer([series([99])]);
        await zooming;

        expect(chart.element.isConnected).toBe(false);
        expect(chart.element.querySelector(".af-chart-axis")?.textContent).not.toContain("99");
    });

    it("keeps a keyboard on the button it pressed, and moves it to Zoom in when that one is done", async () => {
        const chart = new LineChart([series(values)], options);
        document.body.appendChild(chart.element);
        const zoomIn = () => byRole(chart.element, "button", "Zoom in");

        zoomIn().focus();
        zoomIn().click();
        await flush();
        expect(document.activeElement).toBe(zoomIn());
        byRole(chart.element, "button", "Reset zoom").focus();
        byRole(chart.element, "button", "Reset zoom").click();
        await flush();

        expect(chart.zoomed()).toBeNull();
        expect(document.activeElement).toBe(zoomIn());
        chart.destroy();
    });

    it("keeps what it drew and says so when its owner cannot answer a zoom", async () => {
        const chart = new LineChart([series(values)], {
            ...options,
            onZoom: async window => {
                if (window) {
                    throw new Error("the server did not answer");
                }
            },
        });

        await chart.zoomTo({ from: T0, to: T0 + 4 * MIN });

        expect(chart.element.querySelector(".af-trend-line")).not.toBeNull();
        const note = byRole(chart.element, "status");
        expect(note.textContent).toBe("Could not read finer data for this window: the server did not answer");
        await chart.zoomTo(null);
        expect(allByRole(chart.element, "status")).toHaveLength(0);
    });

    it("says the finer data did not come even where the window it holds is empty", async () => {
        const chart = new LineChart([series([1, null, null, null, null, null, null, null, 2])], {
            ...options,
            onZoom: async () => {
                throw new Error("timed out");
            },
        });

        await chart.zoomTo({ from: T0 + 3 * MIN, to: T0 + 5 * MIN });

        expect(chart.element.textContent).toContain("No samples in this window");
        expect(byRole(chart.element, "status").textContent).toBe(
            "Could not read finer data for this window: timed out",
        );
    });

    it("drops the failure of a zoom a later one overtook", async () => {
        let fail: (e: unknown) => void = () => undefined;
        const chart = new LineChart([series(values)], {
            ...options,
            onZoom: window =>
                window?.from === T0 ? new Promise<Series[]>((_, reject) => (fail = reject)) : undefined,
        });

        const first = chart.zoomTo({ from: T0, to: T0 + 4 * MIN });
        await chart.zoomTo({ from: T0 + 4 * MIN, to: T0 + 8 * MIN });
        fail("late");
        await first;

        expect(allByRole(chart.element, "status")).toHaveLength(0);
    });

    it("says when the window holds nothing", async () => {
        const chart = new LineChart([series([1, null, null, null, null, null, null, null, 2])], options);
        await chart.zoomTo({ from: T0 + 3 * MIN, to: T0 + 5 * MIN });

        expect(chart.element.textContent).toContain("No samples in this window");
    });
});

describe("the tooltip", () => {
    it("lists every series and what is marked at the time under the pointer", () => {
        const chart = new LineChart([series([1, 2, 3]), series([3, 2, 1], "shadow", "#d95926")], {
            ...OPTIONS,
            bands: [{ from: T0, to: T0 + MIN, label: "fault" }],
        });
        const plot = chart.element.querySelector(".af-chart-plot") as HTMLElement;
        boxed(plot);
        boxed(plot.querySelector(".af-chart-canvas") as HTMLElement);

        plot.dispatchEvent(pointer("pointermove", 0));

        const tip = plot.querySelector(".af-chart-tip") as HTMLElement;
        expect(tip.hidden).toBe(false);
        expect(tip.textContent).toContain("1.00active");
        expect(tip.textContent).toContain("3.00shadow");
        expect(tip.textContent).toContain("fault");
    });
});

describe("the window helpers", () => {
    it("spans the points given, and narrows series and their spread", () => {
        const spread = [0, 5].map(i => ({ t: T0 + i * MIN, lo: 0, hi: 1 }));
        const given = [{ ...series([1, 2, 3, 4, 5, 6]), spread }];

        expect(spanOf(given)).toEqual({ from: T0, to: T0 + 5 * MIN });
        expect(spanOf([])).toBeNull();
        const inside = narrowed(given, { from: T0 + MIN, to: T0 + 5 * MIN });
        expect(inside[0]?.points.map(p => p.v)).toEqual([2, 3, 4, 5, 6]);
        expect(inside[0]?.spread).toHaveLength(1);
    });
});
