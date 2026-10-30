/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The questions agents have asked that still wait for an answer, from the
 * server's ledger (`GET /api/questions`).
 *
 * A question reaches the page live only while the page is open; one asked
 * before, or while the socket was down, is known to the ledger alone. Read
 * as chat messages, they get the toast and the buttons a live one would have.
 */
import type { ChatMessage, LedgerQuestion } from "../types/agent";
import { posting, requestJson, type Answer } from "./api";
import { normaliseChat, normaliseQuestion } from "./ServerEventRouter";

export type { LedgerQuestion };

/** `question` as the chat message that asked it, the answer included once there is one. */
export function questionMessage(question: LedgerQuestion, from = question.agent): ChatMessage {
    const msg = normaliseChat(
        {
            id: question.id,
            from,
            content: question.text,
            timestamp: question.asked_at,
            options: question.options,
            channel: question.channel,
        },
        question.agent_id,
    );
    return typeof question.answer === "string" ? { ...msg, answer: question.answer } : msg;
}

/** The questions the ledger lists: those still open, oldest first, and the newest answered. */
export interface QuestionLedger {
    open: ChatMessage[];
    answered: ChatMessage[];
    /** The same questions as listed, and when they were asked for; absent when the read failed. */
    listed?: ListedQuestions;
}

/** The ledger's questions, and when the read of them began (`Date.now()`). */
export interface ListedQuestions {
    questions: LedgerQuestion[];
    askedAt: number;
}

/** A question as the wire lists it, its fields coerced; null for a record with no id. */
function listedQuestion(record: unknown): LedgerQuestion | null {
    const entry = (record ?? {}) as { agent_id?: unknown; agent?: unknown };
    const agentId = typeof entry.agent_id === "string" ? entry.agent_id : "";
    return normaliseQuestion(record, agentId, typeof entry.agent === "string" ? entry.agent : "");
}

/** The ledger, as chat messages and as listed; both lists empty when the request
 *  fails, which changes nothing where it is taken in. */
export async function fetchQuestionLedger(apiBase: string): Promise<QuestionLedger> {
    const askedAt = Date.now();
    const got = await requestJson<{ open?: unknown[]; answered?: unknown[] }>(`${apiBase}/api/questions`);
    if (!got.ok) {
        return { open: [], answered: [] };
    }
    const open = (got.value.open ?? []).flatMap(record => listedQuestion(record) ?? []);
    const answered = (got.value.answered ?? []).flatMap(record => listedQuestion(record) ?? []);
    return {
        open: open.map(question => questionMessage(question)),
        answered: answered.map(question => questionMessage(question)),
        listed: { questions: [...open, ...answered], askedAt },
    };
}

/** Sends a person's answers to the server at `apiBase`. */
export class QuestionsApi {
    constructor(private readonly apiBase: string) {}

    /** Pick `option` for `question`, with `note` beside it when there is one. */
    async answer(question: LedgerQuestion, option: string, note = ""): Promise<Answer<null>> {
        const agent = encodeURIComponent(question.agent_id);
        const id = encodeURIComponent(question.id);
        const got = await requestJson<unknown>(
            `${this.apiBase}/api/questions/${agent}/${id}/answer`,
            posting(note ? { option, note } : { option }),
        );
        return got.ok ? { ok: true, value: null } : got;
    }
}
