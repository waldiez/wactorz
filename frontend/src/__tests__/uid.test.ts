/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Client-side id generation: uniqueness, WID format, and ordering.
 */
import { describe, expect, it } from "vitest";

import { uid } from "../ids";

/** `YYYYMMDDTHHMMSS.<lc4>Z-<node>` — the shape a `{node, W: 4}` generator mints. */
const WID_RE = /^\d{8}T\d{6}\.\d{4}Z-browser$/;

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

    it("mints a WID", () => {
        expect(uid()).toMatch(WID_RE);
    });

    it("keeps the WID shape behind a prefix", () => {
        const id = uid("chat");
        expect(id.startsWith("chat-")).toBe(true);
        expect(id.slice("chat-".length)).toMatch(WID_RE);
    });

    it("increases strictly, which is what dedupe orders on", () => {
        // The logical counter is what makes ordering hold inside one second.
        const ids = Array.from({ length: 1000 }, () => uid());
        const sorted = [...ids].sort();
        expect(ids).toEqual(sorted);
    });
});
