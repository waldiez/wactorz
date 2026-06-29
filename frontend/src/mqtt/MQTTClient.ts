/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * MQTT WebSocket client.
 *
 * Connects to the Mosquitto broker's WebSocket listener (default: ws://localhost:9001)
 * and emits typed events for each topic pattern Wactorz uses.
 *
 * Usage:
 * ```ts
 * const client = new MQTTClient("ws://localhost:9001");
 * client.on("heartbeat", (payload) => { ... });
 * ```
 */

import mqtt, { type MqttClient } from "mqtt";
import { nameFromWid, resolveAgentName } from "../agents/naming";
import type {
    AlertPayload,
    ChatMessage,
    HeartbeatPayload,
    HostStats,
    LogPayload,
    MetricsPayload,
    NodeHeartbeatPayload,
    SpawnPayload,
    StatusPayload,
} from "../types/agent";

/** QA safety flag raised by the QAAgent. */
export interface QaFlagPayload {
    agentId: string;
    agentName: string;
    from: string;
    category: string;
    severity: string;
    excerpt: string;
    message: string;
    timestampMs: number;
}

export interface MQTTEvents {
    connected: void;
    disconnected: void;
    error: Error;
    heartbeat: HeartbeatPayload;
    status: StatusPayload;
    alert: AlertPayload;
    spawn: SpawnPayload;
    chat: ChatMessage;
    "qa-flag": QaFlagPayload;
    /** LLM cost + token + message count metrics from an agent. */
    metrics: MetricsPayload;
    /** Log/text output from an agent. */
    logs: LogPayload;
    /** Agent completed a task. */
    completed: { agentId: string; agentName: string };
    /** Remote Wactorz node phoned home. */
    "node-heartbeat": NodeHeartbeatPayload;
    /** system/health snapshot from MonitorAgent. */
    "system-health": unknown;
    /** Host-level CPU + memory stats from the backend. */
    "host-stats": HostStats;
    /** Catch-all for raw messages not matching a known pattern. */
    raw: { topic: string; payload: unknown };
}

export type Listener<T> = (data: T) => void;
type Listeners = { [K in keyof MQTTEvents]: Array<Listener<MQTTEvents[K]>> };

export class MQTTClient {
    private client: MqttClient | null = null;
    private listeners: Partial<Listeners> = {};
    private _disconnectTimer: ReturnType<typeof setTimeout> | null = null;

    // Default: MQTT WebSocket via nginx path (/mqtt) rather than direct port 9001.
    // Override with VITE_MQTT_WS_URL env var or constructor argument.
    constructor(private readonly brokerUrl: string = "ws://localhost/mqtt") {}

    /** Connect and subscribe to all agent topics. */
    connect(): void {
        this.client = mqtt.connect(this.brokerUrl, {
            clientId: `wactorz-dashboard-${Math.random().toString(16).slice(2, 8)}`,
            keepalive: 30,
            reconnectPeriod: 2000,
        });

        this.client.on("connect", () => {
            console.info("[MQTT] Connected to", this.brokerUrl);
            // Cancel any pending disconnected notification from a brief close/reconnect cycle.
            if (this._disconnectTimer !== null) {
                clearTimeout(this._disconnectTimer);
                this._disconnectTimer = null;
            }
            this.client?.subscribe("#", { qos: 1 });
            this.emit("connected", undefined);
        });

        this.client.on("disconnect", () => {
            this.emit("disconnected", undefined);
        });

        this.client.on("close", () => {
            // mqtt.js fires "close" on every WebSocket close, including between
            // reconnect attempts (reconnectPeriod: 2000 ms).  Immediately emitting
            // "disconnected" flips the badge to "Demo fallback" on every brief
            // hiccup.  Delay the notification so a successful reconnect (which fires
            // "connect" and cancels this timer) doesn't cause a visible flicker.
            if (this._disconnectTimer !== null) {
                return;
            }
            this._disconnectTimer = setTimeout(() => {
                this._disconnectTimer = null;
                this.emit("disconnected", undefined);
            }, 6000);
        });

        this.client.on("error", err => {
            console.error("[MQTT] Error:", err);
            this.emit("error", err);
        });

        this.client.on("message", (topic: string, raw: Buffer) => {
            this.handleMessage(topic, raw);
        });
    }

    /** Disconnect cleanly. */
    disconnect(): void {
        if (this._disconnectTimer !== null) {
            clearTimeout(this._disconnectTimer);
            this._disconnectTimer = null;
        }
        this.client?.end(true);
        this.client = null;
    }

    /** Publish a raw JSON payload to a topic. Returns false if not connected. */
    publish(topic: string, payload: unknown): boolean {
        if (!this.client?.connected) {
            return false;
        }
        this.client.publish(topic, JSON.stringify(payload), { qos: 1 });
        return true;
    }

