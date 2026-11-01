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
 *   and a thread is a new array after every change to it.
 *
 * Kept thread by thread, with an index of ids, so a message costs the same
 * however many threads are held: a write copies only the thread it changes, and
 * which agents are asking something is kept per thread as it changes rather
 * than found by reading every message.
 *
 * No DOM: the views read `thread`, `asking` and `all`, and redraw when a write
 * says it changed something.
 */
import type { ChatMessage } from "../types/agent";

/** Most messages one thread keeps; the oldest goes first. */
export const MAX_PER_THREAD = 500;

/** How far apart the page's time for a message and the server's may be. */
const SAME_MESSAGE_WINDOW_MS = 120_000;

const NONE: readonly ChatMessage[] = Object.freeze([]);

/** The thread a message is in: its agent's id, as the server named it. Threads are
 *  kept by id because two agents may share a name. */
export function threadOf(msg: ChatMessage): string {
    return msg.threadId;
}

/** The name of the agent whose thread `msg` is in: whom the person spoke to, or the sender. */
function ownerName(msg: ChatMessage): string {
    return msg.from === "user" ? msg.to : msg.from;
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
    /** Each thread's messages, by its agent's id, in the order the threads began. */
    private _threads = new Map<string, readonly ChatMessage[]>();
    /** Which thread each held id is in. */
    private _where = new Map<string, string>();
    /** The threads in which an agent has a question still open. */
    private _asking = new Set<string>();
    /** Threads whose persisted history was taken in, by agent id. */
    private _loaded = new Set<string>();
    private _all: readonly ChatMessage[] | null = null;

    /** Every message, thread by thread. */
    get all(): readonly ChatMessage[] {
        this._all ??= [...this._threads.values()].flat();
        return this._all;
    }

    /** The messages of the thread `id`, in order: the thread itself, unchanged
     *  until it is next written to. */
    thread(id: string): readonly ChatMessage[] {
        return this._threads.get(id) ?? NONE;
    }

    /** The message `id`, or undefined when none is held. */
    get(id: string): ChatMessage | undefined {
        const thread = this._where.get(id);
        return thread === undefined ? undefined : this.thread(thread).find(m => m.id === id);
    }

    /** The ids of the agents asking the person something still open. */
    asking(): Set<string> {
        return new Set(this._asking);
    }

    /** Whether the history of the thread `id` has been taken in. */
    historyLoaded(id: string): boolean {
        return this._loaded.has(id);
    }

    /** Take a message as it arrives, after the rest; false when its id is held already. */
    add(msg: ChatMessage): boolean {
        if (this._where.has(msg.id)) {
            return false;
        }
        const thread = threadOf(msg);
        this._set(thread, [...this.thread(thread), msg]);
        return true;
    }

    /**
     * Take the persisted history of the thread `id`, marking it loaded: each
     * message where its time puts it, except one already shown, whose stored id
     * it adopts instead. Returns how many were added.
     */
    addHistory(id: string, incoming: ChatMessage[]): number {
        this._loaded.add(id);
        const threads = new Map<string, ChatMessage[]>();
        const seen = new Set<string>();
        let added = 0;
        for (const raw of incoming) {
            if (this._where.has(raw.id) || seen.has(raw.id)) {
                continue;
            }
            seen.add(raw.id);
            // The `@agent ` routing prefix is added on send; the restored turn
            // must match what the thread shows, and pair with it.
            const m = raw.from === "user" ? { ...raw, content: raw.content.replace(/^@[\w-]+\s+/, "") } : raw;
            const into = threadOf(m);
            const list = threads.get(into) ?? [...this.thread(into)];
            const adopted = placeRestored(list, m);
            if (adopted === null) {
                added += 1;
            } else {
                this._where.delete(adopted);
            }
            threads.set(into, list);
        }
        threads.forEach((list, into) => this._set(into, list));
        return added;
    }

    /** Replace the message `id` with what `change` makes of it; the message as it is
     *  now, or null when there is none. */
    update(id: string, change: (m: ChatMessage) => ChatMessage): ChatMessage | null {
        const thread = this._where.get(id);
        const found = this.get(id);
        if (thread === undefined || !found) {
            return null;
        }
        const message = change(found);
        this._set(
            thread,
            this.thread(thread).map(m => (m === found ? message : m)),
        );
        return message;
    }

    /**
     * File the message `id` in the thread `threadId`, the agent called `name`, where
     * its time puts it: the person's words reached an agent other than the thread
     * they were typed in. The message as it is now, or null when none is held.
     */
    move(id: string, threadId: string, name: string): ChatMessage | null {
        const from = this._where.get(id);
        const found = this.get(id);
        if (from === undefined || !found) {
            return null;
        }
        if (from === threadId) {
            return found;
        }
        const message = { ...found, threadId, to: name };
        this._set(
            from,
            this.thread(from).filter(m => m !== found),
        );
        const into = [...this.thread(threadId)];
        into.splice(insertionPoint(into, message), 0, message);
        this._set(threadId, into);
        return message;
    }

    /** Mark the question `id` answered with `option`; the message as it is now, or
     *  null when there is no such question here. */
    answer(id: string, option: string): ChatMessage | null {
        const found = this.get(id);
        if (!found || (found.options?.length ?? 0) === 0) {
            return null;
        }
        return this.update(id, m => ({ ...m, answer: option }));
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
        const closed: string[] = [];
        let changed = false;
        for (const msg of open) {
            if (!this._where.has(msg.id)) {
                const thread = threadOf(msg);
                const list = [...this.thread(thread)];
                list.splice(insertionPoint(list, msg), 0, msg);
                this._set(thread, list);
                changed = true;
            }
        }
        const picks = new Map(answered.map(m => [m.id, m.answer]));
        const listed = new Set([...open, ...answered].map(m => m.id));
        const known = new Set([...open, ...answered].map(m => m.agentId ?? ""));
        for (const [thread, list] of [...this._threads]) {
            let touched = false;
            const next = list.flatMap(m => {
                if (!isOpenQuestion(m) || !m.agentId || !known.has(m.agentId)) {
                    return [m];
                }
                const pick = picks.get(m.id);
                if (pick !== undefined) {
                    touched = true;
                    closed.push(m.id);
                    return [{ ...m, answer: pick }];
                }
                if (!listed.has(m.id)) {
                    touched = true;
                    closed.push(m.id);
                    this._where.delete(m.id);
                    return [];
                }
                return [m];
            });
            if (touched) {
                this._set(thread, next);
                changed = true;
            }
        }
        return { changed, closed };
    }

    /** Remove the newest message of the thread `id` that `matches` picks; whether there was one. */
    removeLast(id: string, matches: (m: ChatMessage) => boolean): boolean {
        const list = this.thread(id);
        const at = lastIndexWhere(list, matches);
        const gone = list[at];
        if (!gone) {
            return false;
        }
        this._where.delete(gone.id);
        this._set(
            id,
            list.filter((_, i) => i !== at),
        );
        return true;
    }

    /**
     * Drop the threads of the agents called `agent` — their own messages and
     * everything said to them — or every thread when `agent` is null. By name,
     * as the server resets them: every agent that went by it. `agentId` limits
     * it to that agent's thread, where another shares the name. Every thread's
     * history is to be fetched again either way: the server's copy may have
     * been reset with it.
     */
    reset(agent: string | null, agentId: string | null = null): void {
        if (agent === null) {
            this._drop();
            return;
        }
        for (const [id, list] of [...this._threads]) {
            if (agentId ? id === agentId : list.some(m => ownerName(m) === agent)) {
                list.forEach(m => this._where.delete(m.id));
                this._threads.delete(id);
                this._asking.delete(id);
            }
        }
        this._all = null;
        this._loaded.clear();
    }

    /** Forget that the history of the thread `id` was taken in, so it is fetched again. */
    forgetHistory(id: string): void {
        this._loaded.delete(id);
    }

    /** Drop every message and what was loaded. */
    clear(): void {
        this._drop();
    }

    private _drop(): void {
        this._threads = new Map();
        this._where = new Map();
        this._asking = new Set();
        this._all = null;
        this._loaded.clear();
    }

    /** Make `list` the thread `id`, kept to its cap, with the index and whether it
     *  asks something brought up to date for that thread alone. */
    private _set(id: string, list: readonly ChatMessage[]): void {
        const kept = list.length > MAX_PER_THREAD ? list.slice(-MAX_PER_THREAD) : list;
        for (let i = 0; i < list.length - kept.length; i++) {
            const dropped = list[i];
            if (dropped) {
                this._where.delete(dropped.id);
            }
        }
        kept.forEach(m => this._where.set(m.id, id));
        this._threads.set(id, kept);
        if (kept.some(m => m.from !== "user" && isOpenQuestion(m))) {
            this._asking.add(id);
        } else {
            this._asking.delete(id);
        }
        this._all = null;
    }
}

