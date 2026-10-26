/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { fetchQuestionLedger, questionMessage, type LedgerQuestion } from "../io/openQuestions";

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

describe("fetchQuestionLedger", () => {
    const origFetch = globalThis.fetch;
    afterEach(() => {
        globalThis.fetch = origFetch;
    });

    function serving(response: unknown): ReturnType<typeof vi.fn> {
        const fake = vi.fn(async () => response);
        globalThis.fetch = fake as unknown as typeof fetch;
        return fake;
    }

    it("reads both lists from the ledger, as messages", async () => {
        const fake = serving({
            ok: true,
            json: async () => ({ open: [asked], answered: [{ ...asked, id: "q-0", answer: "Not sure" }] }),
        });

        const ledger = await fetchQuestionLedger("/base");

        expect(fake).toHaveBeenCalledWith("/base/api/questions");
        expect(ledger.open.map(m => [m.id, m.agentId])).toEqual([["q-1", "agent-1"]]);
        expect(ledger.answered.map(m => [m.id, m.answer])).toEqual([["q-0", "Not sure"]]);
    });

    it("is two empty lists when the server refuses, says nothing, or cannot be reached", async () => {
        const nothing = { open: [], answered: [] };
        serving({ ok: false });
        expect(await fetchQuestionLedger("")).toEqual(nothing);

        serving({ ok: true, json: async () => ({}) });
        expect(await fetchQuestionLedger("")).toEqual(nothing);

        globalThis.fetch = vi.fn(async () => {
            throw new TypeError("offline");
        }) as unknown as typeof fetch;
        expect(await fetchQuestionLedger("")).toEqual(nothing);
    });
});