    on<K extends keyof MQTTEvents>(event: K, listener: Listener<MQTTEvents[K]>): this {
        if (!this.listeners[event]) {
            (this.listeners as Listeners)[event] = [];
        }
        (this.listeners[event] as Array<Listener<MQTTEvents[K]>>).push(listener);
        return this;
    }

    off<K extends keyof MQTTEvents>(event: K, listener: Listener<MQTTEvents[K]>): this {
        const arr = this.listeners[event] as Array<Listener<MQTTEvents[K]>> | undefined;
        if (arr) {
            const idx = arr.indexOf(listener);
            if (idx !== -1) {
                arr.splice(idx, 1);
            }
        }
        return this;
    }

    private emit<K extends keyof MQTTEvents>(event: K, data: MQTTEvents[K]): void {
        const arr = this.listeners[event] as Array<Listener<MQTTEvents[K]>> | undefined;
        arr?.forEach(fn => {
            try {
                fn(data);
            } catch (err) {
                console.error(`[MQTT] listener error on "${event}":`, err);
            }
        });
    }

    private handleMessage(topic: string, raw: Buffer): void {
        let payload: unknown;
        try {
            payload = JSON.parse(raw.toString());
        } catch {
            return;
        }

        if (
            this._routeAgentEvent(topic, payload) ||
            this._routeSystemEvent(topic, payload) ||
            this._routeParameterised(topic, payload)
        ) {
            return;
        }

        this.emit("raw", { topic, payload });
    }

    /** Fixed-shape agent topics: `agents/{id}/{heartbeat|status|alert|chat|spawn}`. */
    private _routeAgentEvent(topic: string, payload: unknown): boolean {
        if (/^agents\/.+\/heartbeat$/.test(topic)) {
            this.emit("heartbeat", normaliseHeartbeat(payload));
        } else if (/^agents\/.+\/status$/.test(topic)) {
            this.emit("status", normaliseStatus(payload));
        } else if (/^agents\/.+\/alert$/.test(topic)) {
            this.emit("alert", normaliseAlert(payload));
        } else if (/^agents\/.+\/chat$/.test(topic)) {
            this.emit("chat", normaliseChat(payload));
        } else if (/^agents\/.+\/spawn$/.test(topic)) {
            this.emit("spawn", normaliseSpawn(payload));
        } else {
            return false;
        }
        return true;
    }

    /** Exact system/* topics. */
    private _routeSystemEvent(topic: string, payload: unknown): boolean {
        if (topic === "system/qa-flag") {
            this.emit("qa-flag", payload as QaFlagPayload);
        } else if (topic === "system/spawn") {
            // legacy / alternate spawn topic
            this.emit("spawn", normaliseSpawn(payload));
        } else if (topic === "system/health") {
            this.emit("system-health", payload);
        } else if (topic === "system/host") {
            this.emit("host-stats", this._toHostStats(payload as Record<string, unknown>));
        } else {
            return false;
        }
        return true;
    }

    /** Topics carrying an id captured from the path (metrics/logs/completed/node). */
    private _routeParameterised(topic: string, payload: unknown): boolean {
        const p = payload as Record<string, unknown>;

        const metrics = topic.match(/^agents\/(.+)\/metrics$/);
        if (metrics?.[1]) {
            this._emitMetrics(metrics[1], p);
            return true;
        }

        const logs = topic.match(/^agents\/(.+)\/logs$/);
        if (logs?.[1]) {
            const message = (p["message"] ?? p["text"]) as string | undefined;
            this.emit("logs", {
                agentId: logs[1],
                agentName: this._agentName(p, logs[1]),
                ...(message !== undefined && { message }),
            });
            return true;
        }

        const completed = topic.match(/^agents\/(.+)\/completed$/);
        if (completed?.[1]) {
            this.emit("completed", {
                agentId: completed[1],
                agentName: this._agentName(p, completed[1]),
            });
            return true;
        }

        const node = topic.match(/^nodes\/([^/]+)\/heartbeat$/);
        if (node?.[1]) {
            const nodeId = p["node_id"] as string | undefined;
            this.emit("node-heartbeat", {
                node: node[1],
                agents: (p["agents"] as string[]) ?? [],
                ...(nodeId !== undefined && { nodeId }),
            });
            return true;
        }

        return false;
    }

    /** host-level CPU + memory snapshot, accepting both snake_case and camelCase keys. */
    private _toHostStats(p: Record<string, unknown>): HostStats {
        const stats: HostStats = {};
        const cpu = p["cpu"] ?? p["cpu_pct"];
        const memUsed = p["mem_used_mb"] ?? p["memUsedMb"];
        const memTotal = p["mem_total_mb"] ?? p["memTotalMb"];
        if (typeof cpu === "number") {
            stats.cpu = cpu;
        }
        if (typeof memUsed === "number") {
            stats.memUsedMb = memUsed;
        }
        if (typeof memTotal === "number") {
            stats.memTotalMb = memTotal;
        }
        return stats;
    }

