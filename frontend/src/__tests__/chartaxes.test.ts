/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A chart's axes: round values up the side and round local times along the
 * bottom, worked out for the window shown. The windows here are built from
 * local dates, so the ticks are the same whatever time zone runs the tests.
 */
import { describe, expect, it } from "vitest";

import { LineChart, type Series } from "../ui/charts";
import { firstOnStep, niceStep, timeStep, timeTicks, valueTicks } from "../ui/charts/axes";
import { spokenName } from "../ui/charts/lineChart";

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

/** A local moment: the 15th of January 2026 at ``hours``:``minutes``. */
const local = (hours: number, minutes = 0, day = 15): number =>
    new Date(2026, 0, day, hours, minutes).getTime();

const fixed = (digits: number) => (v: number) => v.toFixed(digits);

describe("value ticks", () => {
    it("steps by one, two, two and a half or five times a power of ten", () => {
        expect([0.3, 0.22, 0.07, 7, 1].map(niceStep)).toEqual([0.5, 0.25, 0.1, 10, 1]);
        expect([0, Number.NaN, -3].map(niceStep)).toEqual([1, 1, 1]);
    });

    it("lists the top, the round values between and the bottom", () => {
        const ticks = valueTicks({ t0: 0, t1: 1, lo: 0, hi: 200 }, v => `${v} ms`);

        expect(ticks.map(t => t.label)).toEqual(["200 ms", "150 ms", "100 ms", "50 ms", "0 ms"]);
    });

    it("leaves out a round value too near an end, so labels do not crowd", () => {
        const ticks = valueTicks({ t0: 0, t1: 1, lo: 0, hi: 1.1 }, fixed(1));

        expect(ticks.map(t => t.label)).toEqual(["1.1", "0.5", "0.0"]);
    });

    it("leaves out a value that reads as the one before it", () => {
        const ticks = valueTicks({ t0: 0, t1: 1, lo: 0, hi: 1 }, fixed(0));

        expect(ticks.map(t => t.label)).toEqual(["1", "0"]);
    });

    it("has only the one label for a flat chart", () => {
        expect(valueTicks({ t0: 0, t1: 1, lo: 5, hi: 5 }, fixed(0)).map(t => t.label)).toEqual(["5"]);
    });
});

describe("time ticks", () => {
    it("chooses the finest round step that gives no more than six", () => {
        const size = (span: number) => timeStep(span).ms;

        expect(size(30_000)).toBe(5_000);
        expect(size(90 * MIN)).toBe(15 * MIN);
        expect(size(3 * DAY)).toBe(12 * HOUR);
        expect(size(20 * DAY)).toBe(7 * DAY);
        expect(timeStep(400 * DAY)).toEqual({ unit: "month", count: 3, ms: 90 * DAY });
        expect(timeStep(10 * 365 * DAY)).toEqual({ unit: "year", count: 2, ms: 2 * 365 * DAY });
        expect(timeStep(1e6 * 365 * DAY).count).toBe(100);
    });

    it("falls on round local times: the next whole hour, the next midnight, the first of a month or a year", () => {
        expect(firstOnStep(local(10, 7), timeStep(4 * HOUR))).toBe(local(11));
        expect(firstOnStep(local(10, 7), timeStep(90 * MIN))).toBe(local(10, 15));
        expect(firstOnStep(local(10, 7), timeStep(5 * DAY))).toBe(local(0, 0, 16));
        expect(firstOnStep(local(0, 0, 16), timeStep(5 * DAY))).toBe(local(0, 0, 16));
        expect(firstOnStep(local(10, 7), timeStep(400 * DAY))).toBe(new Date(2026, 3, 1).getTime());
        expect(firstOnStep(local(10, 7), timeStep(10 * 365 * DAY))).toBe(new Date(2028, 0, 1).getTime());
    });

    it("labels a window under a minute by the second", () => {
        const ticks = timeTicks({ t0: local(11, 53) + 2_000, t1: local(11, 53) + 32_000, lo: 0, hi: 1 });

        expect(ticks.map(t => t.label)).toEqual([
            "11:53:05",
            "11:53:10",
            "11:53:15",
            "11:53:20",
            "11:53:25",
            "11:53:30",
        ]);
    });

    it("labels a window of a year or more by the month, then by the year, at no more than six or so", () => {
        const months = timeTicks({ t0: local(9, 0, 1), t1: new Date(2027, 3, 1).getTime(), lo: 0, hi: 1 });
        const decade = timeTicks({ t0: local(9), t1: new Date(2036, 0, 15).getTime(), lo: 0, hi: 1 });

        expect(months.map(t => t.label)).toEqual([
            "Apr 2026",
            "Jul 2026",
            "Oct 2026",
            "Jan 2027",
            "Apr 2027",
        ]);
        expect(decade.map(t => t.label)).toEqual(["2028", "2030", "2032", "2034", "2036"]);
    });

    it("labels a window within a day by the time of day", () => {
        const ticks = timeTicks({ t0: local(10, 7), t1: local(11, 37), lo: 0, hi: 1 });

        expect(ticks.map(t => t.label)).toEqual(["10:15", "10:30", "10:45", "11:00", "11:15", "11:30"]);
    });

    it("labels a window of weeks by the day, at midnights", () => {
        const ticks = timeTicks({ t0: local(9, 0, 1), t1: local(9, 0, 28), lo: 0, hi: 1 });

        expect(ticks.map(t => t.label)).toEqual(["2/1", "9/1", "16/1", "23/1"]);
        expect(ticks.every(t => new Date(t.at).getHours() === 0)).toBe(true);
    });

    it("gives its two ends, to the second, when no round time falls inside", () => {
        const ticks = timeTicks({ t0: local(10, 7) + 1_200, t1: local(10, 7) + 1_900, lo: 0, hi: 1 });

        expect(ticks.map(t => t.label)).toEqual(["10:07:01", "10:07:01"]);
        expect(ticks[0]?.at).toBe(local(10, 7) + 1_200);
    });
});

