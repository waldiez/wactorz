/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Reading an agent's stored chat from the server, in the shape it is stored in.
 * Turning that into a thread is `ui/dashboard/chatHistory.ts`. Each reader answers
 * null when the request failed, so "no history" and "could not ask" stay apart.
 */
import { apiUrl, requestJson } from "./api";
import type { LedgerQuestion } from "./openQuestions";

/** What the server records on a turn: the file's identity, but no address. */
export type StoredAttachment = { id: string; name: string; mime: string; size: number };

/** One row of the chat_log table. */
export interface ChatLogRow {
    /** The row's id. */
    id: number;
    /** When it was written, in Unix seconds. */
    ts: number;
    /** Who spoke: `user`, or the agent. */
    role: string;
    /** What was said. */
    content: string;
    /** The files sent with it. */
    attachments?: StoredAttachment[];
    /** The question the row is, when the server's ledger still knows it. */
    question?: Omit<LedgerQuestion, "agent">;
}

/** One turn of an actor's kv_store history, which keeps no timestamps. */
export interface ActorTurn {
    /** Who spoke: `user`, or the agent. */
    role: string;
    /** What was said. */
    content: string;
}

async function readList<T>(path: string): Promise<T[] | null> {
    const got = await requestJson<T[]>(apiUrl(path));
    return got.ok && Array.isArray(got.value) ? got.value : null;
}

/** The newest rows of the agent `agentId`'s chat_log, newest first. */
export function readChatLog(agentId: string): Promise<ChatLogRow[] | null> {
    return readList<ChatLogRow>(`/api/chats?agent_id=${encodeURIComponent(agentId)}&limit=200`);
}

/** The agent `agentId`'s kv_store conversation history, oldest first. */
export function readActorHistory(agentId: string): Promise<ActorTurn[] | null> {
    return readList<ActorTurn>(`/api/actors/${encodeURIComponent(agentId)}/history`);
}
