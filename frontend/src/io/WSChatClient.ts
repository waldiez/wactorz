/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * WSChatClient — lightweight wrapper around the monitor server's /ws endpoint.
 *
 * On connect the server sends:
 *   {"type":"config","chat_mode":"direct_ws"|"mqtt"}
 *
 * When chat_mode is "direct_ws" the browser should send chat messages here
 * instead of publishing to MQTT io/chat.  The server streams replies back as:
 *   {"type":"chat","from":"io-gateway","content":"...","timestamp":...}
 */

export type ChatHandler = (content: string, from: string, timestampMs: number) => void;
export type StreamChunkHandler = (chunk: string, from: string, timestampMs: number) => void;
export type StreamEndHandler = (from: string) => void;
export type ModeHandler = (mode: "direct_ws" | "mqtt") => void;

/** One agent entry as the server includes it in state-patch messages. */
export type StatePatchAgent = {
    agent_id: string;
    name?: string;
    state?: string;
    status?: string;
    protected?: boolean;
    messages_processed?: number;
    cost_usd?: number;
    uptime?: number;
    cpu?: number;
    mem?: number;
    task?: string;
    agent_type?: string;
};

/** Snapshot-level totals computed by the backend (includes historical/deleted agents). */
export type SnapshotStats = {
    totalCostUsd?: number;
    totalMessages?: number;
};

/**
 * Called whenever the server broadcasts a state patch over the WebSocket.
 * `deletedId` is set when the server explicitly deletes an agent.
 * `stats` carries backend-computed totals that include deleted agents.
 */
export type StatePatchHandler = (
    agents: StatePatchAgent[],
    deletedId?: string,
    stats?: SnapshotStats,
) => void;

/** One MQTT-derived event entry from the server's in-memory log_feed. */
export interface LogFeedItem {
    type: string;
    agent_id?: string;
    name?: string;
    agentName?: string;
    message?: string;
    text?: string;
    timestamp?: number;
    status?: Record<string, unknown>;
    severity?: string;
    agentType?: string;
    agent_type?: string;
    command?: string;
}

export type LogFeedHandler = (items: LogFeedItem[]) => void;

/** Shape of the `state` payload carried by reset / delete / patch frames. */
interface StatePatch {
    agents?: StatePatchAgent[];
    total_cost_usd?: number;
    total_messages?: number;
    log_feed?: LogFeedItem[];
}

export class WSChatClient {
    private ws: WebSocket | null = null;
    private _chatMode: "direct_ws" | "mqtt" = "mqtt";
    private _onChat: ChatHandler | null = null;
    private _onStreamChunk: StreamChunkHandler | null = null;
    private _onStreamEnd: StreamEndHandler | null = null;
    private _onMode: ModeHandler | null = null;
    private _onStatePatch: StatePatchHandler | null = null;
    private _onLogFeed: LogFeedHandler | null = null;
    private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    private _reconnectDelay = 1_000;
    private _url = "";
    private _closed = false;
    /** The agent the last chat was addressed to — used to attribute replies,
     *  which the server stamps with the generic transport id "io-gateway". */
    private _lastAgentName = "main-actor";

    get chatMode(): "direct_ws" | "mqtt" {
        return this._chatMode;
    }

    get connected(): boolean {
        return this.ws?.readyState === WebSocket.OPEN;
    }

    /** Complete (non-streaming) message — slash command replies, errors, etc. */
    onChat(fn: ChatHandler): void {
        this._onChat = fn;
    }

    /** One streaming chunk from the LLM. */
    onStreamChunk(fn: StreamChunkHandler): void {
        this._onStreamChunk = fn;
    }

    /** Stream finished — render final markdown, clear typing indicator. */
    onStreamEnd(fn: StreamEndHandler): void {
        this._onStreamEnd = fn;
    }

    /** Server announced which chat mode is active. */
    onMode(fn: ModeHandler): void {
        this._onMode = fn;
    }

    /** Server broadcast a state patch (agent list updated, or agent deleted). */
    onStatePatch(fn: StatePatchHandler): void {
        this._onStatePatch = fn;
    }

    /** Server broadcast new MQTT-derived log_feed entries inside a state patch. */
    onLogFeed(fn: LogFeedHandler): void {
        this._onLogFeed = fn;
    }

    connect(url: string): void {
        this._url = url;
        this._closed = false;
        this._reconnectDelay = 1_000;
        this._open();
    }

