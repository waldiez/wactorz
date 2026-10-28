/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * What a model's pointers mean on the page: its status, what each version is
 * to it, and the moves offered, with the version each one puts in place.
 */
import { describe, it, expect } from "vitest";
import { modelOffers, roleOf, statusOf, summaryLine, versionOffer } from "../models/pointers";
import type { ModelDetail, ModelVersion, PointerMove, PointerState } from "../types/models";
import { moveLine } from "../ui/models/modelDetail";

function version(n: number): ModelVersion {
    return {
        name: "hvac",
        version: n,
        sha256: `sha-${n}`,
        format: "onnx",
        size: 1024,
        runs_code: false,
        added_at: 1_700_000_000 + n,
        metadata: {},
        reference: `hvac:${n}`,
    };
}

function pointer(which: "active" | "shadow", n: number, over: Partial<PointerState> = {}): PointerState {
    return {
        name: "hvac",
        pointer: which,
        version: n,
        previous: null,
        moved_at: 1,
        by: "cli",
        reason: null,
        holds: true,
        ...over,
    };
}

/** hvac with versions 4 to 8: 7 served (after 6), 8 on trial. */
function detail(over: Partial<ModelDetail> = {}): ModelDetail {
    return {
        name: "hvac",
        versions: [4, 5, 6, 7, 8].map(version),
        pointers: { active: pointer("active", 7, { previous: 6 }), shadow: pointer("shadow", 8) },
        ...over,
    };
}

describe("a model's status", () => {
    it("is on trial with a shadow, serving with an active alone, and neither without", () => {
        expect(statusOf(detail().pointers)).toBe("on trial");
        expect(statusOf({ active: pointer("active", 7) })).toBe("serving");
        expect(statusOf({})).toBe("no active");
    });
});

describe("what a version is to its model", () => {
    it("is active, shadow, the previous active, or nothing", () => {
        const { pointers } = detail();

        expect([4, 6, 7, 8].map(v => roleOf(pointers, v))).toEqual([null, "previous", "active", "shadow"]);
    });
});

describe("the moves offered", () => {
    it("name the version each puts in place", () => {
        expect(modelOffers(detail()).map(o => [o.label, o.asksWhy, o.refused])).toEqual([
            ["Promote v8", true, null],
            ["Clear shadow", false, null],
            ["Roll back to v6", true, null],
        ]);
    });

    it("are only those that apply", () => {
        expect(modelOffers(detail({ pointers: { active: pointer("active", 7) } }))).toEqual([]);
    });

    it("refuse a promote whose version no longer holds its bytes, and a rollback to a version removed", () => {
        const broken = detail({
            versions: [7, 8].map(version),
            pointers: {
                active: pointer("active", 7, { previous: 6 }),
                shadow: pointer("shadow", 8, { holds: false }),
            },
        });

        const refusals = modelOffers(broken).map(o => o.refused);

        expect(refusals[0]).toContain("no longer holds");
        expect(refusals[2]).toContain("no longer stored");
    });

    it("on a version: try it as shadow, but not one already active or on trial", () => {
        expect(versionOffer(detail(), 5)).toMatchObject({
            label: "Try v5 as shadow",
            action: { kind: "shadow", version: 5 },
        });
        expect(versionOffer(detail(), 5)?.consequence).toContain("in place of v8");
        expect(versionOffer(detail(), 7)).toBeNull();
        expect(versionOffer(detail(), 8)).toBeNull();
        expect(versionOffer(detail({ pointers: {} }), 5)?.consequence).not.toContain("in place");
    });
});

describe("a model's line in the list", () => {
    it("says its pointers and how many versions it has", () => {
        const summary = { name: "hvac", newest: version(8), versions: 5, pointers: detail().pointers };

        expect(summaryLine(summary)).toBe("active v7 · shadow v8 · 5 versions");
        expect(summaryLine({ ...summary, versions: 1, pointers: {} })).toBe(
            "newest v8 · serves newest · 1 version",
        );
    });
});

describe("a pointer move as a line", () => {
    const move = (over: Partial<PointerMove>): PointerMove => ({
        at: 1,
        pointer: "active",
        change: "moved",
        version: 7,
        from: 6,
        by: "dashboard",
        reason: null,
        ...over,
    });

    it("says what moved where, from what, and who moved it", () => {
        expect(moveLine(move({}))).toBe("active → v7 (was v6), by dashboard");
        expect(moveLine(move({ pointer: "shadow", version: 8, from: null, by: "chat" }))).toBe(
            "shadow → v8, by chat",
        );
        expect(moveLine(move({ pointer: "shadow", change: "promoted", version: null, from: 8 }))).toBe(
            "shadow v8 promoted, by dashboard",
        );
        expect(
            moveLine(move({ pointer: "shadow", change: "cleared", version: null, from: 8, by: "cli" })),
        ).toBe("shadow cleared (was v8), by cli");
    });
});
