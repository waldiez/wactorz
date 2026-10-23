/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Reading the metrics history and the node listing from the server.
 *
 * Every reader answers null when the server cannot say — not running yet, no
 * database to keep a history in, a node manager absent — so a caller draws
 * nothing rather than an error where a trend would be.
 */
import type { Sample } from "./trend";

/** One stored sample of an agent or a node: its time and whatever fields were kept. */
export type HistoryRow = Record<string, unknown> & { ts: number };

/** What a node's listing entry carries that the dashboard reads. */
export interface NodeListing {
    /** The node's name. */
    node: string;
    /** Whether main has heard from it lately. */
    online: boolean;
    /** Seconds since the epoch it was last heard from. */
    last_seen?: number;
    /** What its machine is, or null from a node that has not said. */
    manifest?: Record<string, unknown> | null;
    /** Its latest readings, under the names its heartbeat uses. */
    [reading: string]: unknown;
}

function base(): string {
    return window.__WACTORZ_INGRESS_PATH ?? "";
}

async function getJson(path: string): Promise<Record<string, unknown> | null> {
    try {
        const res = await fetch(`${base()}${path}`);
        if (!res.ok) {
            return null;
        }
        const body: unknown = await res.json();
        return body && typeof body === "object" ? (body as Record<string, unknown>) : null;
    } catch {
        return null;
    }
}

/** One agent's or node's samples over the last ``hours``, oldest first. */
export async function fetchHistory(
    kind: "agents" | "nodes",
    name: string,
    hours: number,
): Promise<HistoryRow[] | null> {
    const body = await getJson(`/api/history/${kind}/${encodeURIComponent(name)}?hours=${hours}`);
    const samples = body?.["samples"];
    return Array.isArray(samples) ? (samples as HistoryRow[]) : null;
}

/** One field of every agent's samples over the last ``hours``, by agent name. */
export async function fetchAgentsField(field: string, hours: number): Promise<Map<string, Sample[]> | null> {
    const body = await getJson(`/api/history/agents?field=${encodeURIComponent(field)}&hours=${hours}`);
    const agents = body?.["agents"];
    if (!agents || typeof agents !== "object") {
        return null;
    }
    const found = new Map<string, Sample[]>();
    for (const [name, rows] of Object.entries(agents as Record<string, unknown>)) {
        if (Array.isArray(rows)) {
            found.set(name, rows as Sample[]);
        }
    }
    return found;
}

/** Every node main knows, with its readings and its manifest. */
export async function fetchNodes(): Promise<NodeListing[] | null> {
    const body = await getJson("/api/nodes");
    const nodes = body?.["nodes"];
    return Array.isArray(nodes) ? (nodes as NodeListing[]) : null;
}
