/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Feed a `ChatStreamUI` as `IOManager` does: each piece of a reply with the reply
 * so far, and its end with all of it. The UI keeps what it is told rather than
 * putting the reply together itself, so a test that sends pieces says so here.
 */
import type { ChatStreamUI } from "../ui/dashboard/chatStreaming";

const soFar = new WeakMap<ChatStreamUI, Map<string, string>>();

function replies(ui: ChatStreamUI): Map<string, string> {
    let held = soFar.get(ui);
    if (!held) {
        held = new Map();
        soFar.set(ui, held);
    }
    return held;
}

/** `piece` of `from`'s reply arrives, in the thread `agentId` (its own, unless said). */
export function chunk(ui: ChatStreamUI, piece: string, from: string, agentId = from): void {
    const text = (replies(ui).get(from) ?? "") + piece;
    replies(ui).set(from, text);
    ui.onChunk({ chunk: piece, from, agentId, text });
}

/** `from`'s reply ends, with everything it streamed. */
export function end(ui: ChatStreamUI, from: string, agentId = from): void {
    const text = replies(ui).get(from) ?? "";
    replies(ui).delete(from);
    ui.onEnd({ text, from, agentId });
}
