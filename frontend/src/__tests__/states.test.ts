/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { parseAgentState } from "../agents/states";

describe("parseAgentState", () => {
    it("keeps the states the dashboard draws as they are", () => {
        expect(parseAgentState("running")).toBe("running");
        expect(parseAgentState("stopped")).toBe("stopped");
        expect(parseAgentState("initializing")).toBe("initializing");
    });

    it("reads the server's idle, an actor built but not started, as starting", () => {
        expect(parseAgentState("idle")).toBe("initializing");
    });

    it("reads the server's starting, an agent not yet heard from, as starting", () => {
        expect(parseAgentState("starting")).toBe("initializing");
    });

    it("reads the server's failed as failed, in either form", () => {
        expect(parseAgentState("failed")).toEqual({ failed: "" });
        expect(parseAgentState({ failed: "boom" })).toEqual({ failed: "boom" });
    });

    it("reads anything else as running", () => {
        for (const odd of ["unknown", "", undefined, null, 3, {}, { failed: 1 }, ["failed"]]) {
            expect(parseAgentState(odd)).toBe("running");
        }
    });
});
