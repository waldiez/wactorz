/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";

import { AgentStreams } from "../io/agentStreams";
import { MAX_STREAM_CHARS } from "../io/streamCap";

describe("AgentStreams", () => {
    it("accumulates chunks per agent without merging concurrent streams", () => {
        const s = new AgentStreams();
        s.append("alpha", "Hel");
        s.append("beta", "XYZ");
        s.append("alpha", "lo");
        expect(s.text("alpha")).toBe("Hello");
        expect(s.text("beta")).toBe("XYZ");
    });

    it("reports the first-started stream as active (drives the single visible row)", () => {
        const s = new AgentStreams();
        expect(s.activeFrom).toBeNull();
        s.append("beta", "x");
        s.append("alpha", "y");
        expect(s.activeFrom).toBe("beta");
        s.take("beta");
        expect(s.activeFrom).toBe("alpha");
    });

    it("take() returns that agent's buffer and clears only it", () => {
        const s = new AgentStreams();
        s.append("alpha", "A");
        s.append("beta", "B");
        expect(s.take("alpha")).toBe("A");
        expect(s.has("alpha")).toBe(false);
        expect(s.text("beta")).toBe("B");
    });

    it("caps each buffer at MAX_STREAM_CHARS (runaway streams can't grow it)", () => {
        const s = new AgentStreams();
        s.append("alpha", "x".repeat(MAX_STREAM_CHARS + 10));
        expect(s.text("alpha").length).toBeLessThanOrEqual(MAX_STREAM_CHARS);
    });
});
