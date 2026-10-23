/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import {
    buildLegend,
    buildLineChart,
    buildSparkline,
    ceiling,
    downsample,
    extent,
    GAP_S,
    lastValue,
    levels,
    nearest,
    pathData,
    perMinute,
    samplesOf,
    SERIES_COLORS,
    timeLabel,
    tooltipRows,
    withGaps,
    type Point,
    type Sample,
    type Series,
} from "../ui/dashboard/trend";

const at = (minute: number): number => 1_700_000_000 + minute * 60;

describe("samplesOf", () => {
    it("takes one field, and anything that is not a finite number as a gap", () => {
        const rows = [
            { ts: 1, cpu_pct: 5 },
            { ts: 2, cpu_pct: null },
            { ts: 3, cpu_pct: "high" },
            { ts: 4, cpu_pct: Number.NaN },
            { ts: 5 },
        ];
        expect(samplesOf(rows, "cpu_pct")).toEqual([
            [1, 5],
            [2, null],
            [3, null],
            [4, null],
            [5, null],
        ]);
    });
});

describe("perMinute", () => {
    it("turns a counter into its rate per minute", () => {
        const samples: Sample[] = [
            [at(0), 10],
            [at(1), 13],
            [at(2), 13],
        ];
        expect(perMinute(samples).map(p => p.v)).toEqual([3, 0]);
    });

    it("draws a restart as a gap, not a negative rate", () => {
        const samples: Sample[] = [
            [at(0), 50],
            [at(1), 2],
            [at(2), 5],
        ];
        expect(perMinute(samples).map(p => p.v)).toEqual([null, 3]);
    });

    it("draws a step across missing samples as a gap", () => {
        const samples: Sample[] = [
            [at(0), 1],
            [at(0) + GAP_S + 1, 9],
        ];
        expect(perMinute(samples)[0]?.v).toBeNull();
    });

    it("draws a step from or to an unknown value as a gap", () => {
        const samples: Sample[] = [
            [at(0), null],
            [at(1), 4],
            [at(2), null],
        ];
        expect(perMinute(samples).map(p => p.v)).toEqual([null, null]);
    });

    it("needs two samples for one rate", () => {
        expect(perMinute([[at(0), 1]])).toEqual([]);
    });
});

describe("levels and withGaps", () => {
    it("keeps values as they are, in milliseconds", () => {
        expect(levels([[at(0), 7]])).toEqual([{ t: at(0) * 1000, v: 7 }]);
    });

    it("breaks the line where samples stopped for a while", () => {
        const points: Point[] = [
            { t: 0, v: 1 },
            { t: (GAP_S + 1) * 1000, v: 2 },
        ];
        expect(withGaps(points).map(p => p.v)).toEqual([1, null, 2]);
    });
});

describe("downsample", () => {
    it("leaves a short series alone", () => {
        const points = [{ t: 1, v: 1 }];
        expect(downsample(points, 10)).toEqual(points);
    });

    it("averages each slice, and keeps a slice without values a gap", () => {
        const points: Point[] = [
            { t: 1, v: 2 },
            { t: 2, v: 4 },
            { t: 3, v: null },
            { t: 4, v: null },
        ];
        expect(downsample(points, 2)).toEqual([
            { t: 2, v: 3 },
            { t: 4, v: null },
        ]);
    });
});

describe("extent, lastValue and nearest", () => {
    it("spans every series and skips gaps", () => {
        expect(
            extent([
                [
                    { t: 1, v: 3 },
                    { t: 2, v: null },
                ],
                [{ t: 1, v: -1 }],
            ]),
        ).toEqual([-1, 3]);
        expect(extent([[{ t: 1, v: null }]])).toBeNull();
    });

    it("finds the last value that is not a gap", () => {
        expect(
            lastValue([
                { t: 1, v: 4 },
                { t: 2, v: null },
            ]),
        ).toBe(4);
        expect(lastValue([])).toBeNull();
    });

    it("finds the point nearest in time", () => {
        const points = [
            { t: 0, v: 1 },
            { t: 100, v: 2 },
        ];
        expect(nearest(points, 70)?.v).toBe(2);
        expect(nearest([], 70)).toBeUndefined();
    });
});

describe("pathData", () => {
    const x = (t: number): number => t;
    const y = (v: number): number => v;

    it("starts a new run after every gap", () => {
        const d = pathData(
            [
                { t: 0, v: 1 },
                { t: 1, v: 2 },
                { t: 2, v: null },
                { t: 3, v: 4 },
            ],
            x,
            y,
        );
        expect(d).toBe("M0.0 1.0h0.1 L1.0 2.0 M3.0 4.0h0.1");
    });

    it("is empty without values", () => {
        expect(pathData([{ t: 0, v: null }], x, y)).toBe("");
    });
});

describe("buildSparkline", () => {
    it("draws one line and names itself for a screen reader", () => {
        const svg = buildSparkline(
            [
                { t: 0, v: 1 },
                { t: 60_000, v: 3 },
            ],
            "cpu",
        );
        expect(svg.getAttribute("aria-label")).toBe("cpu");
        expect(svg.querySelectorAll("path").length).toBe(1);
        expect(svg.querySelector("path")?.getAttribute("stroke")).toBe(SERIES_COLORS[0]);
    });

    it("draws nothing at all without a value, rather than a zero", () => {
        expect(buildSparkline([{ t: 0, v: null }], "cpu").querySelector("path")).toBeNull();
        expect(buildSparkline([], "cpu").querySelector("path")).toBeNull();
    });

    it("draws a flat line mid-height", () => {
        const d = buildSparkline(
            [
                { t: 0, v: 5 },
                { t: 1, v: 5 },
            ],
            "flat",
        )
            .querySelector("path")
            ?.getAttribute("d");
        expect(d).toContain(" 2.0");
    });
});

