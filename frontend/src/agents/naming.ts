/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Canonical agent-name resolution — the single source of truth shared by the
 * agent-store wiring (main.ts), the dashboard and the MQTT normalisers.
 *
 * Agent ids are WID strings of the form `<timestamp>Z-<name>[-<6 hex>]`; the
 * human-facing name is the `<name>` segment.
 */

/**
 * The orchestrator's registered name, and the default chat target.
 *
 * Mirrors the backend's `MAIN_ACTOR_NAME`; the two must agree, since routing is
 * by `@name`. It was written out at a dozen call sites across the transport and
 * the dashboard, which is how the old `"main-actor"` spelling survived in some
 * of them long after the backend stopped using it.
 */
export const MAIN_AGENT: string = "main";

/** Extract the embedded `<name>` from a WID id, or return the input unchanged. */
export function nameFromWid(raw: string | undefined): string {
    if (!raw) {
        return "";
    }
    const m = raw.match(/Z-(.+?)(?:-[0-9a-f]{6})?$/i);
    return m?.[1] ?? raw;
}

/** True when a "name" is really an unresolved backend id. The backend uses UUID
 *  agent ids (not WIDs), so an agent that never got a friendly name keeps the raw
 *  id as its name — which must never be shown as a label or used as a chat target. */
export function looksLikeAgentId(name: string | undefined): boolean {
    return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-/i.test(name ?? "");
}

/** Resolve a WID/name to a name and shorten a bare UUID (no friendly name) to
 *  its first segment so the UI never shows a full 36-char id. */
export function displayName(raw: string | undefined): string {
    const name = nameFromWid(raw);
    return looksLikeAgentId(name) ? name.slice(0, 8) : name;
}

/**
 * Resolve a human-readable display name from whatever the backend sends.
 * - If name is empty or looks like a raw timestamp (all digits), extract from the WID id.
 * - If name is itself a WID string (contains "Z-"), extract the embedded name.
 * - Otherwise trust the name as-is.
 */
export function resolveAgentName(name: string | undefined, id: string): string {
    const n = (name ?? "").trim();
    const isTimestampOnly = !n || /^\d+$/.test(n);
    return isTimestampOnly ? nameFromWid(id) : nameFromWid(n);
}
