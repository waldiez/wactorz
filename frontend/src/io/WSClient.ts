/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * WSClient — the browser's single connection to the monitor server's /ws endpoint.
 *
 * It owns the connection (and the "live" signal) and dispatches every server frame
 * by `type`: chat/stream, state patches (store + log_feed), and `server_event`
 * frames — `{topic, payload}` live activity that it hands to the ServerEventRouter.
 * On connect the server sends `{"type":"config","chat_mode":"direct_ws"|"mqtt"}`;
 * chat is sent back over this socket (never over a broker).
 */
import { log } from "./logger";
import { toMs } from "../time";
import { emit } from "../events";
import type { StatePatchAgent, SnapshotStats, LogFeedItem } from "../types/ws";

export type ChatHandler = (
    content: string,
    from: string,
    timestampMs: number,
    to: string,
    source: string,
    surface: string,
    surfaceLabel: string,
    brain: string,
) => void;
export type StreamChunkHandler = (chunk: string, from: string, timestampMs: number) => void;
export type StreamEndHandler = (from: string) => void;
/** A live `server_event` frame carrying a topic-addressed payload. */
export type ServerEventHandler = (topic: string, payload: unknown) => void;
/** The /ws connection opened or dropped. */
export type ConnectionHandler = () => void;
/** Server↔broker connection state — combined with the /ws state to drive "live". */
export type MqttStatusHandler = (connected: boolean) => void;

/** Coerce an untrusted JSON field to a string; non-strings fall back (so a
 *  hostile object can't stringify to "[object Object]"). */
const asStr = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);

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

export type LogFeedHandler = (items: LogFeedItem[]) => void;

/** Shape of the `state` payload carried by reset / delete / patch frames. */
interface StatePatch {
    agents?: StatePatchAgent[];
    total_cost_usd?: number;
    total_messages?: number;
    log_feed?: LogFeedItem[];
}

export class WSClient {
    private ws: WebSocket | null = null;
    private _onChat: ChatHandler | null = null;
    private _onStreamChunk: StreamChunkHandler | null = null;
    private _onStreamEnd: StreamEndHandler | null = null;
    private _onStatePatch: StatePatchHandler | null = null;
    private _onLogFeed: LogFeedHandler | null = null;
    private _onServerEvent: ServerEventHandler | null = null;
    private _onConnected: ConnectionHandler | null = null;
    private _onDisconnected: ConnectionHandler | null = null;
    private _onMqttStatus: MqttStatusHandler | null = null;
    private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    /** Debounces "dropped" so a brief close/reopen doesn't flip the UI to demo. */
    private _disconnectTimer: ReturnType<typeof setTimeout> | null = null;
    private _reconnectDelay = 1_000;
    private _url = "";
    private _closed = false;
    /** The agent the last chat was addressed to — used to attribute replies,
     *  which the server stamps with the generic transport id "io-gateway". */
    private _lastAgentName = "main-actor";

    /** True while the WebSocket is open. */
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

    /** Stream finished — render the final markdown for the accumulated reply. */
    onStreamEnd(fn: StreamEndHandler): void {
        this._onStreamEnd = fn;
    }

    /** Server broadcast a state patch (agent list updated, or agent deleted). */
    onStatePatch(fn: StatePatchHandler): void {
        this._onStatePatch = fn;
    }

    /** Server broadcast new log_feed entries inside a state patch. */
    onLogFeed(fn: LogFeedHandler): void {
        this._onLogFeed = fn;
    }

    /** A live `server_event` frame — hand `(topic, payload)` to the ServerEventRouter. */
    onServerEvent(fn: ServerEventHandler): void {
        this._onServerEvent = fn;
    }

    /** The /ws connection opened (or reopened after a drop). */
    onConnected(fn: ConnectionHandler): void {
        this._onConnected = fn;
    }

    /** The /ws connection dropped (fired ~6 s after close, cancelled by a reconnect). */
    onDisconnected(fn: ConnectionHandler): void {
        this._onDisconnected = fn;
    }

