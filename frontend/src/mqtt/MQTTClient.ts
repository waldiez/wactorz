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
import { log } from "../io/logger";
import { uid } from "../ids";
import { nameFromWid, resolveAgentName } from "../agents/naming";
import type {
    AgentState,
    AlertPayload,
    ChatMessage,
    HeartbeatPayload,
    HostStats,
    LogPayload,
    MetricsPayload,
    NodeHeartbeatPayload,
    QaFlagPayload,
    SpawnPayload,
    StatusPayload,
} from "../types/agent";

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
            log.info("[MQTT] Connected to", this.brokerUrl);
            // Cancel any pending disconnected notification from a brief close/reconnect cycle.
            if (this._disconnectTimer !== null) {
                clearTimeout(this._disconnectTimer);
                this._disconnectTimer = null;
            }
            // Scope to the prefixes the dashboard actually routes (see
            // handleMessage: agents/system/nodes + homeassistant/state_changes via
            // the raw→parseHaRawEvent path) instead of "#", so unrelated broker
            // topics never reach the browser to be parsed. All dynamic ids live
            // under these prefixes. Subscribe narrowly to state_changes (not all of
            // homeassistant/#) to skip the large chunked map payloads we don't use.
            this.client?.subscribe(["agents/#", "system/#", "nodes/#", "homeassistant/state_changes/#"], {
                qos: 1,
            });
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
            log.error("[MQTT] Error:", err);
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

    /** Register a listener for a typed event. Chainable. */
    on<K extends keyof MQTTEvents>(event: K, listener: Listener<MQTTEvents[K]>): this {
        if (!this.listeners[event]) {
            (this.listeners as Listeners)[event] = [];
        }
        (this.listeners[event] as Array<Listener<MQTTEvents[K]>>).push(listener);
        return this;
    }

    /** Remove a previously registered listener. Chainable. */
    off<K extends keyof MQTTEvents>(event: K, listener: Listener<MQTTEvents[K]>): this {
        const arr = this.listeners[event];
        if (arr) {
            const idx = arr.indexOf(listener);
            if (idx !== -1) {
                arr.splice(idx, 1);
            }
        }
        return this;
    }

    private emit<K extends keyof MQTTEvents>(event: K, data: MQTTEvents[K]): void {
        const arr = this.listeners[event];
        arr?.forEach(fn => {
            try {
                fn(data);
            } catch (err) {
                log.error(`[MQTT] listener error on "${event}":`, err);
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

        // Structured routing needs an object. A null/primitive payload can't match
        // any known shape and would throw on property access inside the routers,
        // so surface it as `raw` instead of letting it escape the message handler.
        if (payload === null || typeof payload !== "object") {
            this.emit("raw", { topic, payload });
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
            this.emit("qa-flag", normaliseQaFlag(payload));
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
            const message = optStr(p["message"] ?? p["text"]);
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
            const nodeId = optStr(p["node_id"]);
            this.emit("node-heartbeat", {
                node: node[1],
                agents: strArray(p["agents"]),
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
        const costUsd = num(p["costUsd"] ?? p["cost_usd"]);
        const inputTokens = num(p["inputTokens"] ?? p["input_tokens"]);
        const outputTokens = num(p["outputTokens"] ?? p["output_tokens"]);
        const messagesProcessed = num(p["messagesProcessed"] ?? p["messages_processed"]);
        const uptime = num(p["uptime"]);
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
        return str(p["agentName"] ?? p["name"]) || agentId.slice(0, 8);
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

// --- Field validators ---------------------------------------------------------
// Every MQTT payload is untrusted external JSON. These coerce individual fields
// to their declared types so a malformed or hostile value (e.g. a number where a
// string is expected, or an unknown key) can't be laundered into a typed payload.

/** Coerce to a plain object; null/primitive payloads become `{}`. */
function asObj(p: unknown): RawObj {
    return p !== null && typeof p === "object" ? (p as RawObj) : {};
}

/** A string value, or undefined when not a string. */
function optStr(v: unknown): string | undefined {
    return typeof v === "string" ? v : undefined;
}

/** A finite number, or undefined otherwise. */
function num(v: unknown): number | undefined {
    return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

/** A boolean, or undefined when not a boolean. */
function bool(v: unknown): boolean | undefined {
    return typeof v === "boolean" ? v : undefined;
}

/** Validate an untrusted value into an {@link AgentState}, defaulting to "running". */
function coerceState(v: unknown): AgentState {
    if (v === "initializing" || v === "running" || v === "paused" || v === "stopped") {
        return v;
    }
    if (v !== null && typeof v === "object" && typeof (v as { failed?: unknown }).failed === "string") {
        return { failed: (v as { failed: string }).failed };
    }
    return "running";
}

/** Validate an untrusted alert severity, defaulting to "info". */
function coerceSeverity(v: unknown): AlertPayload["severity"] {
    return v === "info" || v === "warning" || v === "error" || v === "critical" ? v : "info";
}

/** Keep only the string entries of an untrusted array. */
function strArray(v: unknown): string[] {
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

// Agent-name resolution is single-sourced in agents/naming; re-exported here,
// with `nameFromId` as an alias of nameFromWid, for callers that import it from
// the MQTT module.
export { nameFromWid as nameFromId, resolveAgentName };

/** Normalise a raw heartbeat payload, tolerating snake_case/camelCase keys and resolving id + name. */
export function normaliseHeartbeat(p: unknown): HeartbeatPayload {
    const o = asObj(p);
    const agentId = str(o["agentId"] ?? o["actor_id"] ?? o["agent_id"]);
    const out: HeartbeatPayload = {
        agentId,
        agentName: resolveAgentName(str(o["agentName"] ?? o["name"]), agentId),
        state: coerceState(o["state"]),
        sequence: num(o["sequence"]) ?? 0,
        timestampMs: toMs(o["timestampMs"] ?? o["timestamp_ms"] ?? o["timestamp"]),
    };
    const cpu = num(o["cpu"]);
    if (cpu !== undefined) {
        out.cpu = cpu;
    }
    const memoryMb = num(o["memory_mb"]);
    if (memoryMb !== undefined) {
        out.memory_mb = memoryMb;
    }
    const task = optStr(o["task"]);
    if (task !== undefined) {
        out.task = task;
    }
    const node = optStr(o["node"]);
    if (node !== undefined) {
        out.node = node;
    }
    return out;
}

/** Normalise a raw chat payload; defaults `to` to "user" and synthesises an id when absent. */
export function normaliseChat(p: unknown): ChatMessage {
    const o = asObj(p);
    const timestampMs = toMs(o["timestampMs"] ?? o["timestamp_ms"] ?? o["timestamp"]);
    const source = optStr(o["source"]);
    const surface = optStr(o["surface"]);
    const surfaceLabel = optStr(o["surfaceLabel"] ?? o["surface_label"]);
    const brain = optStr(o["brain"]);
    return {
        id: str(o["id"]) || uid("chat"), // WID, not `chat-${ms}`: same-ms ids collide and dedupe-drop
        from: str(o["from"] ?? o["agentName"] ?? o["name"]),
        to: str(o["to"]) || "user", // default to "user" when field absent
        content: str(o["content"]),
        timestampMs,
        ...(source !== undefined && { source }),
        ...(surface !== undefined && { surface }),
        ...(surfaceLabel !== undefined && { surfaceLabel }),
        ...(brain !== undefined && { brain }),
    };
}

/** Normalise a raw status payload, resolving agent id + display name. */
export function normaliseStatus(p: unknown): StatusPayload {
    const o = asObj(p);
    const agentId = str(o["agentId"] ?? o["actor_id"] ?? o["agent_id"]);
    const out: StatusPayload = {
        agentId,
        agentName: resolveAgentName(str(o["agentName"] ?? o["name"]), agentId),
        state: coerceState(o["state"]),
        messagesReceived: num(o["messagesReceived"] ?? o["messages_received"]) ?? 0,
        messagesProcessed: num(o["messagesProcessed"] ?? o["messages_processed"]) ?? 0,
        messagesFailed: num(o["messagesFailed"] ?? o["messages_failed"]) ?? 0,
    };
    const prot = bool(o["protected"]);
    if (prot !== undefined) {
        out.protected = prot;
    }
    return out;
}

/** Normalise a raw spawn payload, resolving id + name and coercing the timestamp to ms. */
export function normaliseSpawn(p: unknown): SpawnPayload {
    const o = asObj(p);
    const agentId = str(o["agentId"] ?? o["actor_id"] ?? o["agent_id"]);
    const out: SpawnPayload = {
        agentId,
        agentName: resolveAgentName(str(o["agentName"] ?? o["name"]), agentId),
        agentType: str(o["agentType"] ?? o["agent_type"]),
        timestampMs: toMs(o["timestampMs"] ?? o["timestamp_ms"] ?? o["timestamp"]),
    };
    const prot = bool(o["protected"]);
    if (prot !== undefined) {
        out.protected = prot;
    }
    return out;
}

/** Normalise a raw alert payload, resolving id + name and coercing the timestamp to ms. */
export function normaliseAlert(p: unknown): AlertPayload {
    const o = asObj(p);
    const agentId = str(o["agentId"] ?? o["actor_id"] ?? o["agent_id"]);
    return {
        agentId,
        agentName: resolveAgentName(str(o["agentName"] ?? o["name"]), agentId),
        severity: coerceSeverity(o["severity"]),
        message: str(o["message"]),
        timestampMs: toMs(o["timestampMs"] ?? o["timestamp_ms"] ?? o["timestamp"]),
    };
}

/** Normalise a raw QA-flag payload, resolving id + name and coercing each field to its type. */
export function normaliseQaFlag(p: unknown): QaFlagPayload {
    const o = asObj(p);
    const agentId = str(o["agentId"] ?? o["actor_id"] ?? o["agent_id"]);
    return {
        agentId,
        agentName: resolveAgentName(str(o["agentName"] ?? o["name"]), agentId),
        from: str(o["from"]),
        category: str(o["category"]),
        severity: str(o["severity"]),
        excerpt: str(o["excerpt"]),
        message: str(o["message"]),
        timestampMs: toMs(o["timestampMs"] ?? o["timestamp_ms"] ?? o["timestamp"]),
    };
}
