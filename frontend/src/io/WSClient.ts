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
 * Chat is sent back over this socket, never over a broker.
 */
import { log } from "./logger";
import { toMs } from "../time";
import { emit } from "../events";
import { IO_GATEWAY, MAIN_AGENT } from "../agents/naming";
import type { StatePatchAgent, SnapshotStats, LogFeedItem } from "../types/ws";
import type { AddressChoice } from "../types/agent";

export type ChatHandler = (
    content: string,
    from: string,
    timestampMs: number,
    to: string,
    source: string,
    surface: string,
    surfaceLabel: string,
    brain: string,
    threadId: string,
    addressChoice: AddressChoice | null,
) => void;
/** A piece of a streamed reply; `agentId` is the thread it is in. */
export type StreamChunkHandler = (chunk: string, from: string, timestampMs: number, agentId: string) => void;
export type StreamEndHandler = (from: string, agentId: string) => void;
/** A live `server_event` frame carrying a topic-addressed payload, and for a chat
 *  message the thread the server says it is in. */
export type ServerEventHandler = (topic: string, payload: unknown, threadId: string) => void;
/** The /ws connection opened or dropped. */
export type ConnectionHandler = () => void;
/** Server↔broker connection state — combined with the /ws state to drive "live". */
export type MqttStatusHandler = (connected: boolean) => void;

/** What a client can be told of, with what each handler takes. */
export interface WSEvents {
    /** Complete (non-streaming) message — slash command replies, errors, etc. */
    chat: ChatHandler;
    /** One streaming chunk from the LLM. */
    streamChunk: StreamChunkHandler;
    /** Stream finished — render the final markdown for the accumulated reply. */
    streamEnd: StreamEndHandler;
    /** Server broadcast a state patch (agent list updated, or agent deleted). */
    statePatch: StatePatchHandler;
    /** Server broadcast new log_feed entries inside a state patch. */
    logFeed: LogFeedHandler;
    /** A live `server_event` frame — hand `(topic, payload)` to the ServerEventRouter. */
    serverEvent: ServerEventHandler;
    /** The /ws connection opened (or reopened after a drop). */
    connected: ConnectionHandler;
    /** The /ws connection dropped (fired ~6 s after close, cancelled by a reconnect). */
    disconnected: ConnectionHandler;
    /** Server↔broker connection state changed (or its value on connect). */
    mqttStatus: MqttStatusHandler;
}

/** Coerce an untrusted JSON field to a string; non-strings fall back (so a
 *  hostile object can't stringify to "[object Object]"). */
const asStr = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);

/** Coerce an untrusted JSON field to an array; anything else becomes empty, so
 *  a caller can iterate without checking what the server actually sent. */
const asArray = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

/** The addresses a reply offers to pick from, and the message a pick re-sends, or null when it offers none. */
function addressChoiceOf(data: Record<string, unknown>): AddressChoice | null {
    const addresses = asArray(data["choices"]).filter((a): a is string => typeof a === "string" && a !== "");
    const retryOf = asStr(data["retry_of"]);
    return addresses.length && retryOf ? { addresses, retryOf } : null;
}

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

/**
 * Tell the page what a frame that only informs it says, as the app event it
 * becomes; whether `type` is such a frame. Emitted rather than handled here:
 * WSClient owns the socket, not the views that decide what follows. A switch
 * over the types it knows, so the frame's own text never picks what is called.
 */
function told(type: unknown, data: Record<string, unknown>): boolean {
    switch (type) {
        case "turn":
            // The agent the person's message reached, which a mention can make
            // another than the thread it was typed in.
            emit("af-turn-reached", {
                id: asStr(data["id"]),
                agentId: asStr(data["agent_id"]),
                name: asStr(data["name"]),
            });
            return true;
        case "turn_end":
            // For clients that follow a turn to its end. The page shows a reply as
            // it arrives and has nothing to do here.
            return true;
        case "app_log":
            // Records the server wrote since the last frame.
            emit("af-app-log", { entries: asArray(data["entries"]) });
            return true;
        default:
            return false;
    }
}

export class WSClient {
    private readonly _handlers: { [K in keyof WSEvents]?: Set<WSEvents[K]> } = {};
    private ws: WebSocket | null = null;
    private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    /** Debounces "dropped" so a brief close/reopen doesn't flip the UI to disconnected. */
    private _disconnectTimer: ReturnType<typeof setTimeout> | null = null;
    private _reconnectDelay = 1_000;
    /** Fires the liveness check; null whenever no socket is being watched. */
    private _watchdogTimer: ReturnType<typeof setInterval> | null = null;
    /** When this socket last carried anything at all, parseable or not. */
    private _lastFrameAt = 0;
    private _url = "";
    private _closed = false;