describe("timeLabel", () => {
    it("is a time of day within a day, and adds the date beyond one", () => {
        const t = new Date(2026, 9, 7, 9, 5).getTime();
        expect(timeLabel(t, 3600_000)).toBe("09:05");
        expect(timeLabel(t, 7 * 24 * 3600_000)).toBe("7/10 09:05");
    });
});

const two: Series[] = [
    {
        name: "waiting",
        color: SERIES_COLORS[0],
        points: [
            { t: 0, v: 0.01 },
            { t: 60_000, v: 0.02 },
        ],
    },
    {
        name: "handling",
        color: SERIES_COLORS[1],
        points: [
            { t: 0, v: 0.2 },
            { t: 60_000, v: null },
        ],
    },
];
const ms = (v: number): string => `${(v * 1000).toFixed(0)} ms`;

describe("tooltipRows", () => {
    it("lists every series at a time, a dash for a gap", () => {
        expect(tooltipRows(two, 60_000, ms)).toEqual([
            ["waiting", SERIES_COLORS[0], "20 ms"],
            ["handling", SERIES_COLORS[1], "—"],
        ]);
    });
});

describe("buildLineChart", () => {
    it("draws a line per series, the axis range, and a legend for two or more", () => {
        const chart = buildLineChart(two, { title: "p95", format: ms });
        expect(chart.querySelector("figcaption")?.textContent).toBe("p95");
        expect(chart.querySelectorAll("path.af-trend-line").length).toBe(2);
        expect([...chart.querySelectorAll(".af-chart-axis span")].map(s => s.textContent)).toEqual([
            "200 ms",
            "0 ms",
        ]);
        expect([...chart.querySelectorAll(".af-chart-legend-item")].map(i => i.textContent)).toEqual([
            "waiting",
            "handling",
        ]);
    });

    it("has no legend for one series: the title names it", () => {
        const one = two.slice(0, 1);
        expect(
            buildLineChart(one, { title: "wait", format: ms }).querySelector(".af-chart-legend"),
        ).toBeNull();
    });

    it("keeps its own range when zero need not be in view", () => {
        const temps: Series[] = [
            {
                name: "t",
                color: SERIES_COLORS[0],
                points: [
                    { t: 0, v: 50 },
                    { t: 1, v: 60 },
                ],
            },
        ];
        const chart = buildLineChart(temps, { title: "temp", format: v => `${v}`, fromZero: false });
        expect([...chart.querySelectorAll(".af-chart-axis span")].map(s => s.textContent)).toEqual([
            "60",
            "50",
        ]);
    });

    it("says so when the window holds nothing", () => {
        const empty = buildLineChart([{ name: "x", color: "#fff", points: [] }], { title: "x", format: ms });
        expect(empty.querySelector(".af-chart-empty")?.textContent).toBe("No samples in this window");
    });

    it("shows every series at the pointer, as text, and hides again on leaving", () => {
        const hostile: Series[] = [{ ...two[0]!, name: "<img src=x onerror=alert(1)>" }];
        const chart = buildLineChart(hostile, { title: "p95", format: ms });
        document.body.appendChild(chart);
        const plot = chart.querySelector<HTMLElement>(".af-chart-plot")!;
        const tip = chart.querySelector<HTMLElement>(".af-chart-tip")!;
        const cross = chart.querySelector<SVGLineElement>(".af-chart-crosshair")!;

        plot.dispatchEvent(new MouseEvent("pointermove", { clientX: 10 }));
        expect(tip.hidden).toBe(false);
        expect(cross.style.display).toBe("");
        expect(tip.querySelector("img")).toBeNull();
        expect(tip.textContent).toContain("<img src=x onerror=alert(1)>");

        plot.dispatchEvent(new MouseEvent("pointerleave"));
        expect(tip.hidden).toBe(true);
        expect(cross.style.display).toBe("none");
    });

    it("gives the same readout from the keyboard", () => {
        const chart = buildLineChart(two, { title: "p95", format: ms });
        document.body.appendChild(chart);
        const plot = chart.querySelector<HTMLElement>(".af-chart-plot")!;
        expect(plot.tabIndex).toBe(0);
        plot.dispatchEvent(new FocusEvent("focus"));
        const tip = chart.querySelector<HTMLElement>(".af-chart-tip")!;
        expect(tip.hidden).toBe(false);
        expect(tip.textContent).toContain("20 ms");
        plot.dispatchEvent(new FocusEvent("blur"));
        expect(tip.hidden).toBe(true);
    });
});

describe("buildLegend", () => {
    it("keys each series with a stroke of its color", () => {
        const legend = buildLegend(two);
        const keys = [...legend.querySelectorAll<HTMLElement>(".af-chart-key")].map(k => k.style.background);
        expect(keys.length).toBe(2);
    });
});

describe("ceiling", () => {
    it("is the top of the values when they have a range", () => {
        expect(ceiling(0, 5)).toBe(5);
    });

    it("gives a flat series room above it: zeros lie on the baseline under a one", () => {
        expect(ceiling(0, 0)).toBe(1);
        expect(ceiling(50, 50)).toBe(60);
        expect(ceiling(-5, -5)).toBe(-4);
    });

    it("labels a chart of zeros from zero to one", () => {
        const chart = buildLineChart(
            [
                {
                    name: "errors",
                    color: SERIES_COLORS[0],
                    points: [
                        { t: 0, v: 0 },
                        { t: 1, v: 0 },
                    ],
                },
            ],
            { title: "errors", format: v => v.toFixed(0) },
        );
        expect([...chart.querySelectorAll(".af-chart-axis span")].map(s => s.textContent)).toEqual([
            "1",
            "0",
        ]);
    });
});
