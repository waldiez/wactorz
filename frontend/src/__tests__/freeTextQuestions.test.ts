/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * A question asked for words: the person types the answer in the Inbox, and
 * every other place the question shows leads there.
 */
import { describe, it, expect, vi, afterEach } from "vitest";

import { normaliseChat, normaliseQuestion } from "../io/ServerEventRouter";
import { questionMessage, QuestionsApi } from "../io/openQuestions";
import { QuestionInbox } from "../questions/QuestionInbox";
import type { ChatMessage, LedgerQuestion } from "../types/agent";
import { buildWordsLink } from "../ui/chat/chatThread";
import { QuestionCard, type CardHost } from "../ui/inbox/questionCard";
import { ANSWER_IN_INBOX, questionToast } from "../ui/questionToast";

const asked: LedgerQuestion = {
    id: "q-1",
    agent_id: "agent-1",
    agent: "hvac",
    text: "What did you change on the AHU?",
    options: [],
    channel: "dashboard",
    asked_at: 1_700_000_000,
    free_text: true,
    max_chars: 4000,
};

function message(over: Partial<ChatMessage> = {}): ChatMessage {
    return {
        id: "q-1",
        from: "hvac",
        to: "user",
        threadId: "agent-1",
        content: asked.text,
        timestampMs: 0,
        agentId: "agent-1",
        freeText: true,
        ...over,
    };
}

function host(over: Partial<CardHost> = {}): CardHost {
    return {
        label: () => "hvac",
        face: () => document.createElement("span"),
        answer: vi.fn(async () => ""),
        answerText: vi.fn(async () => ""),
        ...over,
    };
}

describe("the frame and the record", () => {
    it("say a question is answered in words, and nothing else does", () => {
        expect(normaliseChat({ id: "m", content: "x", free_text: true }, "a").freeText).toBe(true);
        expect(normaliseChat({ id: "m", content: "x", free_text: "yes" }, "a").freeText).toBeUndefined();
        const record = normaliseQuestion(
            { id: "q", text: "x", free_text: true, max_chars: 4000 },
            "a",
            "hvac",
        );
        expect(record).toMatchObject({ free_text: true, max_chars: 4000 });
        expect(normaliseQuestion({ id: "q", text: "x" }, "a", "hvac")).not.toHaveProperty("free_text");
    });

    it("a question from the ledger is a message that says so", () => {
        expect(questionMessage(asked).freeText).toBe(true);
    });
});

describe("the notice", () => {
    afterEach(() => {
        location.hash = "";
    });

    it("leads to the Inbox, with no option to press", () => {
        const toast = questionToast(message())!;

        expect(toast.sticky).toBe(true);
        expect(toast.actions!.map(a => a.label)).toEqual([ANSWER_IN_INBOX]);
        toast.actions![0]!.onClick();
        expect(location.hash).toBe("#/inbox");
    });

    it("is nothing once it is answered", () => {
        expect(questionToast(message({ answer: "Swapped the filter." }))).toBeNull();
    });
});

describe("the thread", () => {
    it("links to the Inbox while it waits", () => {
        const link = buildWordsLink(message()).querySelector("a")!;

        expect(link.textContent).toBe(ANSWER_IN_INBOX);
        expect(link.getAttribute("href")).toBe("#/inbox");
    });

    it("shows the words once answered", () => {
        expect(buildWordsLink(message({ answer: "Swapped it." })).textContent).toContain("Swapped it.");
    });
});

