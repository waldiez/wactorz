/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { fetchOpenQuestions, questionMessage, type LedgerQuestion } from "../io/openQuestions";

const asked: LedgerQuestion = {
    id: "q-1",
    agent_id: "agent-1",
    agent: "triage",
    text: "Maintenance, or a real problem?",
    options: ["Maintenance", "Real problem", "Not sure"],
    channel: "dashboard",
    asked_at: 1_700_000_000,
    answer: null,
};

describe("questionMessage", () => {
    it("is the message that asked it: its id, its words, its options, who asked", () => {
        expect(questionMessage(asked)).toMatchObject({
            id: "q-1",
            from: "triage",
            to: "user",
            content: "Maintenance, or a real problem?",
            timestampMs: 1_700_000_000_000,
            agentId: "agent-1",
            options: ["Maintenance", "Real problem", "Not sure"],
        });
        expect(questionMessage(asked).answer).toBeUndefined();
        // The dashboard is where it shows; only another channel is named.
        expect(questionMessage(asked).channel).toBeUndefined();
    });

    it("carries the pick once there is one, and the channel it also went to", () => {
        const msg = questionMessage({ ...asked, answer: "Maintenance", channel: "telegram" });
        expect(msg.answer).toBe("Maintenance");
        expect(msg.channel).toBe("telegram");
    });
});

describe("fetchOpenQuestions", () => {
    const origFetch = globalThis.fetch;
    afterEach(() => {
        globalThis.fetch = origFetch;
    });

    function serving(response: unknown): ReturnType<typeof vi.fn> {
        const fake = vi.fn(async () => response);
        globalThis.fetch = fake as unknown as typeof fetch;
        return fake;
    }

    it("reads the open questions from the ledger, as messages", async () => {
        const fake = serving({ ok: true, json: async () => ({ open: [asked], answered: [] }) });

        const open = await fetchOpenQuestions("/base");

        expect(fake).toHaveBeenCalledWith("/base/api/questions");
        expect(open.map(m => [m.id, m.agentId])).toEqual([["q-1", "agent-1"]]);
    });

    it("is none when the server refuses, says nothing, or cannot be reached", async () => {
        serving({ ok: false });
        expect(await fetchOpenQuestions("")).toEqual([]);

        serving({ ok: true, json: async () => ({}) });
        expect(await fetchOpenQuestions("")).toEqual([]);

        globalThis.fetch = vi.fn(async () => {
            throw new TypeError("offline");
        }) as unknown as typeof fetch;
        expect(await fetchOpenQuestions("")).toEqual([]);
    });
});
