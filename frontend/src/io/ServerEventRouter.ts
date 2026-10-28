/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { log } from "./logger";
import { uid } from "../ids";
import { resolveAgentName } from "../agents/naming";
import { readingsFrom } from "../agents/nodeReadings";
import type {
    AgentState,
    AlertPayload,
    AnswerPayload,
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
import { toMs } from "../time";

type RawObj = Record<string, unknown>;

export interface ServerEvents {
    heartbeat: HeartbeatPayload;
    status: StatusPayload;
    alert: AlertPayload;
    spawn: SpawnPayload;
    chat: ChatMessage;
    /** A person answered a question an agent asked, on some channel. */
    answer: AnswerPayload;
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
    /** A stored model's pointer moved (`models/{name}/{active|shadow}`); an unset one carries an empty payload. */
    "model-pointer": { name: string; pointer: string };
    /** Catch-all for raw messages not matching a known pattern. */
    raw: { topic: string; payload: unknown };
}

/** `models/{name}/{pointer}`: where main says a stored model's pointer stands. */
const MODEL_POINTER = /^models\/([^/]+)\/(active|shadow)$/;

export type Listener<T> = (data: T) => void;
type Listeners = { [K in keyof ServerEvents]: Array<Listener<ServerEvents[K]>> };

export class ServerEventRouter {
    private listeners: Partial<Listeners> = {};

    /** Register a listener for a typed event. Chainable. */
    on<K extends keyof ServerEvents>(event: K, listener: Listener<ServerEvents[K]>): this {
        if (!this.listeners[event]) {
            (this.listeners as Listeners)[event] = [];
        }
        (this.listeners[event] as Array<Listener<ServerEvents[K]>>).push(listener);
        return this;
    }

    /** Remove a previously registered listener. Chainable. */
    off<K extends keyof ServerEvents>(event: K, listener: Listener<ServerEvents[K]>): this {
        const arr = this.listeners[event];
        if (arr) {
            const idx = arr.indexOf(listener);
            if (idx !== -1) {
                arr.splice(idx, 1);
            }
        }
        return this;
    }

    /** Hand on one relayed frame. `threadId` is the thread the server says a chat
     *  message is in, when that is not its sender's own. */
    route(topic: string, payload: unknown, threadId = ""): void {
        // Before the shape check: an unset pointer is published as an empty payload.
        const pointer = MODEL_POINTER.exec(topic);
        if (pointer) {
            this.emit("model-pointer", { name: pointer[1] ?? "", pointer: pointer[2] ?? "" });
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
            this._routeAgentEvent(topic, payload, threadId) ||
            this._routeSystemEvent(topic, payload) ||
            this._routeParameterised(topic, payload)
        ) {
            return;
        }

        this.emit("raw", { topic, payload });
    }

    private emit<K extends keyof ServerEvents>(event: K, data: ServerEvents[K]): void {
        const arr = this.listeners[event];
        arr?.forEach(fn => {
            try {
                fn(data);
            } catch (err) {
                log.error(`[ServerEvent] listener error on "${event}":`, err);
            }
        });
    }

    /** Fixed-shape agent topics: `agents/{id}/{heartbeat|status|alert|chat|answer|spawn}`. */
    private _routeAgentEvent(topic: string, payload: unknown, threadId: string): boolean {
        if (/^agents\/.+\/heartbeat$/.test(topic)) {
            this.emit("heartbeat", normaliseHeartbeat(payload));
        } else if (/^agents\/.+\/status$/.test(topic)) {
            this.emit("status", normaliseStatus(payload));
        } else if (/^agents\/.+\/alert$/.test(topic)) {
            this.emit("alert", normaliseAlert(payload));
        } else if (/^agents\/.+\/chat$/.test(topic)) {
            this.emit("chat", normaliseChat(payload, agentIdOf(topic), threadId));
        } else if (/^agents\/.+\/answer$/.test(topic)) {
            this.emit("answer", normaliseAnswer(payload, agentIdOf(topic)));
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
            this._emitLogs(logs[1], p);
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
                readings: readingsFrom(p),
            });
            return true;
        }

        return false;
    }

    /** An agent's log event, with the turn it was working on when it logged it. */
    private _emitLogs(agentId: string, p: Record<string, unknown>): void {
        const message = optStr(p["message"] ?? p["text"]);
        const turn = optStr(p["turn"]);
        this.emit("logs", {
            agentId,
            agentName: this._agentName(p, agentId),
            ...(message !== undefined && { message }),
            ...(turn !== undefined && { turn }),
        });
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

function str(v: unknown, fallback = ""): string {
    return typeof v === "string" ? v : fallback;
}

// --- Field validators ---------------------------------------------------------
// Every payload is untrusted external JSON. These coerce individual fields
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
    if (v === "initializing" || v === "running" || v === "stopped") {
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

/** The `{id}` of an `agents/{id}/...` topic. */
function agentIdOf(topic: string): string {
    return topic.split("/")[1] ?? "";
}

/** The options a chat frame offers, as strings; none when the field is absent or not a list. */
function optionsOf(raw: unknown): string[] | undefined {
    if (!Array.isArray(raw)) {
        return undefined;
    }
    const options = raw.map(o => str(o).trim()).filter(o => o !== "");
    return options.length ? options : undefined;
}

/** What a chat frame says about the question it asks: who asked, the options,
 *  and the channel it also went to. Each only when the frame carries it. */
function questionFields(o: RawObj, agentId: string): Pick<ChatMessage, "agentId" | "options" | "channel"> {
    const options = optionsOf(o["options"]);
    const channel = optStr(o["channel"]);
    return {
        ...(agentId && { agentId }),
        ...(options !== undefined && { options }),
        ...(channel !== undefined && channel !== "dashboard" && { channel }),
    };
}

/** Normalise a raw chat payload; defaults `to` to "user" and synthesises an id when absent.
 *  `agentId` is the sender's id from the topic the frame arrived on, which an
 *  answer to the frame's options has to name; `threadId` the thread the server
 *  says the message is in, the sender's own unless it says otherwise. */
export function normaliseChat(p: unknown, agentId = "", threadId = ""): ChatMessage {
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
        threadId: threadId || agentId,
        content: str(o["content"]),
        timestampMs,
        ...(source !== undefined && { source }),
        ...(surface !== undefined && { surface }),
        ...(surfaceLabel !== undefined && { surfaceLabel }),
        ...(brain !== undefined && { brain }),
        ...questionFields(o, agentId),
    };
}

/** Normalise a raw answer payload from `agents/{id}/answer`. */
export function normaliseAnswer(p: unknown, agentId = ""): AnswerPayload {
    const o = asObj(p);
    return {
        id: str(o["id"]),
        agentId: agentId || str(o["agentId"] ?? o["agent_id"]),
        option: str(o["option"]),
        index: num(o["index"]) ?? 0,
        channel: str(o["channel"]) || "dashboard",
        timestampMs: toMs(o["timestampMs"] ?? o["timestamp_ms"] ?? o["timestamp"]),
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