    disconnect(): void {
        this._closed = true;
        if (this._reconnectTimer !== null) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = null;
        }
        this.ws?.close();
        this.ws = null;
    }

    /**
     * Send a chat message over the WebSocket.
     * Returns false when the socket is not open (caller can fall back to MQTT).
     */
    send(content: string, agentName = "main-actor"): boolean {
        if (!this.connected) {
            return false;
        }
        this._lastAgentName = agentName;
        this.ws!.send(JSON.stringify({ type: "chat", content, agent_name: agentName }));
        return true;
    }

    /**
     * Send any raw JSON object over the WebSocket (e.g. agent commands).
     * Returns false when the socket is not open.
     */
    sendRaw(msg: object): boolean {
        if (!this.connected) {
            return false;
        }
        this.ws!.send(JSON.stringify(msg));
        return true;
    }

    private _open(): void {
        try {
            this.ws = new WebSocket(this._url);
        } catch (err) {
            console.warn("[WSChat] Cannot open WebSocket:", err);
            this._scheduleReconnect();
            return;
        }

        this.ws.addEventListener("open", () => {
            console.info("[WSChat] connected →", this._url);
            this._reconnectDelay = 1_000;
        });

        this.ws.addEventListener("message", (ev: MessageEvent) => {
            let data: Record<string, unknown>;
            try {
                data = JSON.parse(ev.data as string) as Record<string, unknown>;
            } catch {
                return;
            }
            this._handleFrame(data);
        });

        this.ws.addEventListener("close", () => {
            if (!this._closed) {
                this._scheduleReconnect();
            }
        });

        this.ws.addEventListener("error", () => {
            // "close" follows "error" — reconnect happens there
        });
    }

    /** Route a parsed server frame by its `type` / `state` fields. */
    private _handleFrame(data: Record<string, unknown>): void {
        const type = data["type"];
        if (type === "config") {
            this._handleConfig(data);
            return;
        }
        if (type === "reset") {
            this._handleReset(data);
            return;
        }
        if (type === "delete_agent") {
            // Server explicitly deleted an agent — remove it and apply rest of patch
            this._applyStatePatch(data["state"] as StatePatch | undefined, String(data["agent_id"] ?? ""));
            return;
        }
        // Any message with a "state" field is a state patch broadcast; it may
        // ALSO carry chat/stream content, so fall through after applying it.
        if (data["state"]) {
            this._applyStatePatch(data["state"] as StatePatch);
        }
        this._dispatchContent(data);
    }

    private _handleConfig(data: Record<string, unknown>): void {
        const mode = (data["chat_mode"] as string) === "direct_ws" ? "direct_ws" : "mqtt";
        this._chatMode = mode;
        console.info("[WSChat] chat_mode =", mode);
        this._onMode?.(mode);
    }

    /** State reset broadcast — apply the state patch then clear UI as needed. */
    private _handleReset(data: Record<string, unknown>): void {
        const scope = String(data["scope"] ?? "");
        if (scope === "all") {
            document.dispatchEvent(new CustomEvent("af-wipe-all"));
            return;
        }
        this._applyStatePatch(data["state"] as StatePatch | undefined);
        if (scope === "chat") {
            document.dispatchEvent(
                new CustomEvent("af-reset-chat", {
                    detail: { agent: data["agent"] ?? null },
                }),
            );
        }
        // metrics and logs both clear the server-side activity feed; the
        // on-screen feed is append-only, so tell it to drop its entries.
        if (scope === "metrics" || scope === "logs") {
            document.dispatchEvent(new CustomEvent("af-clear-feed"));
        }
    }

    /** Apply a state patch to the UI; `removedId` removes a single agent. */
    private _applyStatePatch(patch: StatePatch | undefined, removedId?: string): void {
        const stats: SnapshotStats = {};
        if (patch?.total_cost_usd !== undefined) {
            stats.totalCostUsd = patch.total_cost_usd;
        }
        if (patch?.total_messages !== undefined) {
            stats.totalMessages = patch.total_messages;
        }
        this._onStatePatch?.(patch?.agents ?? [], removedId, stats);
        if (patch?.log_feed?.length) {
            this._onLogFeed?.(patch.log_feed);
        }
    }

    /** Deliver chat / stream content carried by a frame. */
    private _dispatchContent(data: Record<string, unknown>): void {
        // The server stamps every direct_ws reply with the transport id
        // "io-gateway", even though it routed to (and got the answer from) the
        // agent the user addressed. Re-attribute it to that agent so the thread,
        // feed and toasts are consistent live and after a reload. The proper fix
        // is server-side — the reply frame should carry the real agent name.
        const rawFrom = String(data["from"] ?? "io-gateway");
        const from = rawFrom === "io-gateway" ? this._lastAgentName : rawFrom;
        const rawTs = data["timestamp"] as number | undefined;
        const ts = rawTs ? (rawTs < 1e10 ? rawTs * 1000 : rawTs) : Date.now();

        if (data["type"] === "chat") {
            this._onChat?.(String(data["content"] ?? ""), from, ts);
        } else if (data["type"] === "stream_chunk") {
            this._onStreamChunk?.(String(data["content"] ?? ""), from, ts);
        } else if (data["type"] === "stream_end") {
            this._onStreamEnd?.(from);
        }
    }

    private _scheduleReconnect(): void {
        if (this._closed || this._reconnectTimer !== null) {
            return;
        }
        const delay = this._reconnectDelay;
        this._reconnectDelay = Math.min(this._reconnectDelay * 2, 30_000);
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null;
            this._open();
        }, delay);
    }
}
