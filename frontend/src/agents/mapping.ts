/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Pure mappers from server payloads to UI models.
 *
 * - `toAgentInfo` / `buildMetricsUpdate`: WS state-patch / MQTT metrics → {@link AgentInfo}.
 * - `mapLogFeedItem`: a WS `log_feed` entry → an {@link FeedItem} (or null to drop).
 * - the `*FeedItem` builders near the end: live MQTT event payloads → {@link FeedItem}
 *   (the real-time counterpart of the WS `log_feed` replay above).
 *
 * Kept free of DOM / side effects so they are trivially unit-testable.
 */
import type {
    AgentInfo,
    AgentState,
    HeartbeatPayload,
    SpawnPayload,
    AlertPayload,
    StatusPayload,
    LogPayload,
    NodeHeartbeatPayload,
    ChatMessage,
    MetricsPayload,
} from "../types/agent";
import type { StatePatchAgent, LogFeedItem } from "../io/WSChatClient";
import type { QaFlagPayload } from "../mqtt/MQTTClient";
import type { FeedItem } from "../types/feed";
import { nameFromWid, resolveAgentName } from "./naming";

/** Coerce the backend's free-form state/status string into an {@link AgentState}. */
function toAgentState(raw: string): AgentState {
    if (raw === "paused" || raw === "stopped" || raw === "initializing") {
        return raw;
    }
    return "running";
}

/** Build an {@link AgentInfo} from a WS state-patch agent (caller ensures `agent_id`). */
export function toAgentInfo(a: StatePatchAgent): AgentInfo {
    const update: AgentInfo = {
        id: a.agent_id,
        name: resolveAgentName(a.name, a.agent_id),
        state: toAgentState((a.state ?? a.status ?? "running") as string),
        protected: a.protected ?? false,
    };
    if (a.messages_processed != null) {
        update.messagesProcessed = a.messages_processed;
    }
    if (a.cost_usd != null) {
        update.costUsd = a.cost_usd;
    }
    if (a.uptime != null) {
        update.uptime = a.uptime;
    }
    if (a.cpu != null) {
        update.cpu = a.cpu;
    }
    if (a.mem != null) {
        update.mem = a.mem;
    }
    if (a.task != null) {
        update.task = a.task;
    }
    if (a.agent_type != null) {
        update.agentType = a.agent_type;
    }
    return update;
}

/**
 * Merge cost/message/uptime metrics onto an existing agent, preserving its
 * identity/state and copying only the fields the payload actually carries
 * (so `exactOptionalPropertyTypes` stays happy and undefined never clobbers).
 */
export function buildMetricsUpdate(p: MetricsPayload, existing: AgentInfo): AgentInfo {
    const update: AgentInfo = {
        id: p.agentId,
        name: existing.name,
        state: existing.state,
        protected: existing.protected,
    };
    if (p.messagesProcessed !== undefined) {
        update.messagesProcessed = p.messagesProcessed;
    }
    if (p.costUsd !== undefined) {
        update.costUsd = p.costUsd;
    }
    if (p.uptime !== undefined) {
        update.uptime = p.uptime;
    }
    return update;
}

interface FeedCtx {
    agentName: string;
    ts: number;
}

/** Per-`type` builders for log-feed entries; missing types are dropped. */
const FEED_MAPPERS: Record<string, (item: LogFeedItem, ctx: FeedCtx) => FeedItem | null> = {
    spawned: (item, { agentName, ts }) => ({
        type: "spawn",
        label: `spawned (${item.agentType ?? item.agent_type ?? "agent"})`,
        agentName: item.agentName ?? item.name ?? agentName,
        timestamp: ts,
    }),
    completed: (_item, { agentName, ts }) => ({
        type: "spawn",
        label: "task completed",
        agentName,
        timestamp: ts,
    }),
    log: (item, { agentName, ts }) => {
        const msg = item.message ?? item.text ?? "";
        return msg ? { type: "chat", label: msg, agentName, timestamp: ts } : null;
    },
    status: (item, { agentName, ts }) => {
        const st = (item.status as Record<string, unknown> | undefined)?.["state"] as string | undefined;
        return st === "stopped" ? { type: "stopped", label: "stopped", agentName, timestamp: ts } : null;
    },
    alert: (item, { agentName, ts }) => {
        const isError = item.severity === "error" || item.severity === "critical";
        return {
            type: isError ? "alert-error" : "alert-warning",
            label: item.message ?? "",
            agentName: item.name ?? agentName,
            timestamp: ts,
        };
    },
};

/**
 * Build an `agent_id → friendly name` index from a log_feed batch.
 *
 * Many entries (notably `log`) carry only the agent's id; the friendly name
 * arrives on the `spawned` entry for the same agent. Scanning the whole batch
 * first lets us attribute those nameless entries on reload, when no live MQTT
 * spawn event is available to populate the store.
 */
export function buildNameIndex(items: LogFeedItem[]): Map<string, string> {
    const index = new Map<string, string>();
    for (const item of items) {
        const id = item.agent_id;
        const name = item.name ?? item.agentName;
        if (id && name) {
            index.set(id, resolveAgentName(name, id));
        }
    }
    return index;
}

/**
 * Map a WS `log_feed` entry to a feed item, or null when it should be dropped.
 *
 * `resolveName` supplies a friendly name for entries that carry only an id
 * (see {@link buildNameIndex}); it typically combines the batch index with the
 * live store. Falls back to the id-derived name only when nothing else resolves.
 */
