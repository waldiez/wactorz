/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { toMs } from "../time";

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