describe("a chart's axes", () => {
    const series: Series[] = [
        {
            name: "score",
            color: "#3987e5",
            points: Array.from({ length: 91 }, (_, i) => ({ t: local(10, 7) + i * MIN, v: i % 7 })),
        },
    ];

    it("draws a gridline at each round value and time, and labels each at its place", () => {
        const chart = new LineChart(series, { title: "Score", format: fixed(0) });
        const grid = chart.element.querySelectorAll("svg .af-chart-grid");
        const values = [...chart.element.querySelectorAll<HTMLElement>(".af-chart-axis span")];
        const times = [...chart.element.querySelectorAll<HTMLElement>(".af-chart-tick")];

        expect(values.map(v => v.textContent)).toEqual(["6", "4", "2", "0"]);
        expect(values[0]?.style.top).toBe("4.44%");
        expect(values.at(-1)?.style.top).toBe("95.56%");
        expect(times.map(t => t.textContent)).toEqual(["10:15", "10:30", "10:45", "11:00", "11:15", "11:30"]);
        expect(times.map(t => t.className.includes("odd"))).toEqual([false, true, false, true, false, true]);
        expect(grid).toHaveLength(values.length - 2 + times.length);
    });

    it("titles its axes when told what they are, and says so to a screen reader", () => {
        const options = {
            title: "Pump 1",
            format: fixed(2),
            yLabel: "reconstruction error",
            xLabel: "event time",
        };
        const chart = new LineChart(series, options);

        expect(chart.element.querySelector(".af-chart-ylabel")?.textContent).toBe("reconstruction error");
        expect(chart.element.querySelector(".af-chart-xlabel")?.textContent).toBe("event time");
        expect(chart.element.querySelector("svg")?.getAttribute("aria-label")).toBe(
            "Pump 1: reconstruction error over event time",
        );
        expect(spokenName({ title: "Pump 1", format: fixed(0), yLabel: "ms" })).toBe("Pump 1: ms over time");
        expect(spokenName({ title: "Pump 1", format: fixed(0) })).toBe("Pump 1");
    });

    it("works the ticks out again for a window zoomed into", async () => {
        const chart = new LineChart(series, { title: "Score", format: fixed(0), zoom: true });

        await chart.zoomTo({ from: local(10, 20), to: local(10, 50) });

        const times = [...chart.element.querySelectorAll(".af-chart-tick")].map(t => t.textContent);
        expect(times).toEqual(["10:20", "10:25", "10:30", "10:35", "10:40", "10:45", "10:50"]);
    });
});
