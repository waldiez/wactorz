/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The vendored HLC-WID generator: construction limits, minting, resumable
 * state, and the logical-clock merge.
 */
import { describe, expect, it, vi } from "vitest";

import { HLCWidGen } from "../vendor/hlcWid";

const SEC_RE = /^\d{8}T\d{6}\.\d{4}Z-node$/;

describe("HLCWidGen construction", () => {
    it("rejects a counter width outside 1..18", () => {
        expect(() => new HLCWidGen({ node: "node", W: 0 })).toThrow("W must be between 1 and 18");
        expect(() => new HLCWidGen({ node: "node", W: 19 })).toThrow("W must be between 1 and 18");
    });

    it("rejects padding outside 0..64", () => {
        expect(() => new HLCWidGen({ node: "node", Z: -1 })).toThrow("Z must be between 0 and 64");
        expect(() => new HLCWidGen({ node: "node", Z: 65 })).toThrow("Z must be between 0 and 64");
    });

    it("rejects a node name that would break the format", () => {
        // The node sits between delimiters, so a dash or dot in it makes the
        // id ambiguous to parse rather than merely ugly.
        expect(() => new HLCWidGen({ node: "my-node" })).toThrow("node must match");
        expect(() => new HLCWidGen({ node: "" })).toThrow("node must match");
    });

    it("defaults to a 4-wide counter, no padding, second precision", () => {
        expect(new HLCWidGen({ node: "node" }).next()).toMatch(SEC_RE);
    });
});

describe("minting", () => {
    it("appends random padding when Z is set", () => {
        const id = new HLCWidGen({ node: "node", Z: 8 }).next();
        expect(id).toMatch(/^\d{8}T\d{6}\.\d{4}Z-node-[0-9a-f]{8}$/);
    });

    it("uses millisecond precision when asked", () => {
        const id = new HLCWidGen({ node: "node", timeUnit: "ms" }).next();
        expect(id).toMatch(/^\d{8}T\d{9}\.\d{4}Z-node$/);
    });

    it("advances the logical counter within one tick", () => {
        const gen = new HLCWidGen({ node: "node" });
        const [a, b] = [gen.next(), gen.next()];
        expect(a).not.toEqual(b);
        expect(b > a).toBe(true);
    });

    it("resets the counter when the clock moves on", () => {
        const gen = new HLCWidGen({ node: "node" });
        gen.next();
        vi.spyOn(Date, "now").mockReturnValue((Date.now() + 5000) * 1);
        expect(gen.next()).toMatch(/\.0000Z-node$/);
        vi.restoreAllMocks();
    });

    it("rolls the tick forward when the counter is exhausted", () => {
        // W:1 exhausts after ten ids in one second.
        const gen = new HLCWidGen({ node: "node", W: 1 });
        const before = gen.state.pt;
        const ids = gen.nextN(12);
        expect(new Set(ids).size).toBe(ids.length);
        expect(gen.state.pt).toBeGreaterThan(before);
    });

    it("nextN mints exactly n", () => {
        expect(new HLCWidGen({ node: "node" }).nextN(5)).toHaveLength(5);
    });
});

describe("state, for resuming across a restart", () => {
    it("round-trips", () => {
        const gen = new HLCWidGen({ node: "node" });
        gen.restoreState(1_700_000_000, 7);
        expect(gen.state).toEqual({ pt: 1_700_000_000, lc: 7 });
    });

    it("refuses a negative state", () => {
        const gen = new HLCWidGen({ node: "node" });
        expect(() => gen.restoreState(-1, 0)).toThrow("invalid state");
        expect(() => gen.restoreState(0, -1)).toThrow("invalid state");
    });

    it("saturates a corrupted future tick instead of minting a malformed year", () => {
        // A corrupted resume value pins the timestamp rather than minting a
        // >8-digit year that nothing parses.
        const gen = new HLCWidGen({ node: "node" });
        gen.restoreState(Number.MAX_SAFE_INTEGER, 0);
        expect(gen.next()).toMatch(/^99991231T\d{6}\.\d{4}Z-node$/);
    });
});

describe("observe — merging a remote clock", () => {
    it("refuses negative remote values", () => {
        const gen = new HLCWidGen({ node: "node" });
        expect(() => gen.observe(-1, 0)).toThrow("must be non-negative");
        expect(() => gen.observe(0, -1)).toThrow("must be non-negative");
    });

    it("takes the greater counter when both clocks agree on the tick", () => {
        const gen = new HLCWidGen({ node: "node" });
        const tick = gen.state.pt || Math.floor(Date.now() / 1000);
        gen.restoreState(tick + 100, 3);
        gen.observe(tick + 100, 9);
        expect(gen.state).toEqual({ pt: tick + 100, lc: 10 });
    });

    it("just advances when the local tick is ahead", () => {
        const gen = new HLCWidGen({ node: "node" });
        const tick = Math.floor(Date.now() / 1000) + 100;
        gen.restoreState(tick, 2);
        gen.observe(tick - 50, 9);
        expect(gen.state).toEqual({ pt: tick, lc: 3 });
    });

    it("follows the remote counter when the remote tick is ahead", () => {
        const gen = new HLCWidGen({ node: "node" });
        const tick = Math.floor(Date.now() / 1000) + 200;
        gen.observe(tick, 4);
        expect(gen.state).toEqual({ pt: tick, lc: 5 });
    });

    it("restarts the counter when local wall-clock time wins", () => {
        const gen = new HLCWidGen({ node: "node" });
        gen.restoreState(1_000, 5);
        gen.observe(2_000, 5);
        expect(gen.state.lc).toBe(0);
        expect(gen.state.pt).toBeGreaterThan(2_000);
    });
});
