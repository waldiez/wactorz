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
import type { ChatMessage } from "../types/agent";
import { normaliseChat } from "./ServerEventRouter";

/** A question as the ledger lists it: the question's record, and who asked it. */
export interface LedgerQuestion {
    /** The question's id, the one its answer names. */
    id: string;
    /** The id of the agent that asked. */
    agent_id: string;
    /** The name of the agent that asked. */
    agent: string;
    text: string;
    options: string[];
    /** Where else it went ("telegram"…), or "dashboard". */
    channel?: string;
    /** When it was asked, in Unix seconds. */
    asked_at?: number;
    /** The option picked, once there is one. */
    answer?: string | null;
}

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

/** The open questions the ledger lists, oldest first; none when the request fails. */
export async function fetchOpenQuestions(apiBase: string): Promise<ChatMessage[]> {
    try {
        const res = await fetch(`${apiBase}/api/questions`);
        if (!res.ok) {
            return [];
        }
        const body = (await res.json()) as { open?: LedgerQuestion[] };
        return (body.open ?? []).map(question => questionMessage(question));
    } catch {
        return [];
    }
}
