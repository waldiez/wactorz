/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * IO Manager: routes user input to the appropriate agent and surfaces replies
 * to the dashboard via DOM CustomEvents (`af-chat-message` / `af-stream-chunk` /
 * `af-stream-end`) and the activity feed. The dashboard chat (DashboardChat)
 * renders from those events — IOManager owns no UI.
 *
 * Messages go to the server over the WebSocket, which routes them into the actor
 * system directly. The `@agent-name` prefix is preserved in the content so the
 * server can resolve the recipient from it.
 */

import type { AgentInfo, ChatMessage } from "../types/agent";
import type { ServerEventRouter } from "./ServerEventRouter";
import type { WSClient } from "./WSClient";
import { tts } from "../ext/tts";
import { toast } from "../ui/ToastManager";
import { emit } from "../events";
import { uid } from "../ids";

import { AgentStreams, streamKey } from "./agentStreams";
import { MAIN_AGENT } from "../agents/naming";

/**
 * `text` as it goes to the thread's agent, called `label`: mentioned first,
 * unless it mentions someone already or is a slash command. A command is
 * answered by the orchestrator before any agent sees it, and addressed to the
 * thread's agent it would arrive as that agent's task text instead. A shared
 * name is mentioned by its address, or the server could not tell which agent
 * the message is for.
 */
export function mentioning(text: string, label: string): string {
    return text.startsWith("@") || text.startsWith("/") ? text : `@${label} ${text}`;
}

export class IOManager {
    /** Per-agent stream accumulation (stream-end emits the full text per agent). */
    private _streams = new AgentStreams();
    private _ws: WSClient | null = null;

    constructor(private readonly router: ServerEventRouter) {}

    /** Wire in a WSClient so send() can use direct WebSocket when available. */
    setWSClient(ws: WSClient): void {
        this._ws = ws;

        ws.on("streamChunk", (chunk, from, _ts, agentId) => {
            const key = streamKey(agentId, from);
            this._streams.append(key, chunk);
            // The reply so far travels with the piece, so this is the one place a
            // stream is put together.
            emit("af-stream-chunk", { chunk, from, agentId, text: this._streams.text(key) });
        });

        ws.on("streamEnd", (from, agentId) => {
            // The socket names a sender for every frame, the addressed agent for
            // one without, so only that agent's buffer is taken and concurrent
            // streams never merge.
            const text = this._streams.take(streamKey(agentId, from));
            // A stream_end with no streamed text (e.g. agents that reply via a
            // single non-streamed `chat` frame) must NOT create a feed row —
            // that produced a phantom empty bubble attributed to the default name.
            if (text) {
                tts.notify(text);
                emit("af-feed-push", {
                    item: { type: "chat", label: text, agentName: from, timestamp: Date.now() },
                });
            }
            emit("af-stream-end", { text, from, agentId });
        });
    }

    /** Send `text` to the appropriate agent over the WebSocket, mentioning it as
     *  `label` (its address where its name is shared). Nothing reaches the feed
     *  unless the transport accepted it. */
    // Async for the caller-facing contract; the transport calls (WS send / MQTT
    // publish) are fire-and-forget, so there's nothing to await.
    // eslint-disable-next-line @typescript-eslint/require-await
    async send(
        text: string,
        agent: AgentInfo | null,
        attachments: string[] = [],
        id = uid(),
        label = "",
    ): Promise<void> {
        const content = agent ? mentioning(text, label || agent.name) : text;
        const timestampMs = Date.now();
        const sent = this._ws?.send(content, agent?.name ?? MAIN_AGENT, attachments, agent?.id ?? "", id);
        if (!sent) {
            toast.show({
                type: "alert-error",
                title: "Disconnected",
                message: "Reconnecting — the message goes once the connection is back.",
            });
            // The dashboard rendered the bubble before this attempt, under `id`:
            // it keeps it waiting to go once the connection is back.
            emit("af-send-failed", { id, content: text, target: agent?.id ?? "" });
            return;
        }

        // Echo only what actually went out. Echoing before the attempt left a
        // failed message sitting in the feed looking exactly like a delivered
        // one, and it would vanish on the next refresh since nothing persisted
        // it. The routed form is used so the live row matches the persisted one
        // — the feed renders the `@agent` mention.
        emit("af-feed-push", {
            item: { type: "chat", label: content, agentName: "user", timestamp: timestampMs },
        });
    }

    /**
     * Handle an incoming agent→user chat message: read it aloud (TTS).
     *
     * One caller per transport — `wsChat.onChat` (direct_ws) and `mqtt.on("chat")`
     * (mqtt) — and the backend delivers chat over exactly one of them per mode
     * (WS frames are gated on a live registry; MQTT chat only otherwise), so there
     * is no cross-transport double to guard against here. Streamed replies notify
     * separately via `onStreamEnd`; this covers the non-streamed ones.
     */
    receiveAgentMessage(msg: ChatMessage): void {
        // Ignore agent↔agent background chatter — only user-directed replies.
        if (msg.to && msg.to !== "user") {
            return;
        }
        tts.notify(msg.content, msg.from);
    }
}
