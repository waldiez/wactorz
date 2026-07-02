/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { uid } from "../ids";

describe("uid", () => {
    it("returns a non-empty string", () => {
        expect(uid().length).toBeGreaterThan(0);
    });

    it("namespaces with a prefix", () => {
        expect(uid("user").startsWith("user-")).toBe(true);
    });

    it("never collides, even in a same-millisecond burst", () => {
        const ids = Array.from({ length: 1000 }, () => uid("user"));
        expect(new Set(ids).size).toBe(ids.length);
    });
});
