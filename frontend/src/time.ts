/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/** Convert a raw epoch value to milliseconds.
 *  Python's time.time() returns seconds (< 1e10); JS Date.now() returns ms. */
export function toMs(raw: unknown): number {
    const n = Number(raw);
    if (!Number.isFinite(n) || n <= 0) {
        return Date.now();
    }
    return n < 1e10 ? n * 1000 : n;
}

/** Whether two instants fall on the same calendar day, in the viewer's zone. */
function sameDay(a: Date, b: Date): boolean {
    return (
        a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
    );
}

/**
 * A timestamp a reader can place without doing arithmetic.
 *
 * Both surfaces that show one are capped by count rather than by age — the chat
 * thread keeps the last 500 messages, the activity feed the last N rows — so a
 * dashboard left open over a weekend shows Friday and this morning stamped
 * identically. Which is worst exactly when it matters: taking last week's answer
 * for a fresh one.
 *
 * Only as much date as it takes to disambiguate: nothing extra for today, a word
 * for yesterday, a date beyond that, and the year only when it is not this one.
 *
 * Local throughout, and correct because the wire carries epoch values — `toMs`
 * above is the single funnel — so this renders the viewer's own clock rather
 * than the server's. `now` is injectable so the rules can be tested without
 * pinning one; `seconds` is for the feed, where a log row's precision is worth
 * the width and a chat bubble's is not.
 */
export function timeLabel(
    ms: number,
    { now = Date.now(), seconds = false }: { now?: number; seconds?: boolean } = {},
): string {
    const at = new Date(ms);
    const clock = at.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        ...(seconds ? { second: "2-digit" as const } : {}),
    });
    const today = new Date(now);
    if (sameDay(at, today)) {
        return clock;
    }
    const yesterday = new Date(now);
    yesterday.setDate(yesterday.getDate() - 1);
    if (sameDay(at, yesterday)) {
        return `Yesterday ${clock}`;
    }
    const options: Intl.DateTimeFormatOptions =
        at.getFullYear() === today.getFullYear()
            ? { day: "numeric", month: "short" }
            : { day: "numeric", month: "short", year: "numeric" };
    return `${at.toLocaleDateString([], options)} ${clock}`;
}
