/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";
import { emit, listen } from "../events";

describe("event bus", () => {
    it("delivers a typed detail from emit to listen", () => {
        const seen: { status: string }[] = [];
        const fn = listen("af-connection-status", d => seen.push(d));
        emit("af-connection-status", { status: "live" });
        expect(seen).toEqual([{ status: "live" }]);
        document.removeEventListener("af-connection-status", fn);
    });

    it("emits detail-less events without a detail argument", () => {
        const handler = vi.fn();
        const fn = listen("af-wipe-all", handler);
        emit("af-wipe-all");
        expect(handler).toHaveBeenCalledTimes(1);
        document.removeEventListener("af-wipe-all", fn);
    });

    it("returns a listener that can be removed to stop delivery", () => {
        const handler = vi.fn();
        const fn = listen("af-clear-feed", handler);
        document.removeEventListener("af-clear-feed", fn);
        emit("af-clear-feed");
        expect(handler).not.toHaveBeenCalled();
    });
});