    /** Server↔broker connection state changed (or its value on connect). */
    onMqttStatus(fn: MqttStatusHandler): void {
        this._onMqttStatus = fn;
    }

    /** Open the WebSocket to `url` and auto-reconnect on drops until disconnected. */
    connect(url: string): void {
        this._url = url;
        this._closed = false;
        this._reconnectDelay = 1_000;
        this._open();
    }

    /** Close the socket and stop reconnecting. */
    disconnect(): void {
        this._closed = true;
        if (this._reconnectTimer !== null) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = null;
        }
        if (this._disconnectTimer !== null) {
            clearTimeout(this._disconnectTimer);
            this._disconnectTimer = null;
        }
        this.ws?.close();
        this.ws = null;
        this._onDisconnected?.();
    }

    /**
     * Send a chat message over the WebSocket.
     * Returns false when the socket is not open.
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
            log.warn("[WS] Cannot open WebSocket:", err);
            this._scheduleReconnect();
            return;
        }

        this.ws.addEventListener("open", () => {
            log.info("[WS] connected →", this._url);
            this._reconnectDelay = 1_000;
            // A reconnect within the debounce window cancels the pending "dropped".
            if (this._disconnectTimer !== null) {
                clearTimeout(this._disconnectTimer);
                this._disconnectTimer = null;
            }
            this._onConnected?.();
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
            if (this._closed) {
                return;
            }
            this._scheduleReconnect();
            // Debounce the "dropped" notification: a fast reconnect (its "open"
            // handler) cancels this timer, so the badge/feed don't flicker.
            if (this._disconnectTimer === null) {
                this._disconnectTimer = setTimeout(() => {
                    this._disconnectTimer = null;
                    this._onDisconnected?.();
                }, 6_000);
            }
        });

        this.ws.addEventListener("error", () => {
            // "close" follows "error" — reconnect happens there
        });
    }

    /** Route a parsed server frame by its `type` / `state` fields. */
    private _handleFrame(data: Record<string, unknown>): void {
        const type = data["type"];
        if (type === "server_event") {
            this._onServerEvent?.(asStr(data["topic"]), data["payload"]);
            return;
        }
        if (type === "mqtt_status") {
            this._onMqttStatus?.(Boolean(data["connected"]));
            return;
        }
        if (type === "reset") {
            this._handleReset(data);
            return;
        }
        if (type === "delete_agent") {
            // Server explicitly deleted an agent — remove it and apply rest of patch
            this._applyStatePatch(data["state"] as StatePatch | undefined, asStr(data["agent_id"]));
            return;
        }
        // Any message with a "state" field is a state patch broadcast; it may
        // ALSO carry chat/stream content, so fall through after applying it.
        if (data["state"]) {
            this._applyStatePatch(data["state"]);
        }
        this._dispatchContent(data);
    }

    /** State reset broadcast — apply the state patch then clear UI as needed. */
    private _handleReset(data: Record<string, unknown>): void {
        const scope = asStr(data["scope"]);
        if (scope === "all") {
            emit("af-wipe-all");
            return;
        }
        this._applyStatePatch(data["state"] as StatePatch | undefined);
        if (scope === "chat") {
            const agent = data["agent"];
            emit("af-reset-chat", { agent: typeof agent === "string" ? agent : null });
        }
        // metrics and logs both clear the server-side activity feed; the
        // on-screen feed is append-only, so tell it to drop its entries.
        if (scope === "metrics" || scope === "logs") {
            emit("af-clear-feed");
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
        const rawFrom = asStr(data["from"], "io-gateway");
        const from = rawFrom === "io-gateway" ? this._lastAgentName : rawFrom;
        const ts = toMs(data["timestamp"]);

        if (data["type"] === "chat") {
            this._onChat?.(
                asStr(data["content"]),
                from,
                ts,
                asStr(data["to"], "user"),
                asStr(data["source"]),
                asStr(data["surface"]),
                asStr(data["surface_label"]),
                asStr(data["brain"]),
            );
        } else if (data["type"] === "stream_chunk") {
            this._onStreamChunk?.(asStr(data["content"]), from, ts);
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
