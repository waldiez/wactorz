/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Persisted chat-history loading + reconciliation for the dashboard chat.
 *
 * Fetches an agent's history (the chat_log table first, falling back to the
 * actor kv_store) and merges it into the live thread, de-duplicating by id and
 * reconciling optimistic user echoes with their persisted copies.
 */
import type { Attachment, ChatMessage } from "../../types/agent";
import { toMs } from "../../time";
import { stripActionBlocks } from "../../agents/actionBlocks";
import { type LedgerQuestion, questionMessage } from "../../io/openQuestions";

function ingressBase(): string {
    return window.__WACTORZ_INGRESS_PATH ?? "";
}

/**
 * Drop internal bookkeeping turns the backend records into an agent's
 * conversation history for the LLM's benefit but which are not real chat.
 *
 * When an agent is deleted, main_actor records a synthetic pair so the model
 * stops believing the agent still exists:
 *   { role: "user",      content: "[SYSTEM] Agent '…' was deleted …" }
 *   { role: "assistant", content: "Acknowledged — '…' has been removed …" }
 * These live only in the kv_store conversation_history (LLM memory) — surfacing
 * them renders the note as a "you" bubble + an orphan agent ack. Filter the
 * "[SYSTEM]" user turn together with its paired acknowledgement.
 *
 * Operates on chronologically-ordered rows (the ack follows its note).
 */
function stripInternalTurns<T extends { role: string; content: string }>(rows: T[]): T[] {
    const out: T[] = [];
    for (let i = 0; i < rows.length; i++) {
        const r = rows[i];
        if (!r) {
            continue;
        }
        if (r.role === "user" && r.content.startsWith("[SYSTEM]")) {
            // Skip the note and, if present, the synthetic ack recorded right after it.
            if (rows[i + 1]?.role === "assistant" && rows[i + 1]!.content.startsWith("Acknowledged")) {
                i++;
            }
            continue;
        }
        out.push(r);
    }
    return out;
}

/** A stored turn's text as the chat shows it: an agent's reply without the
 *  action blocks it may have been recorded with. What the user typed is theirs. */
function shownContent(row: { role: string; content: string }): string {
    return row.role === "user" ? row.content : stripActionBlocks(row.content);
}

/** What the server records on a turn — the file's identity, but no address. */
type StoredAttachment = { id: string; name: string; mime: string; size: number };

/**
 * Rebuild a turn's attachment chips from what was stored with it.
 *
 * The row carries no `url`: the server keeps the record, not a link, so the
 * address is derived from the id exactly as it was when the file was uploaded.
 * Without it every restored attachment falls back to a plain file chip — an
 * image would come back as a nameplate instead of its thumbnail.
 */
function restoreAttachments(stored: StoredAttachment[] | undefined): Attachment[] | undefined {
    if (!stored?.length) {
        return undefined;
    }
    return stored.map(a => ({
        ...a,
        url: `${ingressBase()}/api/upload/${encodeURIComponent(a.id)}`,
    }));
}

/** Primary source: chat_log table — carries real persisted timestamps.
 *  Returns null on a failed request so callers can retry later. */
async function fromChatLog(agentId: string, agentName: string): Promise<ChatMessage[] | null> {
    const res = await fetch(`${ingressBase()}/api/chats?agent_id=${encodeURIComponent(agentId)}&limit=200`);
    if (!res.ok) {
        return null;
    }
    const rows = (await res.json()) as {
        id: number;
        ts: number;
        role: string;
        content: string;
        attachments?: StoredAttachment[];
        /** The question the row is, when the server's ledger still knows it. */
        question?: Omit<LedgerQuestion, "agent">;
    }[];
    return stripInternalTurns(rows.reverse()).map(r => {
        if (r.question) {
            // By the question's own id: a live copy already shown is then the
            // same message, and an answer names the question by it.
            return questionMessage({ ...r.question, agent: agentName, asked_at: r.ts });
        }
        const attachments = restoreAttachments(r.attachments);
        return {
            id: `hist-${agentId}-${r.id}`,
            from: r.role === "user" ? "user" : agentName,
            to: r.role === "user" ? agentName : "user",
            threadId: agentId,
            content: shownContent(r),
            timestampMs: toMs(r.ts),
            // Spread rather than set: `exactOptionalPropertyTypes` refuses an
            // explicit `undefined` for an optional field.
            ...(attachments ? { attachments } : {}),
        };
    });
}

/** Fallback: actor kv_store history — no timestamps, so synthesise them.
 *  Returns null on a failed request so callers can retry later. */
async function fromKvStore(agentId: string, agentName: string): Promise<ChatMessage[] | null> {
    const res = await fetch(`${ingressBase()}/api/actors/${encodeURIComponent(agentId)}/history`);
    if (!res.ok) {
        return null;
    }
    const rawAll = (await res.json()) as { role: string; content: string }[];
    const raw = stripInternalTurns(rawAll);
    const base = Date.now() - raw.length * 2000 - 5000;
    return raw.map((m, i) => ({
        id: `hist-${agentId}-${i}`,
        from: m.role === "user" ? "user" : agentName,
        to: m.role === "user" ? agentName : "user",
        threadId: agentId,
        content: shownContent(m),
        timestampMs: base + i * 2000,
    }));
}

/** Fetch the persisted chat history of the agent `agentId`, called `agentName` — kept
 *  by id, since two agents may share a name. Returns null on any failure (so the
 *  caller can retry) and [] only when the agent genuinely has no history. */
export async function fetchChatHistory(agentId: string, agentName: string): Promise<ChatMessage[] | null> {
    try {
        const primary = await fromChatLog(agentId, agentName);
        if (primary === null) {
            return null;
        }
        if (primary.length) {
            return primary;
        }
        return await fromKvStore(agentId, agentName);
    } catch {
        return null;
    }
}