    /**
     * How often to ask the server to say something.
     *
     * A healthy connection usually carries the periodic totals patch anyway, so
     * this is not the only inbound traffic there is. It is the only traffic this
     * check owns: resting on another feature's broadcasts staying unconditional
     * would make liveness quietly wrong the day that changes. Asking costs one
     * frame, and it is the only thing here that proves the outbound direction.
     */
    private static readonly PING_EVERY_MS = 30_000;

    /**
     * How long a socket may carry nothing before it is presumed dead.
     *
     * Two and a half missed rounds. The server pings on its own every 30 s, but
     * the browser answers those in its protocol handling and never tells us, so
     * an open socket proves nothing about the far end. Under a proxy the near
     * leg can outlive the far one: `send()` then succeeds into nowhere, `close`
     * never fires, and nothing reconnects.
     */
    private static readonly SILENT_FOR_MS = 75_000;

    /** True while the WebSocket is open. */
    get connected(): boolean {
        return this.ws?.readyState === WebSocket.OPEN;
    }

    /** Be told of `event`, each handler in the order it subscribed; returns the way to stop. */
    on<K extends keyof WSEvents>(event: K, handler: WSEvents[K]): () => void {
        const handlers = this._handlers[event] ?? new Set();
        (this._handlers as Record<K, Set<WSEvents[K]>>)[event] = handlers;
        handlers.add(handler);
        return () => handlers.delete(handler);
    }

    private _emit<K extends keyof WSEvents>(event: K, ...args: Parameters<WSEvents[K]>): void {
        const handlers = this._handlers[event] as Set<(...a: Parameters<WSEvents[K]>) => void> | undefined;
        handlers?.forEach(handler => handler(...args));
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
        this._stopWatching();
        this.ws?.close();
        this.ws = null;
        this._emit("disconnected");
    }

