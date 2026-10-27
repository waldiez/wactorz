/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Where a send goes, and where a message is filed, with the chat kept by agent
 * id. Ids differ from names here, so nothing passes because the two coincide.
 */
import { describe, it, expect } from "vitest";
import { filedWithLastSent, planSend } from "../ui/dashboard/chatRouting";
import type { AgentInfo, ChatMessage } from "../types/agent";

function agent(name: string, id: string, over: Partial<AgentInfo> = {}): AgentInfo {
    return { id, name, state: "running", protected: false, ...over };
}

const AGENTS = [agent("main", "main-id"), agent("catalog", "catalog-id"), agent("counter", "on-pi")];

describe("planning a send", () => {
    it("goes from the open thread, to its agent, without the agent's own mention", () => {
        const plan = planSend("@counter count", { id: "on-pi", name: "counter" }, AGENTS, "main-id");

        expect(plan).toEqual({
            thread: "on-pi",
            name: "counter",
            blocked: null,
            shown: "count",
            routed: "count",
        });
    });

    it("to another agent by mention stays in this thread, the mention on what goes out", () => {
        // The server routes the mention and says which agent it reached.
        const plan = planSend("@catalog list", { id: "on-pi", name: "counter" }, AGENTS, "main-id");

        expect(plan).toMatchObject({
            thread: "on-pi",
            name: "catalog",
            shown: "list",
            routed: "@catalog list",
        });
    });

    it("with no conversation open goes from main's", () => {
        expect(planSend("hi", { id: "", name: "" }, AGENTS, "main-id")).toMatchObject({
            thread: "main-id",
            name: "main",
            blocked: null,
        });
    });

    it("to an agent an address named that has not arrived is refused, naming it", () => {
        const plan = planSend("hi", { id: "", name: "ghost" }, AGENTS, "main-id");

        expect(plan.blocked).toContain("@ghost");
    });

    it("by mention while that agent has not arrived goes from main's thread", () => {
        const plan = planSend("@catalog hi", { id: "", name: "ghost" }, AGENTS, "main-id");

        expect(plan).toMatchObject({ thread: "main-id", name: "catalog", blocked: null });
    });

    it("to a stopped agent is refused", () => {
        const stopped = [agent("counter", "on-pi", { state: "stopped" })];

        expect(planSend("hi", { id: "on-pi", name: "counter" }, stopped, "").blocked).toContain("start it");
    });
});

describe("filing a message that names no agent of its own", () => {
    const said = (from: string): ChatMessage => ({
        id: "m",
        from,
        to: "user",
        threadId: "io-gateway-id",
        content: "x",
        timestampMs: 1,
    });

    it("puts the gateway's and the system's words with whom the person last spoke to", () => {
        const last = { name: "counter", id: "on-pi" };

        expect(filedWithLastSent(said("io-gateway"), last, "main-id")).toMatchObject({
            to: "counter",
            threadId: "on-pi",
        });
        expect(filedWithLastSent(said("system"), last, "main-id").threadId).toBe("on-pi");
    });

    it("puts them with main before anyone was spoken to", () => {
        expect(filedWithLastSent(said("io-gateway"), { name: "main", id: "" }, "main-id").threadId).toBe(
            "main-id",
        );
    });

    it("leaves an agent's own as the server filed it", () => {
        const own = said("counter");

        expect(filedWithLastSent(own, { name: "main", id: "main-id" }, "main-id")).toBe(own);
    });
});