/** Whether `restored` is `shown`, a message the page put on screen under an id of
 *  its own: same sender, same words, close in time, and for the person's own turns
 *  the same thread. A message already restored is never paired again. */
function sameMessage(shown: ChatMessage, restored: ChatMessage): boolean {
    return (
        !shown.id.startsWith("hist-") &&
        shown.from === restored.from &&
        shown.threadId === restored.threadId &&
        shown.content.trim() === restored.content.trim() &&
        Math.abs(shown.timestampMs - restored.timestampMs) < SAME_MESSAGE_WINDOW_MS
    );
}

/**
 * Put the restored `m` into `list`, changing it: onto the message it already is
 * on screen, which takes its stored id so the next restored copy of the same
 * words pairs with the next one shown, or where its time puts it. The id the
 * message on screen had, or null when `m` was inserted.
 */
function placeRestored(list: ChatMessage[], m: ChatMessage): string | null {
    const shown = list.findIndex(x => sameMessage(x, m));
    const was = list[shown];
    if (was) {
        list[shown] = { ...was, id: m.id };
        return was.id;
    }
    list.splice(insertionPoint(list, m), 0, m);
    return null;
}

/** Where `msg` goes in `list`: after the last message stamped no later than it, or first. */
function insertionPoint(list: readonly ChatMessage[], msg: ChatMessage): number {
    return lastIndexWhere(list, m => m.timestampMs <= msg.timestampMs) + 1;
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
