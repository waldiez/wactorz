/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Shared agent-state helpers for the dashboard: messageability rules, state
 * colour/label mapping and relative-time formatting.
 */
import type { AgentState } from "../../types/agent";
import { MAIN_AGENT } from "../../agents/naming";

/** Heartbeat age (ms) after which a remote node / agent is treated as stale. */
export const STALE_MS = 180_000;

/** System agents that exist but cannot be chatted with directly. */
export const SYSTEM_AGENT_NAMES: Set<string> = new Set([
    "monitor-agent",
    "home-assistant-state-bridge",
    "home-assistant-map-agent",
]);

/** The pinned-first messageable agents, in display order. Single source for
 *  both the target picker (DashboardChat) and the messageability check below —
 *  two copies of this list used to live in two files and could drift. */
export const MESSAGEABLE_PRIORITY: readonly string[] = [MAIN_AGENT, "home-assistant-agent", "catalog"];

/** Agents that are always messageable even when flagged protected. */
const ALWAYS_MESSAGEABLE: ReadonlySet<string> = new Set(MESSAGEABLE_PRIORITY);

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
 * Whether a message sent right now would reach this agent.
 *
 * Deliberately separate from `canDirectMessage`, which asks whether the user may
 * address the agent at all — an identity and policy question that knows nothing
 * about state. A stopped or failed agent stays in the list and stays the user's
 * choice; it simply cannot answer until it is running again, so the send says so
 * instead of quietly going somewhere else.
 *
 * `paused` and `initializing` pass: both are transient, and a false block during
 * normal startup would be worse than the problem this solves.
 */
export function isReachable(agent: { state: AgentState }): boolean {
    return !(typeof agent.state === "object" || agent.state === "stopped");
}

/**
 * Names of the agents the user may directly message. Single source for both the
 * target `<select>` and the `@mention` suggestions, so a mention can never offer
 * an agent the picker can't target (which would silently fail to switch target).
 */
export function messageableNames(agents: Iterable<{ name: string; protected?: boolean }>): string[] {
    return [...agents]
        .filter(canDirectMessage)
        .map(a => a.name)
        .filter(Boolean);
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
    return state;
}

/** Agents sorted with main pinned first, then alphabetical. */
export function sortAgents<T extends { name: string }>(agents: Iterable<T>): T[] {
    return [...agents].sort((a, b) => {
        if (a.name === MAIN_AGENT) {
            return -1;
        }
        if (b.name === MAIN_AGENT) {
            return 1;
        }
        return a.name.localeCompare(b.name);
    });
}

/** Compact relative time like "now", "12s ago", "3m ago", "2h ago", "5d ago". */
export function relTime(ms: number): string {
    const s = Math.round((Date.now() - ms) / 1000);
    if (s < 5) {
        return "now";
    }
    if (s < 60) {
        return `${s}s ago`;
    }
    if (s < 3600) {
        return `${Math.floor(s / 60)}m ago`;
    }
    if (s < 86400) {
        return `${Math.floor(s / 3600)}h ago`;
    }
    return `${Math.floor(s / 86400)}d ago`;
}