    /**
     * Send a chat message over the WebSocket: to the thread of `agentName` (`agentId`),
     * as the message `id`, which the server's `turn` frame names back.
     * Returns false when the socket is not open.
     */
    send(
        content: string,
        agentName = MAIN_AGENT,
        attachments: string[] = [],
        agentId = "",
        id = "",
    ): boolean {
        if (!this.connected) {
            return false;
        }
        // Ids only — the server holds the name, type and size it stored, so a
        // turn cannot claim a file is something other than what was uploaded.
        this.ws!.send(
            JSON.stringify({
                type: "chat",
                content,
                agent_name: agentName,
                ...(agentId ? { agent_id: agentId } : {}),
                ...(id ? { id } : {}),
                ...(attachments.length ? { attachments } : {}),
            }),
        );
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
            this._startWatching();
            this._emit("connected");
        });

        this.ws.addEventListener("message", (ev: MessageEvent) => {
            // Stamped before parsing: the bytes arriving is the whole question,
            // and judging on parseable frames alone would close a socket that is
            // plainly still connected.
            this._lastFrameAt = Date.now();
            let data: Record<string, unknown>;
            try {
                data = JSON.parse(ev.data as string) as Record<string, unknown>;
            } catch {
                return;
            }
            this._handleFrame(data);
        });

        this.ws.addEventListener("close", () => {
            this._stopWatching();
            if (this._closed) {
                return;
            }
            this._scheduleReconnect();
            // Debounce the "dropped" notification: a fast reconnect (its "open"
            // handler) cancels this timer, so the badge/feed don't flicker.
            if (this._disconnectTimer === null) {
                this._disconnectTimer = setTimeout(() => {
                    this._disconnectTimer = null;
                    this._emit("disconnected");
                }, 6_000);
            }
        });

        this.ws.addEventListener("error", () => {
            // "close" follows "error" — reconnect happens there
        });
    }

    /** Watch the socket that just opened, from a clean slate. */
    private _startWatching(): void {
        this._stopWatching();
        // Reset rather than carried over: keeping the old stamp would close the
        // fresh socket at once, which is a reconnect loop rather than a recovery.
        this._lastFrameAt = Date.now();
        this._watchdogTimer = setInterval(() => this._checkAlive(), WSClient.PING_EVERY_MS);
    }

    private _stopWatching(): void {
        if (this._watchdogTimer !== null) {
            clearInterval(this._watchdogTimer);
            this._watchdogTimer = null;
        }
    }

    /** Give up on a socket that has carried nothing, or ask it to speak. */
    private _checkAlive(): void {
        if (!this.connected) {
            this._stopWatching();
            return;
        }
        if (Date.now() - this._lastFrameAt >= WSClient.SILENT_FOR_MS) {
            log.warn("[WS] silent too long — closing so it can reconnect");
            // Closed rather than reopened here: closing is what the reconnect
            // path already listens for, so there is one way back and not two.
            this.ws?.close();
            return;
        }
        this.sendRaw({ type: "ping" });
    }

    /** Route a parsed server frame by its `type` / `state` fields. */
    private _handleFrame(data: Record<string, unknown>): void {
        const type = data["type"];
        if (type === "server_event") {
            this._emit("serverEvent", asStr(data["topic"]), data["payload"], asStr(data["thread_id"]));
            return;
        }
        if (told(type, data)) {
            return;
        }
        if (type === "mqtt_status") {
            this._emit("mqttStatus", Boolean(data["connected"]));
            return;
        }
        if (type === "reset") {
            this._handleReset(data);
            return;
        }
        if (type === "delete_agent") {
            this._handleDelete(data);
            return;
        }
        // Any message with a "state" field is a state patch broadcast; it may
        // ALSO carry chat/stream content, so fall through after applying it.
        if (data["state"]) {
            this._applyStatePatch(data["state"]);
        }
        this._dispatchContent(data);
    }

    /** The server deleted an agent: remove it and apply the rest of the patch. */
    private _handleDelete(data: Record<string, unknown>): void {
        this._applyStatePatch(data["state"] as StatePatch | undefined, asStr(data["agent_id"]));
        // The frame names the agent, so this is a fact, not reset churn: the
        // chat target can be judged against the list that is left.
        emit("af-agents-settled", { reason: "deleted" });
    }

    /** State reset broadcast — apply the state patch then clear UI as needed. */
    private _handleReset(data: Record<string, unknown>): void {
        const scope = asStr(data["scope"]);
        if (scope === "all") {
            const patch = data["state"] as StatePatch | undefined;
            const survivors = (patch?.agents ?? []).map(a => a.agent_id).filter((id): id is string => !!id);
            emit("af-wipe-all", { survivors });
            // The reset frame carries the survivors itself — the server rebuilds
            // from the registry before broadcasting — so apply it rather than
            // sitting on an empty list until the next heartbeat.
            this._applyStatePatch(data["state"] as StatePatch | undefined);
            // The list is now whatever survived, in one frame — the only moment
            // a stale chat target can be judged without racing the churn.
            emit("af-agents-settled", { reason: "reset" });
            return;
        }
        this._applyStatePatch(data["state"] as StatePatch | undefined);
        if (scope === "chat") {
            const agent = data["agent"];
            const agentId = data["agent_id"];
            emit("af-reset-chat", {
                agent: typeof agent === "string" ? agent : null,
                agentId: typeof agentId === "string" ? agentId : null,
            });
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
        this._emit("statePatch", patch?.agents ?? [], removedId, stats);
        if (patch?.log_feed?.length) {
            this._emit("logFeed", patch.log_feed);
        }
    }

    /** Deliver chat / stream content carried by a frame. */
    private _dispatchContent(data: Record<string, unknown>): void {
        // Every turn's frames name the agent that owns them. The transport's own
        // id is only on what comes before any turn, which is main's to answer.
        const rawFrom = asStr(data["from"]) || IO_GATEWAY;
        const from = rawFrom === IO_GATEWAY ? MAIN_AGENT : rawFrom;
        const ts = toMs(data["timestamp"]);
        const agentId = asStr(data["agent_id"]);

        if (data["type"] === "chat") {
            this._emit(
                "chat",
                asStr(data["content"]),
                from,
                ts,
                asStr(data["to"], "user"),
                asStr(data["source"]),
                asStr(data["surface"]),
                asStr(data["surface_label"]),
                asStr(data["brain"]),
                agentId,
                addressChoiceOf(data),
            );
        } else if (data["type"] === "stream_chunk") {
            this._emit("streamChunk", asStr(data["content"]), from, ts, agentId);
        } else if (data["type"] === "stream_end") {
            this._emit("streamEnd", from, agentId);
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
