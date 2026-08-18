/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/** Shared activity-feed event types, consumed by the cards feed view. */
export type FeedEventType =
    "spawn" | "heartbeat" | "chat" | "alert-error" | "alert-warning" | "stopped" | "health" | "qa-flag";

export interface FeedItem {
    type: FeedEventType;
    label: string;
    agentName: string;
    timestamp: number;
    /** Speaker role for chat rows ("user" | "assistant"); absent for other types. */
    role?: string | undefined;
}

/** Severity of an application-log row, lowest first. Agent rows have none. */
export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";

/**
 * One application-log record, as the server's ring buffer holds it.
 *
 * A sibling of `FeedItem` rather than a widening of it: the two describe
 * genuinely different things — an agent *event* has a type and an actor, a log
 * record has a severity and a logger — and collapsing them into one optional-
 * everything shape would make every field a question at each use.
 */
export interface AppLogItem {
    source: "app";
    /** Seconds since the epoch, as the buffer records it. */
    ts: number;
    level: LogLevel;
    /** The logger name, e.g. `wactorz.agents.installer`. */
    origin: string;
    text: string;
}

/** Anything the activity feed can show. Agent items carry no `source`. */
export type ActivityItem = FeedItem | AppLogItem;

/** Whether this entry came from the application log rather than agent activity. */
export function isAppLog(item: ActivityItem): item is AppLogItem {
    return (item as AppLogItem).source === "app";
}