describe("the Inbox card", () => {
    function box(card: QuestionCard): HTMLTextAreaElement {
        return card.element.querySelector("textarea")!;
    }

    function send(card: QuestionCard): HTMLButtonElement {
        return [...card.element.querySelectorAll("button")].find(b => b.textContent === "Send")!;
    }

    it("is a box to type in, as long as the question takes, and a button to send", () => {
        const card = new QuestionCard(asked, host());

        expect(box(card).maxLength).toBe(4000);
        expect(card.element.querySelector(".af-inbox-options")).toBeNull();
        expect(send(card)).toBeTruthy();
    });

    it("sends what was typed, trimmed", async () => {
        const answering = host();
        const card = new QuestionCard(asked, answering);
        box(card).value = "  Swapped the filter.  ";

        send(card).click();
        await Promise.resolve();

        expect(answering.answerText).toHaveBeenCalledWith(asked, "Swapped the filter.");
    });

    it("says so when nothing was typed, and sends nothing", async () => {
        const answering = host();
        const card = new QuestionCard(asked, answering);

        send(card).click();
        await Promise.resolve();

        expect(answering.answerText).not.toHaveBeenCalled();
        expect(card.element.querySelector(".af-inbox-refusal")!.textContent).toBe("Type an answer first.");
    });

    it("keeps the words and says why on a refusal", async () => {
        const card = new QuestionCard(asked, host({ answerText: vi.fn(async () => "already answered") }));
        box(card).value = "Swapped it.";

        send(card).click();
        await vi.waitFor(() =>
            expect(card.element.querySelector(".af-inbox-refusal")!.textContent).toBe("already answered"),
        );

        expect(box(card).value).toBe("Swapped it.");
        expect(send(card).disabled).toBe(false);
    });

    it("keeps what is half typed across a redraw", () => {
        const card = new QuestionCard(asked, host());
        box(card).value = "Swapped the";

        card.refresh({ ...asked, context: "Flow rose." });

        expect(box(card).value).toBe("Swapped the");
    });

    it("draws no length limit when the record has none", () => {
        const { max_chars: _omitted, ...unbounded } = asked;

        expect(new QuestionCard(unbounded, host()).element.querySelector("textarea")!.maxLength).toBe(-1);
    });
});

describe("the answer's way to the server", () => {
    const origFetch = globalThis.fetch;
    afterEach(() => {
        globalThis.fetch = origFetch;
    });

    it("posts the words and gives back what the server kept", async () => {
        const fake = vi.fn(async () => Response.json({ answer: { option: "Swapped it." } }));
        globalThis.fetch = fake as unknown as typeof fetch;

        expect(await new QuestionsApi("/base").answerText(asked, "Swapped it.\u0007")).toEqual({
            ok: true,
            value: "Swapped it.",
        });
        const calls = fake.mock.calls as unknown as [string, RequestInit][];
        expect(calls[0]?.[0]).toBe("/base/api/questions/agent-1/q-1/answer");
        expect(JSON.parse(String(calls[0]?.[1].body))).toEqual({ text: "Swapped it.\u0007" });
    });

    it("falls back to the words sent when the server names none, and passes a refusal on", async () => {
        globalThis.fetch = vi.fn(async () => Response.json({})) as unknown as typeof fetch;
        expect(await new QuestionsApi("").answerText(asked, "Swapped.")).toEqual({
            ok: true,
            value: "Swapped.",
        });

        globalThis.fetch = vi.fn(async () =>
            Response.json({ error: "cannot reach it" }, { status: 503 }),
        ) as unknown as typeof fetch;
        expect(await new QuestionsApi("").answerText(asked, "Swapped.")).toMatchObject({
            ok: false,
            error: "cannot reach it",
        });
    });

    it("answers the Inbox's question with the server's words", async () => {
        const answerText = vi.fn(async () => ({ ok: true as const, value: "Swapped it." }));
        const inbox = new QuestionInbox({ answer: vi.fn(), answerText });
        inbox.heard({ agentId: "agent-1", questions: [asked] });

        expect(await inbox.answerText(asked, "  Swapped it. ")).toBe("");
        expect(inbox.state.answered[0]).toMatchObject({ id: "q-1", answer: "Swapped it." });
    });

    it("passes the Inbox a refusal, and keeps the question waiting", async () => {
        const answerText = vi.fn(async () => ({ ok: false as const, error: "no" }));
        const inbox = new QuestionInbox({ answer: vi.fn(), answerText });
        inbox.heard({ agentId: "agent-1", questions: [asked] });

        expect(await inbox.answerText(asked, "x")).toBe("no");
        expect(inbox.state.open.map(q => q.id)).toEqual(["q-1"]);
    });
});
