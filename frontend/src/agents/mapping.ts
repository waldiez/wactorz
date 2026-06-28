/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Pure mappers from server payloads to UI models.
 *
 * - `toAgentInfo`  : a WS state-patch agent → the scene's {@link AgentInfo}.
 * - `mapLogFeedItem`: a WS `log_feed` entry → an {@link FeedItem} (or null to drop).
 *
 * Kept free of DOM / side effects so they are trivially unit-testable.
 */
import type { AgentInfo, AgentState } from "../types/agent";
import type { StatePatchAgent, LogFeedItem } from "../io/WSChatClient";
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
 * spawn event is available to populate the scene.
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
 * live scene. Falls back to the id-derived name only when nothing else resolves.
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
