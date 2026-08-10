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
import type { Attachment, ChatMessage } from "./types/agent";
import type { FeedItem } from "./types/feed";
import type { ConnState } from "./ui/dashboard/types";
import type { TTSVoice } from "./ext/tts/types";

/** Detail payload for each application event, keyed by event name. `void` = no detail. */
export interface AppEventMap {
    "af-feed-push": { item: FeedItem };
    "af-chat-message": { msg: ChatMessage };
    "af-connection-status": { status: ConnState };
    "af-attachment-added": { attachment: Attachment };
    "af-agent-command": { command: string; agentId: string };
    "af-send-message": { content: string; target: string; attachments: string[] };
    "af-send-failed": { content: string; target: string };
    "af-stream-chunk": { chunk: string; from: string };
    "af-stream-end": { text: string | null; from: string } | null;
    "af-reset-chat": { agent: string | null };
    "af-wipe-all": void;
    /** A reset finished and its survivors have been applied — the agent list
     *  is settled, so a stale chat target can now be judged. */
    "af-agents-settled": void;
    "af-clear-feed": void;
    "tts-voices-loaded": { voices: TTSVoice[] };
    "tts-audio-start": void;
    "tts-audio-end": void;
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
 * typed detail. Returns the underlying listener so callers can later pass it to
 * `document.removeEventListener(type, listener)`.
 */
export function listen<K extends keyof AppEventMap>(
    type: K,
    handler: (detail: AppEventMap[K]) => void,
): EventListener {
    const fn: EventListener = e => {
        handler((e as CustomEvent<AppEventMap[K]>).detail);
    };
    document.addEventListener(type, fn);
    return fn;
}
