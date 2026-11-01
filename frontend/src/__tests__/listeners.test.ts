/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";
import { emit, Listeners } from "../events";

describe("Listeners", () => {
    it("hears what it subscribed to until off, then nothing", () => {
        const listeners = new Listeners();
        const answered = vi.fn();
        const reset = vi.fn();
        listeners.on("af-answered", answered);
        listeners.on("af-reset-chat", reset);

        emit("af-answered", { id: "q1", option: "yes" });
        expect(answered).toHaveBeenCalledWith({ id: "q1", option: "yes" });

        listeners.off();
        emit("af-answered", { id: "q2", option: "no" });
        emit("af-reset-chat", { agent: null });
        expect(answered).toHaveBeenCalledTimes(1);
        expect(reset).not.toHaveBeenCalled();

        // Off again is harmless, and the set can be used afresh.
        listeners.off();
        listeners.on("af-answered", answered);
        emit("af-answered", { id: "q3", option: "yes" });
        expect(answered).toHaveBeenCalledTimes(2);
        listeners.off();
    });
});
