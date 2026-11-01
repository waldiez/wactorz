/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * A chat message arriving, as a reply on the socket or as an agent's own: its
 * notice, read aloud, its agent's card, the chat and the feed, the same whichever
 * way it came.
 */
import { chatFeedItem } from "../agents/mapping";
import type { AgentStore } from "../agents/AgentStore";
import { emit } from "../events";
import type { IOManager } from "../io/IOManager";
import type { ChatMessage } from "../types/agent";
import type { QuestionToasts } from "./questionToast";
import { toast } from "./ToastManager";

/** What taking a message in reaches. */
export interface ChatIngressDeps {
    /** Reads it aloud. */
    speaker: Pick<IOManager, "receiveAgentMessage">;
    /** Flashes its agent's card. */
    store: Pick<AgentStore, "onChat">;
    /** Raises it, if it is a question. */
    questions: Pick<QuestionToasts, "arrived">;
}

/** The function every chat message goes through. */
export function chatIngress({ speaker, store, questions }: ChatIngressDeps): (msg: ChatMessage) => void {
    return msg => {
        if (msg.from !== "user" && !questions.arrived(msg)) {
            toast.show({ type: "chat", title: msg.from, message: msg.content.slice(0, 120) });
        }
        speaker.receiveAgentMessage(msg);
        store.onChat(msg.from);
        emit("af-chat-message", { msg });
        emit("af-feed-push", { item: chatFeedItem(msg) });
    };
}
