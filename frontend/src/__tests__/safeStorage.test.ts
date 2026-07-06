/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { safeStorage } from "../safeStorage";

describe("safeStorage", () => {
    beforeEach(() => localStorage.clear());
    afterEach(() => vi.restoreAllMocks());

    it("round-trips a value through set/get", () => {
        safeStorage.set("k", "v");
        expect(safeStorage.get("k")).toBe("v");
    });

    it("returns null for a missing key", () => {
        expect(safeStorage.get("absent")).toBeNull();
    });

    it("removes a key", () => {
        safeStorage.set("k", "v");
        safeStorage.remove("k");
        expect(safeStorage.get("k")).toBeNull();
    });

    it("get returns null instead of throwing when storage is unavailable", () => {
        vi.spyOn(localStorage, "getItem").mockImplementation(() => {
            throw new DOMException("denied", "SecurityError");
        });
        expect(() => safeStorage.get("k")).not.toThrow();
        expect(safeStorage.get("k")).toBeNull();
    });

    it("set is a silent no-op when storage throws (e.g. Safari private mode / quota)", () => {
        vi.spyOn(localStorage, "setItem").mockImplementation(() => {
            throw new DOMException("quota", "QuotaExceededError");
        });
        expect(() => safeStorage.set("k", "v")).not.toThrow();
    });

    it("remove is a silent no-op when storage throws", () => {
        vi.spyOn(localStorage, "removeItem").mockImplementation(() => {
            throw new DOMException("denied", "SecurityError");
        });
        expect(() => safeStorage.remove("k")).not.toThrow();
    });
});
