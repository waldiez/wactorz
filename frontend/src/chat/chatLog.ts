/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The chat's messages, and the one place they are written.
 *
 * Every way a message reaches the chat — sent here, arriving live, restored
 * from history, listed by the server's ledger of questions — goes through
 * here, so the rules hold whichever way it came:
 *
 * - **one copy per id**, and a restored message that is one already on screen
 *   (an echo the page made up an id for) is taken as that message;
 * - **order**: what arrives live is kept in the order it arrived, since the
 *   page and the server stamp time on different clocks; what is restored or
 *   listed later goes where its own time puts it among the rest;
 * - **a cap per thread**, so one busy agent never pushes another's restored
 *   history out while that thread still counts as loaded;
 * - **copy on write**: a message is replaced, never changed where it stands,
 *   and the list is a new one after every change.
 *
 * No DOM: the views read `thread` and `all`, and redraw when a write says it
 * changed something.
 */
import type { ChatMessage } from "../types/agent";
import { IO_GATEWAY } from "../agents/naming";

/** Most messages one thread keeps; the oldest goes first. */
export const MAX_PER_THREAD = 500;

/** How far apart the page's time for a message and the server's may be. */
const SAME_MESSAGE_WINDOW_MS = 120_000;

/** The thread a message is in: the person's own turns, and the gateway's and the
 *  system's, are in the thread of whom they were to; anyone else's in its sender's. */
export function threadOf(msg: ChatMessage): string {
    return msg.from === "user" || msg.from === IO_GATEWAY || msg.from === "system" ? msg.to : msg.from;
}

/** Whether `msg` asks something still open: options offered, none picked. */
export function isOpenQuestion(msg: ChatMessage): boolean {
    return (msg.options?.length ?? 0) > 0 && msg.answer === undefined;
}

/** What a reconcile with the ledger did, for the views to follow: the ids whose
 *  question is closed now, answered or gone, so their toasts can go too. */
export interface Reconciled {
    changed: boolean;
    closed: string[];
}

export class ChatLog {
    private _messages: readonly ChatMessage[] = [];
    /** Threads whose persisted history was taken in, by agent name. */
    private _loaded = new Set<string>();

    /** Every message, oldest first within each thread. */
    get all(): readonly ChatMessage[] {
        return this._messages;
    }

    /** The messages of `name`'s thread, in order. */
    thread(name: string): ChatMessage[] {
        return this._messages.filter(m => threadOf(m) === name);
    }

    /** Whether `name`'s history has been taken in. */
    historyLoaded(name: string): boolean {
        return this._loaded.has(name);
    }

    /** Take a message as it arrives, after the rest; false when its id is held already. */
    add(msg: ChatMessage): boolean {
        if (this._messages.some(m => m.id === msg.id)) {
            return false;
        }
        this._write([...this._messages, msg], threadOf(msg));
        return true;
    }

    /**
     * Take `name`'s persisted history, marking it loaded: each message where
     * its time puts it, except one already shown, whose stored id it adopts
     * instead. Returns how many were added.
     */
    addHistory(name: string, incoming: ChatMessage[]): number {
        this._loaded.add(name);
        let next = [...this._messages];
        let added = 0;
        for (const raw of incoming) {
            if (next.some(m => m.id === raw.id)) {
                continue;
            }
            // The `@agent ` routing prefix is added on send; the restored turn
            // must match what the thread shows, and pair with it.
            const m = raw.from === "user" ? { ...raw, content: raw.content.replace(/^@[\w-]+\s+/, "") } : raw;
            const shown = next.find(x => sameMessage(x, m));
            if (shown) {
                // The persisted id, so the next restored copy of the same words
                // pairs with the next one shown, not with this one again.
                next = next.map(x => (x === shown ? { ...x, id: m.id } : x));
                continue;
            }
            next = insertByTime(next, m);
            added += 1;
        }
        this._write(next, name);
        return added;
    }

    /** Mark the question `id` answered with `option`; the message as it is now, or
     *  null when there is no such question here. */
    answer(id: string, option: string): ChatMessage | null {
        const idx = this._messages.findIndex(m => m.id === id && (m.options?.length ?? 0) > 0);
        const found = idx === -1 ? undefined : this._messages[idx];
        if (!found) {
            return null;
        }
        const message: ChatMessage = { ...found, answer: option };
        const next = [...this._messages];
        next[idx] = message;
        this._messages = next;
        return message;
    }

