/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Typed application event bus over `document` CustomEvents.
 *
 * The dashboard's components are decoupled and talk via DOM CustomEvents on
 * `document` (the `af-*` / `tts-*` names below). `AppEventMap` records each
 * event's `detail` shape so `emit` is checked at the dispatch site and `listen`
 * hands callers an already-typed detail. The one place a `CustomEvent` cast is
 * sanctioned is inside `listen`.
 */
import type { QuestionLedger } from "./io/openQuestions";
import type { Attachment, ChatMessage, QuestionBook } from "./types/agent";
import type { FeedItem } from "./types/feed";
import type { ConnState } from "./ui/dashboard/types";
import type { TTSVoice } from "./ext/tts/types";
import type { Route } from "./router";

/** Detail payload for each application event, keyed by event name. `void` = no detail. */
export interface AppEventMap {
    "af-feed-push": { item: FeedItem };
    "af-chat-message": { msg: ChatMessage };
    "af-connection-status": { status: ConnState };
    /** `/api/config` was read, or could not be: what it seeds can be read now. */
    "af-config-loaded": void;
    "af-attachment-added": { attachment: Attachment };
    "af-agent-command": { command: string; agentId: string };
    /** The chat puts a message on the wire: `id` is its bubble's, and comes back on
     *  `af-send-failed` should the send not go out. */
    "af-send-message": {
        id: string;
        content: string;
        target: string;
        name: string;
        answerer: string;
        attachments: string[];
    };
    /** The send `id` did not go out: the socket was closed. */
    "af-send-failed": { id: string; content: string; target: string };
    /** The person now talks to `agent`, by typing or by voice: a reply the server
     *  stamps with the gateway's id is that agent's. */
    "af-addressed": { agent: string };
    /** A piece of `from`'s streamed reply, and the reply so far (`text`); `agentId` is
     *  the thread it is in. */
    "af-stream-chunk": { chunk: string; from: string; agentId: string; text: string };
    /** `from`'s streamed reply ended; `text` is all of it, empty when nothing came. */
    "af-stream-end": { text: string; from: string; agentId: string };
    /** The server says which agent the person's message `id` reached: `agentId`, called
     *  `name`. A mention can reach an agent other than the thread it was typed in. */
    "af-turn-reached": { id: string; agentId: string; name: string };
    /** The socket to the server opened or closed: whether a message can go out now.
     *  Not `af-connection-status`, which needs the server's broker link too. */
    "af-socket": { open: boolean };
    /** A turn started or ended, or what waits changed: who it waits on (null when no
     *  turn is under way), and how many of the person's messages wait to go out. */
    "af-turn": { agent: string | null; queued: number };
    "af-reset-chat": { agent: string | null; agentId?: string | null };
    /** The person pressed one of a question's buttons: send the pick to the agent that asked. */
    "af-answer-option": { id: string; agentId: string; option: string; index: number };
    /** The person picked one of the addresses a reply (`id`) offered for a name several agents share. */
    "af-pick-address": { id: string; address: string };
    /** A question was answered, here or on another channel: show the pick, take the buttons off. */
    "af-answered": { id: string; option: string };
    /** The server's ledger of questions, each time the page goes live: the open ones, one
     *  asked while the page was away among them, and the newest answered. Not
     *  `af-chat-message`, which says a message arrived now and ends a turn on it. */
    "af-questions-listed": QuestionLedger;
    /** An agent's question book changed, on the broker: what the Inbox shows follows it. */
    "af-question-book": QuestionBook;
    /** Everything was wiped; `survivors` are the ids of the agents still running. */
    "af-wipe-all": { survivors: string[] };
    /** The agent list is settled and can be trusted: a reset's survivors have
     *  been applied, or a named agent was deleted. Either way "the chat target
     *  is gone" is now a fact rather than a race with agents re-registering.
     *  The reason only picks the wording the user is shown. */
    "af-agents-settled": { reason: "reset" | "deleted" };
    "af-clear-feed": void;
    /** Application-log records the server pushed as it wrote them. Untyped
     *  entries: they arrive off the socket and are validated where they are
     *  turned into rows, not here. */
    "af-app-log": { entries: unknown[] };
    /** The address changed (Back, a link, a typed hash): open what it names. */
    "af-route": Route;
    /** A pointer of the stored model `name` moved, wherever it was moved from. */
    "af-model-pointer": { name: string; pointer: string };
    /** The dashboard opened a view, or another conversation in the chat: the address follows.
     *  `replace` when it is the same place under another name, which is no step for Back. */
    "af-view-changed": Route & { replace?: boolean };
    /** An extension adds a view: a nav button showing `icon` and `label`, and `build` for its
     *  content, opened at `#/<key>`. Sent once, during startup. */
    "af-register-view": { key: string; icon: string; label: string; build: () => HTMLElement };
    "tts-voices-loaded": { voices: TTSVoice[] };
    "tts-audio-start": void;
    "tts-audio-end": void;
    /** Someone stopped the turn: stop saying it, in either voice. */
    "tts-stop": void;
}

/** Dispatch an application event; the detail argument is required iff the map entry isn't `void`. */
export function emit<K extends keyof AppEventMap>(
    type: K,
    ...rest: AppEventMap[K] extends void ? [] : [detail: AppEventMap[K]]
): void {
    document.dispatchEvent(new CustomEvent(type, { detail: rest[0] }));
}

/**
 * Subscribe to an application event on `document`. The handler receives the
 * typed detail. Returns the way to stop.
 */
export function listen<K extends keyof AppEventMap>(
    type: K,
    handler: (detail: AppEventMap[K]) => void,
): () => void {
    const fn: EventListener = e => {
        handler((e as CustomEvent<AppEventMap[K]>).detail);
    };
    document.addEventListener(type, fn);
    return () => document.removeEventListener(type, fn);
}

/**
 * The listeners a component holds on the bus, dropped together when it is torn
 * down. `on` subscribes as `listen` does and keeps the listener; `off` removes
 * every one kept, so a component never has to name each again to let it go.
 */
export class Listeners {
    private held: (() => void)[] = [];

    /** Subscribe `handler` to `type`, as `listen` does, until `off`. */
    on<K extends keyof AppEventMap>(type: K, handler: (detail: AppEventMap[K]) => void): void {
        this.held.push(listen(type, handler));
    }

    /** Remove every listener `on` added. */
    off(): void {
        this.held.forEach(stop => stop());
        this.held = [];
    }
}
