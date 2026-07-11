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
 * Messages go to the IOAgent via the WebSocket (direct_ws mode) or the fixed
 * `io/chat` MQTT topic (legacy mode). The `@agent-name` prefix is preserved in
 * the content so IOAgent can route it.
 */

import type { AgentInfo, ChatMessage } from "../types/agent";
import type { ServerEventRouter } from "./ServerEventRouter";
import type { WSClient } from "./WSClient";
import { tts } from "./TTSManager";
import { toast } from "../ui/ToastManager";
import { emit } from "../events";
import { uid } from "../ids";

/** Ceiling on a single streamed reply's accumulated text, guarding against a
 *  runaway/looping stream growing this without bound. */
const MAX_STREAM_CHARS = 200_000;

export class IOManager {
    private _lastStreamFrom = "";
    /** Accumulates the current streamed reply so stream-end can emit the full text. */
    private _streamText = "";
    private _ws: WSClient | null = null;

    constructor(private readonly router: ServerEventRouter) {}

    /** Wire in a WSClient so send() can use direct WebSocket when available. */
    setWSClient(ws: WSClient): void {
        this._ws = ws;

        ws.onStreamChunk((chunk, from) => {
            this._lastStreamFrom = from;
            if (this._streamText.length < MAX_STREAM_CHARS) {
                this._streamText += chunk;
            }
            emit("af-stream-chunk", { chunk, from });
        });

        ws.onStreamEnd(() => {
            const text = this._streamText;
            const from = this._lastStreamFrom || "main-actor";
            this._streamText = "";
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

    /** Send `text` to the appropriate agent (direct_ws if available, else io/chat). */
    // Async for the caller-facing contract; the transport calls (WS send / MQTT
    // publish) are fire-and-forget, so there's nothing to await.
    // eslint-disable-next-line @typescript-eslint/require-await
    async send(text: string, agent: AgentInfo | null): Promise<void> {
        let content = text;
        // Prepend @name if a specific agent is selected and no prefix given.
        if (agent && !text.startsWith("@")) {
            content = `@${agent.name} ${text}`;
        }

        const msg: ChatMessage = {
            id: uid(),
            from: "user",
            to: agent?.name ?? "main-actor",
            content, // routed form (`@agent …`), mirroring what goes on the wire
            timestampMs: Date.now(),
        };

        // Echo the routed form so the live feed row matches the persisted one
        // shown after a refresh — the feed renders the `@agent` mention.
        emit("af-feed-push", {
            item: { type: "chat", label: content, agentName: "user", timestamp: msg.timestampMs },
        });

        // direct_ws mode
        const sent = this._ws?.send(content, agent?.name ?? "main-actor");
        if (!sent) {
            toast.show({
                type: "alert-error",
                title: "Disconnected",
                message: "WebSocket disconnected — reconnecting, please retry.",
            });
        }
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
