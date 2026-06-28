/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Shared agent-state helpers for the dashboard: messageability rules, state
 * colour/label mapping and relative-time formatting.
 */
import type { AgentInfo, AgentState } from "../../types/agent";
import { looksLikeAgentId } from "../../agents/naming";

/** System agents that exist but cannot be chatted with directly. */
export const SYSTEM_AGENT_NAMES: Set<string> = new Set([
    "io-agent",
    "monitor-agent",
    "home-assistant-state-bridge",
    "home-assistant-map-agent",
]);

/** Agents that are always messageable even when flagged protected. */
const ALWAYS_MESSAGEABLE = new Set(["main", "main-actor", "home-assistant-agent", "catalog"]);

/** Whether the user may send chat messages directly to this agent. */
export function canDirectMessage(agent: { name: string; protected?: boolean }): boolean {
    if (ALWAYS_MESSAGEABLE.has(agent.name)) {
        return true;
    }
    if (SYSTEM_AGENT_NAMES.has(agent.name)) {
        return false;
    }
    return !agent.protected;
}

/**
 * Pick the default chat target for the current agent set: keep `current` if it's
 * still messageable, else prefer main, else the first human-named agent. Never
 * auto-selects an id-named agent — the backend uses UUID ids (not WIDs), so an
 * agent that never resolved keeps the id as its name and must not silently become
 * the chat target (it leaks into the placeholder). The user can still pick one.
 */
export function pickChatTarget(agents: AgentInfo[], current: string): string {
    const messageable = agents.filter(canDirectMessage);
    if (!messageable.length || messageable.some(a => a.name === current)) {
        return current;
    }
    const main = messageable.find(a => a.name === "main" || a.name === "main-actor");
    const named = messageable
        .filter(a => !looksLikeAgentId(a.name))
        .sort((a, b) => a.name.localeCompare(b.name));
    return main?.name ?? named[0]?.name ?? current;
}

/** Accent colour for an agent state (object state = failed/red). */
export function stateColor(state: AgentState): string {
    if (typeof state === "object") {
        return "#f87171";
    }
    switch (state as string) {
        case "running":
            return "#34d399";
        case "paused":
            return "#fbbf24";
        case "initializing":
            return "#60a5fa";
        case "stopped":
            return "#4b5563";
        default:
            return "#34d399";
    }
}

/** Human label for an agent state (object state = "failed"). */
export function stateLabel(state: AgentState): string {
    if (typeof state === "object") {
        return "failed";
    }
    return state as string;
}

/** Agents sorted with main-actor pinned first, then alphabetical. */
export function sortAgents<T extends { name: string }>(agents: Iterable<T>): T[] {
    return [...agents].sort((a, b) => {
        if (a.name === "main-actor") {
            return -1;
        }
        if (b.name === "main-actor") {
            return 1;
        }
        return a.name.localeCompare(b.name);
    });
}

/** Compact relative time like "now", "12s ago", "3m ago". */
export function relTime(ms: number): string {
    const s = Math.round((Date.now() - ms) / 1000);
    if (s < 5) {
        return "now";
    }
    if (s < 60) {
        return `${s}s ago`;
    }
    return `${Math.floor(s / 60)}m ago`;
}
