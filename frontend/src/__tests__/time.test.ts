/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { toMs, timeLabel } from "../time";

describe("toMs", () => {
    it("passes through millisecond timestamps (>= 1e10)", () => {
        expect(toMs(1_700_000_000_000)).toBe(1_700_000_000_000);
    });

    it("converts second timestamps (< 1e10) to ms", () => {
        expect(toMs(1_700_000_000)).toBe(1_700_000_000_000);
    });

    it("returns Date.now() for zero", () => {
        const before = Date.now();
        const result = toMs(0);
        expect(result).toBeGreaterThanOrEqual(before);
    });

    it("returns Date.now() for negative values", () => {
        const before = Date.now();
        const result = toMs(-100);
        expect(result).toBeGreaterThanOrEqual(before);
    });

    it("returns Date.now() for non-finite values", () => {
        const before = Date.now();
        expect(toMs(NaN)).toBeGreaterThanOrEqual(before);
        expect(toMs(Infinity)).toBeGreaterThanOrEqual(before);
    });

    it("returns Date.now() for undefined/null", () => {
        const before = Date.now();
        expect(toMs(undefined)).toBeGreaterThanOrEqual(before);
        expect(toMs(null)).toBeGreaterThanOrEqual(before);
    });

    it("handles numeric strings", () => {
        expect(toMs("1700000000000")).toBe(1_700_000_000_000);
    });
});

describe("timeLabel", () => {
    // The thread loads the last 500 messages by count, not by age, so a bare
    // clock time made last week indistinguishable from this morning. Local time
    // throughout, which is correct because the wire carries epoch values.
    const at = (y: number, m: number, d: number, h = 11, min = 54) => new Date(y, m, d, h, min).getTime();
    const now = at(2026, 7, 24, 15, 0); // 24 Aug 2026, local

    it("shows only the clock for today", () => {
        const out = timeLabel(at(2026, 7, 24), { now });
        expect(out).toMatch(/^\d{1,2}:\d{2}/);
        expect(out).not.toMatch(/Aug|Yesterday/);
    });

    it("names yesterday rather than dating it", () => {
        expect(timeLabel(at(2026, 7, 23), { now })).toMatch(/^Yesterday /);
    });

    it("dates anything older, without the year when it is this one", () => {
        const out = timeLabel(at(2026, 7, 21), { now });
        expect(out).toContain("Aug");
        expect(out).not.toContain("2026");
    });

    it("adds the year once it is a different one", () => {
        expect(timeLabel(at(2025, 11, 31), { now })).toContain("2025");
    });

    it("reads the calendar day locally, so just-after-midnight is today", () => {
        const justAfterMidnight = at(2026, 7, 24, 0, 5);
        expect(timeLabel(justAfterMidnight, { now })).not.toMatch(/Yesterday|Aug/);
    });
});

describe("timeLabel seconds", () => {
    it("adds seconds only when asked, for the feed's log rows", () => {
        const now = new Date(2026, 7, 24, 15, 0).getTime();
        const at = new Date(2026, 7, 24, 11, 54, 30).getTime();
        expect(timeLabel(at, { now })).not.toMatch(/:\d{2}:\d{2}/);
        expect(timeLabel(at, { now, seconds: true })).toMatch(/:\d{2}:\d{2}/);
    });
});
