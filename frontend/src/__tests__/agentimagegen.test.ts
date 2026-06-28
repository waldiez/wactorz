/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { AgentImageGen, agentImageGen } from "../io/AgentImageGen";

function agent(name: string, agentType?: string, id = name) {
    return agentType === undefined ? { id, name } : { id, name, agentType };
}

describe("AgentImageGen avatar resolution", () => {
    const resolve = (name: string, type?: string) => new AgentImageGen().get(agent(name, type));
    // One shared instance for cases where caching is not under test.
    const gen = new AgentImageGen();

    describe("static WebP rules — matched by exact name", () => {
        it.each([
            ["main", "captain.webp"],
            ["main-actor", "captain.webp"],
        ])("%s → %s", (name, file) => {
            expect(resolve(name)).toBe(`./avatars/${file}`);
        });
    });

    describe("static WebP rules — matched by name substring", () => {
        it.each([
            ["monitor-1", "manager.webp"],
            ["anomaly-detector", "manager.webp"],
            ["code-runner", "reasoning.webp"],
            ["dynamic-agent", "reasoning.webp"],
            ["reasoning-unit", "reasoning.webp"],
            ["manual-helper", "assistant.webp"],
            ["assistant-2", "assistant.webp"],
            ["udx-bridge", "assistant.webp"],
            ["nautilus", "remote.webp"],
            ["remote-node", "remote.webp"],
            ["io-gateway", "user.webp"],
            ["docs-index", "rag.webp"],
        ])("%s → %s", (name, file) => {
            expect(resolve(name)).toBe(`./avatars/${file}`);
        });

        it("earlier rules win on overlapping substrings (priority by list order)", () => {
            // "rag" is in both the assistant rule and the docs rule; assistant is
            // listed first, so it wins.
            expect(resolve("rag")).toBe("./avatars/assistant.webp");
            // ⚠ Quirk worth knowing: "home-assistant" is meant for remote.webp, but
            // it contains the substring "assistant", and the assistant rule precedes
            // the remote rule — so it resolves to assistant.webp, not remote.webp.
            // (Characterizing current behaviour; revisit if remote.webp is desired —
            // would need word-boundary matching or reordering the rules.)
            expect(resolve("home-assistant")).toBe("./avatars/assistant.webp");
        });
    });

    describe("static WebP rules — matched by agent type only", () => {
        it.each([
            ["orchestrator", "captain.webp"],
            ["monitor", "manager.webp"],
            ["script", "reasoning.webp"],
            ["gateway", "user.webp"],
            ["io", "user.webp"],
        ])("type %s → %s (name has no keyword)", (type, file) => {
            expect(resolve("zzz-unknown", type)).toBe(`./avatars/${file}`);
        });
    });

    describe("DiceBear fallthrough", () => {
        it("falls back to a deterministic bottts-neutral SVG when nothing matches", () => {
            const url = resolve("zzz-unknown");
            expect(url).toContain("api.dicebear.com/9.x/bottts-neutral/svg");
            expect(url).toContain("seed=zzz-unknown");
        });

        it("url-encodes the seed", () => {
            expect(resolve("a b/c")).toContain("seed=a%20b%2Fc");
        });

        it("matching is case-insensitive for both name and type", () => {
            expect(resolve("MAIN")).toBe("./avatars/captain.webp");
            expect(resolve("zzz", "ORCHESTRATOR")).toBe("./avatars/captain.webp");
        });

        it("a missing agentType still resolves (no type keyword needed)", () => {
            expect(gen.get(agent("assistant"))).toBe("./avatars/assistant.webp");
        });
    });

    describe("caching", () => {
        it("caches by agent id, so a later name change returns the first result", () => {
            const g = new AgentImageGen();
            const first = g.get(agent("main", undefined, "agent-7"));
            // same id, different name — cache short-circuits and returns the first url
            const second = g.get(agent("docs", undefined, "agent-7"));
            expect(second).toBe(first);
            expect(first).toBe("./avatars/captain.webp");
        });
    });

    it("exposes a shared singleton", () => {
        expect(agentImageGen).toBeInstanceOf(AgentImageGen);
        expect(agentImageGen.get(agent("main"))).toBe("./avatars/captain.webp");
    });
});
