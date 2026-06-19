/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { areaIconText } from "../ui/CardDashboard";

// Regression: HA area icons are MDI names (e.g. "mdi:sofa"), not hex code
// points. The old conversion threw RangeError("Invalid code point NaN") for
// names, and an out-of-range error for all-hex names. areaIconText must never
// throw and must fall back to the house emoji.

describe("areaIconText", () => {
    it("falls back for a missing/empty icon", () => {
        expect(areaIconText(undefined)).toBe("🏠");
        expect(areaIconText(null)).toBe("🏠");
        expect(areaIconText("")).toBe("🏠");
    });

    it("falls back for a non-hex MDI name (the crash case)", () => {
        expect(() => areaIconText("mdi:sofa")).not.toThrow();
        expect(areaIconText("mdi:sofa")).toBe("🏠"); // 'sofa' → NaN
        expect(areaIconText("mdi:home")).toBe("🏠");
    });

    it("falls back for an out-of-range all-hex name instead of throwing", () => {
        // "deadbeef" parses to 0xdeadbeef which exceeds U+10FFFF.
        expect(() => areaIconText("mdi:deadbeef")).not.toThrow();
        expect(areaIconText("mdi:deadbeef")).toBe("🏠");
    });

    it("renders a glyph for a valid code point", () => {
        // U+1F6CB (🛋) is a valid code point expressed in hex.
        expect(areaIconText("mdi:1f6cb")).toBe(String.fromCodePoint(0x1f6cb));
    });
});
