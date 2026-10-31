/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The activity feed's entries, apart from how they are drawn: when each happened,
 * what makes two the same, and which rows the toolbar lets through.
 */
import { isAppLog, type ActivityItem, type LogLevel } from "../../types/feed";

/**
 * When an entry happened, in milliseconds.
 *
 * The two sources disagree on units as well as field name: agent events carry
 * `timestamp` in milliseconds, the log buffer records `ts` in seconds, as
 * Python's `time.time()` returns it. Sorting them together without converting
 * puts every log row in 1970.
 */
export function entryTimeMs(item: ActivityItem): number {
    return isAppLog(item) ? item.ts * 1000 : item.timestamp;
}

/** Identity key for a feed entry — entries carry no id, so dedup on content. */
export function feedKey(item: ActivityItem): string {
    if (isAppLog(item)) {
        // Source-prefixed: an agent row and a log row could otherwise collide on
        // identical text at the same instant, and the survivor would be whichever
        // arrived first.
        return `app|${item.ts}|${item.level}|${item.origin}|${item.text}`;
    }
    return `agent|${item.timestamp}|${item.type}|${item.agentName}|${item.label}`;
}

/**
 * Entries arrive from several unsynchronised sources (SQLite seed, WS
 * log_feed replay, live MQTT/chat, and now the application log) in arrival
 * order, not chronological order, and without ids. Drop exact-duplicate
 * entries and sort by time so the rendered feed is stable and chronological
 * regardless of arrival order.
 */
export function dedupeAndSortFeed<T extends ActivityItem>(items: T[]): T[] {
    const seen = new Set<string>();
    const unique: T[] = [];
    for (const item of items) {
        const key = feedKey(item);
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        unique.push(item);
    }
    return unique.sort((a, b) => entryTimeMs(a) - entryTimeMs(b));
}

/** Which rows the toolbar is currently letting through. */
export interface FeedFilters {
    /** Which sources to show. Defaults to `agent`, so the panel keeps the
     *  character it had before the application log joined it. */
    source: "all" | "agent" | "app";
    /** Minimum severity for application-log rows; agent rows are unaffected. */
    level: LogLevel;
    /** Case-insensitive substring over the row's visible text. */
    search: string;
    hideHeartbeats: boolean;
    /** Only the rows of this chat turn; "" for every turn. Set from a row's turn chip. */
    turn: string;
    /** Only the rows of this agent; "" for every agent. Set from a row's agent name. */
    agent: string;
}

export const DEFAULT_FILTERS: FeedFilters = {
    source: "agent",
    level: "INFO",
    search: "",
    hideHeartbeats: true,
    turn: "",
    agent: "",
};
