/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";
import { emit, listen, Listeners } from "../events";

describe("event bus", () => {
    it("delivers a typed detail from emit to listen", () => {
        const seen: { status: string }[] = [];
        const fn = listen("af-connection-status", d => seen.push(d));
        emit("af-connection-status", { status: "live" });
        expect(seen).toEqual([{ status: "live" }]);
        fn();
    });

    it("emits detail-less events without a detail argument", () => {
        const handler = vi.fn();
        const fn = listen("af-clear-feed", handler);
        emit("af-clear-feed");
        expect(handler).toHaveBeenCalledTimes(1);
        fn();
    });

    it("returns a listener that can be removed to stop delivery", () => {
        const handler = vi.fn();
        const fn = listen("af-clear-feed", handler);
        fn();
        emit("af-clear-feed");
        expect(handler).not.toHaveBeenCalled();
    });

    it("stops hearing once told to, and telling it twice does nothing", () => {
        const seen: unknown[] = [];
        const stop = listen("af-connection-status", d => seen.push(d));

        stop();
        stop();
        emit("af-connection-status", { status: "live" });

        expect(seen).toEqual([]);
    });

    it("Listeners lets go of all it holds", () => {
        const seen: unknown[] = [];
        const held = new Listeners();
        held.on("af-connection-status", d => seen.push(d));
        held.on("af-clear-feed", () => seen.push("cleared"));

        held.off();
        emit("af-connection-status", { status: "live" });
        emit("af-clear-feed");

        expect(seen).toEqual([]);
    });
});
