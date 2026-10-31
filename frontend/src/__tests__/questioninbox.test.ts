/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The Inbox's model: books heard replace what is held, the ledger replaces every
 * book, and a read that crosses a newer book does not put the older one back.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { ANSWERED_SHOWN, QuestionInbox } from "../questions/QuestionInbox";
import type { LedgerQuestion } from "../types/agent";

function asked(id: string, over: Partial<LedgerQuestion> = {}): LedgerQuestion {
    return { id, agent_id: "a1", agent: "asker", text: `${id}?`, options: ["Yes", "No"], ...over };
}

function inboxAnswering(result: { ok: true; value: null } | { ok: false; error: string }) {
    const answer = vi.fn(async () => result);
    return { inbox: new QuestionInbox({ answer }), answer };
}

describe("QuestionInbox", () => {
    beforeEach(() => {
        vi.useFakeTimers();
        vi.setSystemTime(1_000_000);
    });
    afterEach(() => {
        vi.useRealTimers();
    });

    it("sorts what is waiting oldest first, and what is answered newest first", () => {
        const { inbox } = inboxAnswering({ ok: true, value: null });
        inbox.heard({
            agentId: "a1",
            questions: [
                asked("newer", { asked_at: 20 }),
                asked("older", { asked_at: 10 }),
                asked("first", { answer: "Yes", answered_at: 5 }),
                asked("last", { answer: "No", answered_at: 9 }),
            ],
        });
        expect(inbox.state.open.map(q => q.id)).toEqual(["older", "newer"]);
        expect(inbox.state.answered.map(q => q.id)).toEqual(["last", "first"]);
    });

    it("shows as many answered as the ledger keeps", () => {
        const { inbox } = inboxAnswering({ ok: true, value: null });
        const many = Array.from({ length: ANSWERED_SHOWN + 5 }, (_, i) =>
            asked(`q${i}`, { answer: "Yes", answered_at: i }),
        );
        inbox.heard({ agentId: "a1", questions: many });
        expect(inbox.state.answered).toHaveLength(ANSWERED_SHOWN);
    });

    it("replaces an agent's book with the one heard, and leaves the others'", () => {
        const { inbox } = inboxAnswering({ ok: true, value: null });
        inbox.heard({ agentId: "a1", questions: [asked("one")] });
        inbox.heard({ agentId: "a2", questions: [asked("two", { agent_id: "a2" })] });

        inbox.heard({ agentId: "a1", questions: [] });

        expect(inbox.state.open.map(q => q.id)).toEqual(["two"]);
    });

    it("takes the ledger in place of every book", () => {
        const { inbox } = inboxAnswering({ ok: true, value: null });
        inbox.heard({ agentId: "gone", questions: [asked("stale", { agent_id: "gone" })] });
        vi.advanceTimersByTime(10);

        inbox.listed({ questions: [asked("fresh")], askedAt: Date.now() });

        expect(inbox.state.open.map(q => q.id)).toEqual(["fresh"]);
        expect(inbox.state.read).toBe(true);
    });

    it("keeps a book heard after the read of the ledger began", () => {
        const { inbox } = inboxAnswering({ ok: true, value: null });
        const askedAt = Date.now();
        vi.advanceTimersByTime(10);
        inbox.heard({ agentId: "a1", questions: [asked("q", { answer: "Yes", answered_at: 1 })] });

        inbox.listed({ questions: [asked("q")], askedAt });

        expect(inbox.state.open).toEqual([]);
        expect(inbox.state.answered.map(q => q.id)).toEqual(["q"]);
    });

    it("keeps an answer sent after the read of the ledger began", async () => {
        const { inbox } = inboxAnswering({ ok: true, value: null });
        const question = asked("q");
        inbox.heard({ agentId: "a1", questions: [question] });
        vi.advanceTimersByTime(10);
        const askedAt = Date.now();
        vi.advanceTimersByTime(10);
        await inbox.answer(question, "Yes", "");

        inbox.listed({ questions: [question], askedAt });

        expect(inbox.state.open).toEqual([]);
        expect(inbox.state.answered.map(q => q.id)).toEqual(["q"]);
    });

    it("says a read failed, keeping what it holds", () => {
        const { inbox } = inboxAnswering({ ok: true, value: null });
        inbox.heard({ agentId: "a1", questions: [asked("q")] });

        inbox.notListed();

        expect(inbox.state.failed).toBe(true);
        expect(inbox.state.open).toHaveLength(1);
        inbox.listed({ questions: [], askedAt: Date.now() + 1 });
        expect(inbox.state.failed).toBe(false);
    });

    it("moves a question to answered once the pick is sent, with its note", async () => {
        const { inbox, answer } = inboxAnswering({ ok: true, value: null });
        const question = asked("q");
        inbox.heard({ agentId: "a1", questions: [question] });

        expect(await inbox.answer(question, "Yes", "checked")).toBe("");

        expect(answer).toHaveBeenCalledWith(question, "Yes", "checked");
        expect(inbox.state.open).toEqual([]);
        expect(inbox.state.answered[0]).toMatchObject({
            answer: "Yes",
            answer_channel: "dashboard",
            answer_note: "checked",
        });
    });

    it("leaves a question waiting when the pick is refused, and says why", async () => {
        const { inbox } = inboxAnswering({ ok: false, error: "already answered" });
        const question = asked("q");
        inbox.heard({ agentId: "a1", questions: [question] });

        expect(await inbox.answer(question, "Yes", "")).toBe("already answered");

        expect(inbox.state.open).toHaveLength(1);
    });

    it("does not overwrite an answer the agent's book already gave", async () => {
        const { inbox } = inboxAnswering({ ok: true, value: null });
        const question = asked("q");
        inbox.heard({ agentId: "a1", questions: [asked("q", { answer: "No", answered_at: 1 })] });

        await inbox.answer(question, "Yes", "");

        expect(inbox.state.answered[0]?.answer).toBe("No");
    });

    it("stops telling a listener that has gone", () => {
        const { inbox } = inboxAnswering({ ok: true, value: null });
        const told = vi.fn();
        const stop = inbox.subscribe(told);
        inbox.heard({ agentId: "a1", questions: [] });
        stop();
        inbox.heard({ agentId: "a1", questions: [] });
        expect(told).toHaveBeenCalledTimes(1);
    });
});
