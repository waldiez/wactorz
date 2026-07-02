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
import type { ChatMessage } from "../../types/agent";
import { toMs } from "../../mqtt/MQTTClient";

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

/** Primary source: chat_log table — carries real persisted timestamps. */
async function fromChatLog(agentId: string): Promise<ChatMessage[]> {
    const res = await fetch(`${ingressBase()}/api/chats?agent=${encodeURIComponent(agentId)}&limit=200`);
    if (!res.ok) {
        return [];
    }
    const rows = (await res.json()) as { id: number; ts: number; role: string; content: string }[];
    return stripInternalTurns(rows.reverse()).map(r => ({
        id: `hist-${agentId}-${r.id}`,
        from: r.role === "user" ? "user" : agentId,
        to: r.role === "user" ? agentId : "user",
        content: r.content,
        timestampMs: toMs(r.ts),
    }));
}

/** Fallback: actor kv_store history — no timestamps, so synthesise them. */
async function fromKvStore(agentId: string): Promise<ChatMessage[]> {
    const res = await fetch(`${ingressBase()}/api/actors/${encodeURIComponent(agentId)}/history`);
    if (!res.ok) {
        return [];
    }
    const rawAll = (await res.json()) as { role: string; content: string }[];
    const raw = stripInternalTurns(rawAll);
    const base = Date.now() - raw.length * 2000 - 5000;
    return raw.map((m, i) => ({
        id: `hist-${agentId}-${i}`,
        from: m.role === "user" ? "user" : agentId,
        to: m.role === "user" ? agentId : "user",
        content: m.content,
        timestampMs: base + i * 2000,
    }));
}

/** Fetch an agent's persisted chat history (best-effort; [] on any failure). */
export async function fetchChatHistory(agentId: string): Promise<ChatMessage[]> {
    try {
        const primary = await fromChatLog(agentId);
        return primary.length ? primary : await fromKvStore(agentId);
    } catch {
        return [];
    }
}

/** The pending optimistic echo of a persisted user message, if any. */
function optimisticEcho(existing: ChatMessage[], m: ChatMessage): ChatMessage | undefined {
    // The optimistic message has id "user-<ts>" and the persisted one "hist-…",
    // so plain id de-dup misses the pair and the user's message renders twice.
    // Match on target + content + a tight timestamp window.
    return existing.find(
        x =>
            x.id.startsWith("user-") &&
            x.from === "user" &&
            x.to === m.to &&
            x.content === m.content &&
            Math.abs(x.timestampMs - m.timestampMs) < 120_000,
    );
}

/**
 * Reconcile `incoming` history against `existing`: returns the messages to
 * prepend, adopting persisted ids onto matching optimistic echoes in place.
 */
export function mergeChatHistory(existing: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
    const ids = new Set(existing.map(m => m.id));
    const toAdd: ChatMessage[] = [];
    for (const raw of incoming) {
        if (ids.has(raw.id)) {
            continue;
        }
        // Strip the `@agent ` routing prefix IOManager adds on send so restored
        // history matches the live thread AND the echo comparison can pair up.
        // Normalise onto a copy — never mutate the caller's incoming messages.
        const m: ChatMessage =
            raw.from === "user" ? { ...raw, content: raw.content.replace(/^@[\w-]+\s+/, "") } : raw;
        const echo = m.from === "user" ? optimisticEcho(existing, m) : undefined;
        if (echo) {
            // Adopt the persisted id onto the live thread's optimistic echo in
            // place — the caller relies on this to reconcile its rendered message.
            echo.id = m.id;
            continue;
        }
        toAdd.push(m);
    }
    return toAdd;
}