    private _emitMetrics(agentId: string, p: Record<string, unknown>): void {
        const costUsd = (p["costUsd"] ?? p["cost_usd"]) as number | undefined;
        const inputTokens = (p["inputTokens"] ?? p["input_tokens"]) as number | undefined;
        const outputTokens = (p["outputTokens"] ?? p["output_tokens"]) as number | undefined;
        const messagesProcessed = (p["messagesProcessed"] ?? p["messages_processed"]) as number | undefined;
        const uptime = p["uptime"] as number | undefined;
        this.emit("metrics", {
            agentId,
            agentName: this._agentName(p, agentId),
            ...(costUsd !== undefined && { costUsd }),
            ...(inputTokens !== undefined && { inputTokens }),
            ...(outputTokens !== undefined && { outputTokens }),
            ...(messagesProcessed !== undefined && { messagesProcessed }),
            ...(uptime !== undefined && { uptime }),
        });
    }

    /** Resolve a display name from the payload, falling back to a short id. */
    private _agentName(p: Record<string, unknown>, agentId: string): string {
        return (p["agentName"] as string) ?? (p["name"] as string) ?? agentId.slice(0, 8);
    }
}

type RawObj = Record<string, unknown>;

/** Convert a raw epoch value to milliseconds.
 *  Python's time.time() returns seconds (< 1e10); JS Date.now() returns ms. */
export function toMs(raw: unknown): number {
    const n = Number(raw);
    if (!Number.isFinite(n) || n <= 0) {
        return Date.now();
    }
    return n < 1e10 ? n * 1000 : n;
}

function str(v: unknown, fallback = ""): string {
    return typeof v === "string" ? v : fallback;
}

// Agent-name resolution is single-sourced in agents/naming. Re-exported here
// (with the historical `nameFromId` alias) so existing importers/tests keep working.
export { nameFromWid as nameFromId, resolveAgentName };

export function normaliseHeartbeat(p: unknown): HeartbeatPayload {
    const o = (p ?? {}) as RawObj;
    const agentId = str(o["agentId"] ?? o["actor_id"] ?? o["agent_id"]);
    const agentName = resolveAgentName(str(o["agentName"] ?? o["name"]), agentId);
    const timestampMs = toMs(o["timestampMs"] ?? o["timestamp_ms"] ?? o["timestamp"]);
    return {
        ...(o as unknown as HeartbeatPayload),
        agentId,
        agentName,
        timestampMs,
        sequence: (o["sequence"] as number) ?? 0,
    };
}

export function normaliseChat(p: unknown): ChatMessage {
    const o = (p ?? {}) as RawObj;
    const timestampMs = toMs(o["timestampMs"] ?? o["timestamp_ms"] ?? o["timestamp"]);
    return {
        ...(o as unknown as ChatMessage),
        id: str(o["id"]) || `chat-${timestampMs}`,
        from: str(o["from"] ?? o["agentName"] ?? o["name"]),
        to: str(o["to"]) || "user", // default to "user" when field absent
        content: str(o["content"]),
        timestampMs,
    };
}

export function normaliseStatus(p: unknown): StatusPayload {
    const o = (p ?? {}) as RawObj;
    const agentId = str(o["agentId"] ?? o["actor_id"] ?? o["agent_id"]);
    const agentName = resolveAgentName(str(o["agentName"] ?? o["name"]), agentId);
    return {
        ...(o as unknown as StatusPayload),
        agentId,
        agentName,
    };
}

export function normaliseSpawn(p: unknown): SpawnPayload {
    const o = (p ?? {}) as RawObj;
    const agentId = str(o["agentId"] ?? o["actor_id"] ?? o["agent_id"]);
    const agentName = resolveAgentName(str(o["agentName"] ?? o["name"]), agentId);
    return {
        ...(o as unknown as SpawnPayload),
        agentId,
        agentName,
        timestampMs: toMs(o["timestampMs"] ?? o["timestamp_ms"] ?? o["timestamp"]),
    };
}

export function normaliseAlert(p: unknown): AlertPayload {
    const o = (p ?? {}) as RawObj;
    const agentId = str(o["agentId"] ?? o["actor_id"] ?? o["agent_id"]);
    const agentName = resolveAgentName(str(o["agentName"] ?? o["name"]), agentId);
    return {
        ...(o as unknown as AlertPayload),
        agentId,
        agentName,
        timestampMs: toMs(o["timestampMs"] ?? o["timestamp_ms"] ?? o["timestamp"]),
    };
}