    /**
     * Bring the questions in line with the server's ledger: add the open ones
     * not held yet, mark those answered meanwhile, and remove an open question
     * the ledger no longer lists.
     *
     * Only for the agents the ledger knows — those with a question open or
     * answered in it. An agent that keeps no book of questions asks only live,
     * and its questions are left as they are.
     */
    reconcile(open: ChatMessage[], answered: ChatMessage[]): Reconciled {
        const before = this._messages;
        const closed: string[] = [];
        let next = [...before];
        for (const msg of open) {
            if (!next.some(m => m.id === msg.id)) {
                next = insertByTime(next, msg);
            }
        }
        const picks = new Map(answered.map(m => [m.id, m.answer]));
        const listed = new Set([...open, ...answered].map(m => m.id));
        const known = new Set([...open, ...answered].map(m => m.agentId ?? ""));
        next = next.flatMap(m => {
            if (!isOpenQuestion(m) || !m.agentId || !known.has(m.agentId)) {
                return [m];
            }
            const pick = picks.get(m.id);
            if (pick !== undefined) {
                closed.push(m.id);
                return [{ ...m, answer: pick }];
            }
            if (!listed.has(m.id)) {
                closed.push(m.id);
                return [];
            }
            return [m];
        });
        this._messages = next;
        this._capAll();
        return { changed: this._messages !== before && !sameList(before, this._messages), closed };
    }

    /** Remove the newest message `matches` picks; whether there was one. */
    removeLast(matches: (m: ChatMessage) => boolean): boolean {
        const at = lastIndexWhere(this._messages, matches);
        if (at === -1) {
            return false;
        }
        this._messages = this._messages.filter((_, i) => i !== at);
        return true;
    }

    /**
     * Drop `agent`'s thread — its own messages and the person's to it — or
     * everything when `agent` is null. Every thread's history is to be fetched
     * again either way: the server's copy may have been reset with it.
     */
    reset(agent: string | null): void {
        this._messages = agent
            ? this._messages.filter(m => m.from !== agent && !(m.from === "user" && m.to === agent))
            : [];
        this._loaded.clear();
    }

    /** Forget that `name`'s history was taken in, so it is fetched again. */
    forgetHistory(name: string): void {
        this._loaded.delete(name);
    }

    /** Drop every message and what was loaded. */
    clear(): void {
        this._messages = [];
        this._loaded.clear();
    }

    /** Make `next` the list, keeping `name`'s thread to its cap. */
    private _write(next: ChatMessage[], name: string): void {
        this._messages = capThread(next, name);
    }

    private _capAll(): void {
        for (const name of new Set(this._messages.map(threadOf))) {
            this._messages = capThread([...this._messages], name);
        }
    }
}

/** Whether `restored` is `shown`, a message the page put on screen under an id of
 *  its own: same sender, same words, close in time, and for the person's own turns
 *  the same thread. A message already restored is never paired again. */
function sameMessage(shown: ChatMessage, restored: ChatMessage): boolean {
    return (
        !shown.id.startsWith("hist-") &&
        shown.from === restored.from &&
        (restored.from !== "user" || shown.to === restored.to) &&
        shown.content.trim() === restored.content.trim() &&
        Math.abs(shown.timestampMs - restored.timestampMs) < SAME_MESSAGE_WINDOW_MS
    );
}

/** `list` with `msg` after the last message stamped no later than it, or first. */
function insertByTime(list: ChatMessage[], msg: ChatMessage): ChatMessage[] {
    const at = lastIndexWhere(list, m => m.timestampMs <= msg.timestampMs) + 1;
    return [...list.slice(0, at), msg, ...list.slice(at)];
}

/** `list` without the oldest of `name`'s messages beyond `MAX_PER_THREAD`. */
function capThread(list: ChatMessage[], name: string): ChatMessage[] {
    let over = list.filter(m => threadOf(m) === name).length - MAX_PER_THREAD;
    if (over <= 0) {
        return list;
    }
    return list.filter(m => {
        if (over > 0 && threadOf(m) === name) {
            over -= 1;
            return false;
        }
        return true;
    });
}

/** The index of the last message `matches` picks, or -1. */
function lastIndexWhere(list: readonly ChatMessage[], matches: (m: ChatMessage) => boolean): number {
    for (let i = list.length - 1; i >= 0; i--) {
        const m = list[i];
        if (m !== undefined && matches(m)) {
            return i;
        }
    }
    return -1;
}

function sameList(a: readonly ChatMessage[], b: readonly ChatMessage[]): boolean {
    return a.length === b.length && a.every((m, i) => m === b[i]);
}
