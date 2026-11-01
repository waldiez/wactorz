/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Where a send goes, and where a message is filed, with the chat kept by agent
 * id. Ids differ from names here, so nothing passes because the two coincide.
 */
import { describe, it, expect } from "vitest";
import { filedWithLastSent, planSend, readdressed, senderOf } from "../ui/chat/chatRouting";
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
            answerer: "on-pi",
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

describe("planning a send to a name two agents share", () => {
    const TWO = [
        agent("main", "main-id"),
        agent("counter", "counter-on-main"),
        agent("counter", "counter-on-pi", { node: "pi" }),
    ];

    it("by its address goes to that one, answered by it", () => {
        const plan = planSend("@counter@pi count", { id: "main-id", name: "main" }, TWO, "main-id");

        expect(plan).toMatchObject({
            thread: "main-id",
            name: "counter",
            answerer: "counter-on-pi",
            shown: "count",
            routed: "@counter@pi count",
        });
    });

    it("by main's address finds the one on main, however main is spelled", () => {
        for (const spelled of ["@counter@main", "@counter@local", "@Counter@MAIN"]) {
            const plan = planSend(`${spelled} count`, { id: "main-id", name: "main" }, TWO, "main-id");

            expect(plan.answerer).toBe("counter-on-main");
        }
    });

    it("in that agent's own thread drops its address from what is shown and sent", () => {
        const plan = planSend("@counter@pi count", { id: "counter-on-pi", name: "counter" }, TWO, "main-id");

        expect(plan).toMatchObject({ shown: "count", routed: "count", answerer: "counter-on-pi" });
    });

    it("by the bare name goes out as typed, for the server to answer with the addresses", () => {
        const plan = planSend("@counter count", { id: "main-id", name: "main" }, TWO, "main-id");

        expect(plan).toMatchObject({
            thread: "main-id",
            name: "main",
            answerer: "main-id",
            blocked: null,
            shown: "@counter count",
            routed: "@counter count",
        });
    });

    it("waits on main for a command", () => {
        expect(planSend("/agents", { id: "counter-on-pi", name: "counter" }, TWO, "main-id").answerer).toBe(
            "main-id",
        );
    });
});

describe("a message sent again to an address", () => {
    it("has its mention made the address", () => {
        expect(readdressed("@counter five", "counter@pi")).toBe("@counter@pi five");
        expect(readdressed("  @counter@pc five", "counter@pi")).toBe("@counter@pi five");
    });

    it("mentions the address first when it had no mention", () => {
        expect(readdressed("five", "counter@pi")).toBe("@counter@pi five");
        expect(readdressed("@counter", "counter@pi")).toBe("@counter@pi");
    });
});

describe("whom a message is from", () => {
    const msg = (from: string, over: Partial<ChatMessage> = {}): ChatMessage => ({
        id: "m",
        from,
        to: "user",
        threadId: "thread-id",
        content: "x",
        timestampMs: 1,
        ...over,
    });

    it("is the agent's own id, or the thread its reply is in", () => {
        expect(senderOf(msg("counter", { agentId: "counter-on-pi" }))).toBe("counter-on-pi");
        expect(senderOf(msg("counter"))).toBe("thread-id");
    });

    it("is no one for the person's words, the gateway's and the system's", () => {
        expect(senderOf(msg("user"))).toBe("");
        expect(senderOf(msg("io-gateway"))).toBe("");
        expect(senderOf(msg("system"))).toBe("");
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
