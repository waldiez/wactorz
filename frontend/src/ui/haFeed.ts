/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Maps Home Assistant entity state-changes onto activity-feed items.
 *
 * Two transports deliver the same events (the direct HA WebSocket and the
 * MQTT ha-state-bridge), so the pusher de-duplicates identical entity+state
 * pairs seen within a short window and ignores noisy/non-actionable states.
 */
import type { FeedItem } from "../types/feed";

/** Domains that produce meaningful on/off-style states worth showing in the feed. */
const HA_FEED_DOMAINS = new Set([
    "switch",
    "light",
    "fan",
    "input_boolean",
    "climate",
    "cover",
    "media_player",
    "vacuum",
    "humidifier",
    "lock",
    "alarm_control_panel",
]);

/** Suppress duplicate entity+state events seen within this window (ms). */
const DEDUP_WINDOW_MS = 5000;

/**
 * Build a pusher that filters + de-duplicates HA state-changes and forwards
 * the survivors to `push`. State is private to each returned pusher.
 */
export function createHaFeedPusher(
    push: (item: FeedItem) => void,
): (entityId: string, state: string, friendlyName: string) => void {
    const recent = new Map<string, number>();

    return (entityId, state, friendlyName) => {
        const domain = entityId.split(".")[0] ?? "";
        if (!HA_FEED_DOMAINS.has(domain)) {
            return;
        }
        // Skip raw numeric states (sensors leaking through) and "unknown" spam.
        if (/^\d/.test(state) || state === "unknown") {
            return;
        }

        const key = `${entityId}:${state}`;
        const now = Date.now();
        if (now - (recent.get(key) ?? 0) < DEDUP_WINDOW_MS) {
            return;
        }
        recent.set(key, now);
        push({ type: "health", label: `${friendlyName} → ${state}`, agentName: "ha", timestamp: now });
    };
}

/** A parsed HA state-change from a raw `ha/...` MQTT message. */
export interface HaRawEvent {
    entityId: string;
    state: string;
    friendlyName: string;
}

/**
 * Parse a raw `ha/state/{domain}/{entity_id}` MQTT message (the ha-state-bridge
 * transport) into an HA event, or null when the topic isn't an HA state topic or
 * carries no state. The entity id falls back to the last two topic segments; the
 * friendly name to the entity id.
 */
export function parseHaRawEvent(topic: string, payload: unknown): HaRawEvent | null {
    if (!topic.startsWith("ha/")) {
        return null;
    }
    const p = (payload ?? {}) as Record<string, unknown>;
    const entityId = (p["entity_id"] as string | undefined) ?? topic.split("/").slice(-2).join(".");
    const newState = p["new_state"] as Record<string, unknown> | undefined;
    const state = (newState?.["state"] as string | undefined) ?? "";
    const attrs = newState?.["attributes"] as Record<string, unknown> | undefined;
    const friendlyName = (attrs?.["friendly_name"] as string | undefined) ?? entityId;
    return state ? { entityId, state, friendlyName } : null;
}
