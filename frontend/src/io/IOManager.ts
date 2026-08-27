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

import { AgentStreams } from "./agentStreams";
import { MAIN_AGENT } from "../agents/naming";

export class IOManager {
    private _lastStreamFrom = "";
    /** Per-agent stream accumulation (stream-end emits the full text per agent). */
    private _streams = new AgentStreams();
    private _ws: WSClient | null = null;

    constructor(private readonly router: ServerEventRouter) {}

    /** Wire in a WSClient so send() can use direct WebSocket when available. */
    setWSClient(ws: WSClient): void {
        this._ws = ws;

        ws.onStreamChunk((chunk, from) => {
            this._lastStreamFrom = from;
            this._streams.append(from, chunk);
            emit("af-stream-chunk", { chunk, from });
        });

        ws.onStreamEnd(frameFrom => {
            // The frame names its own sender; the last chunk's agent is the
            // fallback for a frame that arrives without one. Either way only
            // that agent's buffer is taken, so concurrent streams never merge.
            const from = frameFrom || this._lastStreamFrom || MAIN_AGENT;
            const text = this._streams.take(from);
            // A stream_end with no streamed text (e.g. agents that reply via a
            // single non-streamed `chat` frame) must NOT create a feed row —
            // that produced a phantom empty bubble attributed to the default name.
            if (text) {
                tts.notify(text);
                emit("af-feed-push", {
                    item: { type: "chat", label: text, agentName: from, timestamp: Date.now() },
                });
            }
            emit("af-stream-end", { text, from });
        });
    }

    /** Send `text` to the appropriate agent over the WebSocket. Nothing reaches
     *  the feed unless the transport accepted it. */
    // Async for the caller-facing contract; the transport calls (WS send / MQTT
    // publish) are fire-and-forget, so there's nothing to await.
    // eslint-disable-next-line @typescript-eslint/require-await
    async send(text: string, agent: AgentInfo | null, attachments: string[] = []): Promise<void> {
        let content = text;
        // Prepend @name if a specific agent is selected and no prefix given.
        if (agent && !text.startsWith("@")) {
            content = `@${agent.name} ${text}`;
        }

        const msg: ChatMessage = {
            id: uid(),
            from: "user",
            to: agent?.name ?? MAIN_AGENT,
            content, // routed form (`@agent …`), mirroring what goes on the wire
            timestampMs: Date.now(),
        };

        const sent = this._ws?.send(content, agent?.name ?? MAIN_AGENT, attachments);
        if (!sent) {
            toast.show({
                type: "alert-error",
                title: "Disconnected",
                message: "WebSocket disconnected — reconnecting, please retry.",
            });
            // The dashboard rendered an optimistic bubble before this attempt —
            // tell it to roll that back (the pre-mention text matches the bubble).
            emit("af-send-failed", { content: text, target: agent?.name ?? MAIN_AGENT });
            return;
        }

        // Echo only what actually went out. Echoing before the attempt left a
        // failed message sitting in the feed looking exactly like a delivered
        // one, and it would vanish on the next refresh since nothing persisted
        // it. The routed form is used so the live row matches the persisted one
        // — the feed renders the `@agent` mention.
        emit("af-feed-push", {
            item: { type: "chat", label: content, agentName: "user", timestamp: msg.timestampMs },
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
