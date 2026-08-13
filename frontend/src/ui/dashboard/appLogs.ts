/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Fetching application-log records from `GET /api/logs`.
 *
 * A pull, not a subscription: the view asks when it needs them. Live streaming
 * is deliberately not here — pushing log records to every open page is a wider
 * exposure than serving them to a caller who asked, and it waits on the
 * handshake work.
 */
import type { AppLogItem, LogLevel } from "../../types/feed";

const LEVELS: readonly string[] = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

function ingressBase(): string {
    return window.__WACTORZ_INGRESS_PATH ?? "";
}

/**
 * One record, or null if the server sent something this view cannot draw.
 *
 * Validated rather than cast: the envelope is checked at the boundary so a
 * malformed entry is dropped here instead of becoming `undefined` in the middle
 * of rendering a row.
 */
function toEntry(raw: unknown): AppLogItem | null {
    if (!raw || typeof raw !== "object") {
        return null;
    }
    const e = raw as Record<string, unknown>;
    if (typeof e["ts"] !== "number" || typeof e["text"] !== "string") {
        return null;
    }
    const level = typeof e["level"] === "string" ? e["level"] : "INFO";
    return {
        source: "app",
        ts: e["ts"],
        // An unrecognised level becomes INFO. Python allows custom levels, so
        // a `NOTICE` or `TRACE` can reach us — and passing it through would make
        // the severity filter unpredictable, since a level with no rank cannot
        // be compared to the one the user chose. INFO is the honest default for
        // something a library logged without saying how severe it is.
        level: (LEVELS.includes(level) ? level : "INFO") as LogLevel,
        origin: typeof e["origin"] === "string" ? e["origin"] : "?",
        text: e["text"],
    };
}

/** Recent application-log records, oldest first. Null when the fetch failed,
 *  so the caller can tell "nothing logged" from "could not ask". */
export async function fetchAppLogs(limit = 200): Promise<AppLogItem[] | null> {
    try {
        const res = await fetch(`${ingressBase()}/api/logs?limit=${encodeURIComponent(String(limit))}`);
        if (!res.ok) {
            return null;
        }
        const body = (await res.json()) as { entries?: unknown };
        if (!Array.isArray(body.entries)) {
            return null;
        }
        return body.entries.map(toEntry).filter((e): e is AppLogItem => e !== null);
    } catch {
        return null;
    }
}
