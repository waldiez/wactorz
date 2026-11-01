/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The test ids the dashboard ships and the ones the end-to-end harness looks
 * for are the same list.
 *
 * The harness runs against a built dashboard and takes minutes to say an
 * element is missing; this says it in the unit tests, the moment either side
 * renames one.
 */
import { describe, it, expect } from "vitest";

// `?raw` rather than node:fs — the suite has no @types/node, and Vite resolves
// a raw import from anywhere in the repository.
import harness from "../../../e2e/harness/browser.py?raw";
import { TEST_IDS } from "../ui/testIds";

/** Every module of the dashboard, as source, keyed by path. */
const SOURCES = import.meta.glob<string>(["../**/*.ts", "!../__tests__/**", "!../ui/testIds.ts"], {
    query: "?raw",
    import: "default",
    eager: true,
});

/** The test ids the harness names: `get_by_test_id("…")` and `[data-testid='…']`, in Python or in page scripts. */
function idsInHarness(source: string): Set<string> {
    const ids = new Set<string>();
    for (const m of source.matchAll(/get_by_test_id\(\s*(?:f?["'])([^"'{]+)["']/g)) {
        ids.add(m[1]!);
    }
    for (const m of source.matchAll(/data-testid=\\?["']?([\w-]+)/g)) {
        ids.add(m[1]!);
    }
    return ids;
}

describe("test ids", () => {
    it("the harness uses some, so this check reads it", () => {
        expect(idsInHarness(harness).size).toBeGreaterThan(0);
    });

    it("every one the harness looks for is shipped", () => {
        const shipped = new Set<string>(TEST_IDS);
        const missing = [...idsInHarness(harness)].filter(id => !shipped.has(id) && !id.startsWith("view-"));

        expect(missing).toEqual([]);
    });

    it("every one listed is one the harness looks for, so the list holds nothing dead", () => {
        const used = idsInHarness(harness);

        expect(TEST_IDS.filter(id => !used.has(id))).toEqual([]);
    });

    it("every one listed is put on an element somewhere", () => {
        const code = Object.values(SOURCES).join("\n");
        const unused = TEST_IDS.filter(id => !code.includes(`"${id}"`));

        expect(unused).toEqual([]);
    });

    it("are listed once each", () => {
        expect(new Set(TEST_IDS).size).toBe(TEST_IDS.length);
    });
});
