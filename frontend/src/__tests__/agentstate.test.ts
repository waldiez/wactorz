/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { canDirectMessage, stateColor, stateLabel, sortAgents, relTime } from "../ui/dashboard/agentState";
import type { AgentState } from "../types/agent";

describe("canDirectMessage", () => {
    it("always allows the pinned/system-chat agents even if protected", () => {
        expect(canDirectMessage({ name: "main", protected: true })).toBe(true);
        expect(canDirectMessage({ name: "catalog", protected: true })).toBe(true);
    });
    it("never allows the non-chat system agents", () => {
        expect(canDirectMessage({ name: "io-agent" })).toBe(false);
        expect(canDirectMessage({ name: "monitor-agent" })).toBe(false);
    });
    it("allows normal agents unless they are protected", () => {
        expect(canDirectMessage({ name: "worker" })).toBe(true);
        expect(canDirectMessage({ name: "worker", protected: true })).toBe(false);
    });
});

describe("stateColor / stateLabel", () => {
    it("treats an object state as failed (red)", () => {
        const failed = {} as unknown as AgentState;
        expect(stateColor(failed)).toBe("#f87171");
        expect(stateLabel(failed)).toBe("failed");
    });
    it("maps known string states to colours", () => {
        expect(stateColor("running" as AgentState)).toBe("#34d399");
        expect(stateColor("paused" as AgentState)).toBe("#fbbf24");
        expect(stateColor("initializing" as AgentState)).toBe("#60a5fa");
        expect(stateColor("stopped" as AgentState)).toBe("#4b5563");
        expect(stateColor("???" as AgentState)).toBe("#34d399"); // default
    });
    it("returns the state string as its own label", () => {
        expect(stateLabel("running" as AgentState)).toBe("running");
    });
});

describe("sortAgents", () => {
    it("pins main-actor first, then sorts the rest alphabetically", () => {
        const out = sortAgents([{ name: "zeta" }, { name: "main-actor" }, { name: "alpha" }]);
        expect(out.map(a => a.name)).toEqual(["main-actor", "alpha", "zeta"]);
    });
});

describe("relTime", () => {
    afterEach(() => vi.useRealTimers());
    it("formats recent / seconds / minutes", () => {
        vi.useFakeTimers();
        vi.setSystemTime(100_000);
        expect(relTime(100_000)).toBe("now"); // 0s
        expect(relTime(100_000 - 12_000)).toBe("12s ago");
        expect(relTime(100_000 - 180_000)).toBe("3m ago");
        expect(relTime(100_000 - 7_200_000)).toBe("2h ago"); // ≥1h → hours
        expect(relTime(100_000 - 259_200_000)).toBe("3d ago"); // ≥24h → days
    });
});
