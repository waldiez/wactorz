/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { createDeletionGuard } from "../agents/deletionGuard";

// Injectable clock so the time-based logic is deterministic without fake timers.
function guardAt(start = 1_000) {
    let t = start;
    const g = createDeletionGuard(3_000, 30_000, () => t);
    return { g, advance: (ms: number) => (t += ms), set: (ms: number) => (t = ms) };
}

describe("createDeletionGuard", () => {
    it("reports a never-seen id as not deleted", () => {
        const { g } = guardAt();
        expect(g.isDeleted("x")).toBe(false);
    });

    it("suppresses a just-deleted id (the stop-window)", () => {
        const { g } = guardAt();
        g.markDeleted("x");
        expect(g.isDeleted("x")).toBe(true);
    });

    it("blocks a stale event whose timestamp predates the deletion", () => {
        const { g } = guardAt(10_000);
        g.markDeleted("x"); // deletedAt = 10_000
        expect(g.isDeleted("x", 9_500)).toBe(true); // older → stale
    });

    it("keeps blocking an in-flight event inside the grace window", () => {
        const { g } = guardAt(10_000);
        g.markDeleted("x");
        expect(g.isDeleted("x", 12_000)).toBe(true); // within +3s grace → still stale
    });

    it("re-admits a re-spawn whose event is newer than deletion + grace", () => {
        const { g } = guardAt(10_000);
        g.markDeleted("x");
        expect(g.isDeleted("x", 13_500)).toBe(false); // > 10_000 + 3_000 → respawn
        expect(g.isDeleted("x")).toBe(false); // entry cleared, stays admitted
    });

    it("failsafe-expires for timestamp-less paths so it never sticks forever", () => {
        const { g, advance } = guardAt();
        g.markDeleted("x");
        expect(g.isDeleted("x")).toBe(true);
        advance(31_000); // past the 30s failsafe
        expect(g.isDeleted("x")).toBe(false);
        expect(g.isDeleted("x")).toBe(false); // cleared
    });

    it("re-marking refreshes the deletion timestamp", () => {
        const { g, advance } = guardAt(10_000);
        g.markDeleted("x");
        advance(5_000); // now 15_000
        g.markDeleted("x"); // deletedAt refreshed to 15_000
        expect(g.isDeleted("x", 16_000)).toBe(true); // within new grace
        expect(g.isDeleted("x", 19_000)).toBe(false); // newer than 15_000 + 3_000
    });
});