export function mapLogFeedItem(
    item: LogFeedItem,
    resolveName?: (agentId: string) => string | undefined,
): FeedItem | null {
    const agentId = item.agent_id ?? "";
    const resolved = item.name ?? item.agentName ?? resolveName?.(agentId);
    const agentName = resolved ?? (nameFromWid(agentId) || agentId.slice(0, 8) || "system");
    const ts = item.timestamp ? item.timestamp * 1000 : Date.now();
    return FEED_MAPPERS[item.type]?.(item, { agentName, ts }) ?? null;
}

/** High-water mark for log_feed replay, advanced in place by {@link selectLogFeedReplay}. */
export interface LogFeedReplayState {
    maxTs: number;
    initialized: boolean;
}

/**
 * Decide which `log_feed` items to (re)push, advancing `state` in place.
 *
 * The first batch replays the whole backlog (it happened before the browser
 * connected — MQTT won't re-deliver it). Later batches emit only items newer
 * than the high-water mark; the mark is still advanced even when nothing is
 * pushed, so a reconnect doesn't replay the backlog. While direct MQTT is live
 * it already delivers these events, so we skip (but keep advancing the mark).
 * Returned items are oldest-first and exclude entries that don't map to a feed.
 */
export function selectLogFeedReplay(
    items: LogFeedItem[],
    state: LogFeedReplayState,
    mqttLive: boolean,
    resolveName?: (agentId: string) => string | undefined,
): FeedItem[] {
    const toFeed = (subset: LogFeedItem[]): FeedItem[] =>
        [...subset]
            .reverse()
            .map(item => mapLogFeedItem(item, resolveName))
            .filter((f): f is FeedItem => f !== null);

    if (!state.initialized) {
        state.initialized = true;
        state.maxTs = items.length ? Math.max(...items.map(i => i.timestamp ?? 0)) : 0;
        return toFeed(items);
    }

    const newItems = items.filter(item => (item.timestamp ?? 0) > state.maxTs);
    if (newItems.length) {
        state.maxTs = Math.max(...newItems.map(i => i.timestamp ?? 0));
    }
    return mqttLive ? [] : toFeed(newItems);
}

// ── Live MQTT event payloads → feed rows ───────────────────────────────────
// Real-time counterpart of the `log_feed` replay above (which maps the same
// events from the server's stored shape). NOTE: the `alert` feed kind here
// differs from FEED_MAPPERS.alert — see alertFeedType.

/** Feed row for a heartbeat tick. */
export function heartbeatFeedItem(p: HeartbeatPayload): FeedItem {
    return { type: "heartbeat", label: "heartbeat", agentName: p.agentName, timestamp: p.timestampMs };
}

/** Display agent type — falls back to "agent" when the normaliser yields "". */
export function spawnTypeLabel(p: SpawnPayload): string {
    return p.agentType || "agent";
}

/** Feed row for a spawn. */
export function spawnFeedItem(p: SpawnPayload): FeedItem {
    return {
        type: "spawn",
        label: `spawned (${spawnTypeLabel(p)})`,
        agentName: p.agentName,
        timestamp: p.timestampMs,
    };
}

/** Feed kind for a live alert: only "error" → error row; everything else → warning. */
export function alertFeedType(severity: AlertPayload["severity"]): "alert-error" | "alert-warning" {
    return severity === "error" ? "alert-error" : "alert-warning";
}

/** Toast kind for a live alert: "error" AND "critical" → error; else warning. */
export function alertToastType(severity: AlertPayload["severity"]): "alert-error" | "alert-warning" {
    return severity === "error" || severity === "critical" ? "alert-error" : "alert-warning";
}

/** Feed row for a live alert (message/agent default to ""/"system"). */
export function alertFeedItem(p: AlertPayload): FeedItem {
    return {
        type: alertFeedType(p.severity),
        label: p.message ?? "",
        agentName: p.agentName ?? "system",
        timestamp: p.timestampMs,
    };
}

/** Feed row for a chat message (agent→user or any). */
export function chatFeedItem(msg: ChatMessage): FeedItem {
    return {
        type: "chat",
        label: `→ ${msg.to}: ${msg.content}`,
        agentName: msg.from,
        timestamp: msg.timestampMs,
    };
}

/** Feed row when an agent reports stopped. */
export function stoppedFeedItem(p: StatusPayload, now = Date.now()): FeedItem {
    return { type: "stopped", label: "stopped", agentName: p.agentName, timestamp: now };
}

/** Feed row for a QA safety flag. */
export function qaFlagFeedItem(p: QaFlagPayload): FeedItem {
    return {
        type: "qa-flag",
        label: `[${p.category}] ${p.excerpt}`,
        agentName: `qa-agent ← ${p.from}`,
        timestamp: p.timestampMs,
    };
}

/** Text of a log entry, preferring `message` then `text` (empty when neither). */
export function logMessage(p: LogPayload): string {
    return p.message ?? p.text ?? "";
}

/** Feed row for a non-empty log entry, or null when there's nothing to show. */
export function logFeedItem(p: LogPayload, now = Date.now()): FeedItem | null {
    const msg = logMessage(p);
    return msg ? { type: "chat", label: msg, agentName: p.agentName, timestamp: now } : null;
}

/** Feed row for a completed task. */
export function completedFeedItem(p: { agentName: string }, now = Date.now()): FeedItem {
    return { type: "spawn", label: "task completed", agentName: p.agentName, timestamp: now };
}

/** Feed row for a remote node phoning home (agent count pluralised). */
export function nodeHeartbeatFeedItem(p: NodeHeartbeatPayload, now = Date.now()): FeedItem {
    const n = p.agents.length;
    return {
        type: "health",
        label: `node online · ${n} agent${n !== 1 ? "s" : ""}`,
        agentName: p.node,
        timestamp: now,
    };
}
