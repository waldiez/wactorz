/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { fetchQuestionLedger, questionMessage, QuestionsApi, type LedgerQuestion } from "../io/openQuestions";

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

        expect(fake).toHaveBeenCalledWith("/base/api/questions", expect.anything());
        expect(ledger.open.map(m => [m.id, m.agentId])).toEqual([["q-1", "agent-1"]]);
        expect(ledger.answered.map(m => [m.id, m.answer])).toEqual([["q-0", "Not sure"]]);
        expect(ledger.listed?.questions.map(q => q.id)).toEqual(["q-1", "q-0"]);
    });

    it("leaves out a record with no id", async () => {
        serving({ ok: true, json: async () => ({ open: [{ text: "no id" }, null, asked] }) });
        const ledger = await fetchQuestionLedger("");
        expect(ledger.open.map(m => m.id)).toEqual(["q-1"]);
    });

    it("is two empty lists when the server refuses, says nothing, or cannot be reached", async () => {
        const nothing = { open: [], answered: [] };
        serving({ ok: false });
        expect(await fetchQuestionLedger("")).toEqual(nothing);

        serving({ ok: true, json: async () => ({}) });
        expect(await fetchQuestionLedger("")).toMatchObject({ ...nothing, listed: { questions: [] } });

        globalThis.fetch = vi.fn(async () => {
            throw new TypeError("offline");
        }) as unknown as typeof fetch;
        expect(await fetchQuestionLedger("")).toEqual(nothing);
    });
});

describe("QuestionsApi", () => {
    const origFetch = globalThis.fetch;
    afterEach(() => {
        globalThis.fetch = origFetch;
    });

    it("posts the pick to the question's address, with the note when there is one", async () => {
        const fake = vi.fn(async () => Response.json({ answer: {} }));
        globalThis.fetch = fake as unknown as typeof fetch;

        const api = new QuestionsApi("/base");
        expect(await api.answer(asked, "Maintenance")).toEqual({ ok: true, value: null });
        await api.answer(asked, "Maintenance", "valve swapped");

        const calls = fake.mock.calls as unknown as [string, RequestInit][];
        expect(calls[0]?.[0]).toBe("/base/api/questions/agent-1/q-1/answer");
        expect(JSON.parse(String(calls[0]?.[1].body))).toEqual({ option: "Maintenance" });
        expect(JSON.parse(String(calls[1]?.[1].body))).toEqual({
            option: "Maintenance",
            note: "valve swapped",
        });
    });

    it("comes back with the server's words for a refusal", async () => {
        globalThis.fetch = vi.fn(async () =>
            Response.json({ error: "was already answered" }, { status: 409 }),
        ) as unknown as typeof fetch;

        expect(await new QuestionsApi("").answer(asked, "Maintenance")).toEqual({
            ok: false,
            error: "was already answered",
        });
    });
});
