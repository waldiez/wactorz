/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Maps Home Assistant entity state-changes onto activity-feed items.
 *
 * The sole transport is the MQTT ha-state-bridge (`homeassistant/state_changes`).
 * The pusher de-duplicates identical entity+state pairs seen within a short
 * window and drops what is noise rather than activity.
 *
 * Noise is named, and everything else is shown. Naming what to *keep* instead
 * would mean a domain Home Assistant adds later, or one this list never thought
 * of, is absent with nothing to indicate it: the feed looks quiet and is in fact
 * blind. Noise can at least be seen and named; silence cannot.
 */
import type { FeedItem } from "../types/feed";

/**
 * Domains whose changes are readings or bookkeeping rather than things that
 * happened. A house has far more of these than of anything worth a row, and
 * they change constantly.
 */
const HA_NOISE_DOMAINS = new Set([
    "sensor",
    "number",
    "counter",
    "input_number",
    "input_text",
    "select",
    "input_select",
    // Values someone set, not moments something happened. Their states are
    // dates and times, which read as an event and are not one.
    "input_datetime",
    "datetime",
    "date",
    "time",
    "update",
    "zone",
    "sun",
    "weather",
    "conversation",
    "stt",
    "tts",
    "todo",
    "image",
    "camera",
]);

/**
 * Domains whose state is the moment the thing happened rather than a condition.
 * Their state is a timestamp, which says nothing worth reading aloud in a feed,
 * so the row is written from the domain instead.
 */
const HA_ACTIVATION_VERBS: Record<string, string> = {
    button: "pressed",
    input_button: "pressed",
    event: "fired",
    scene: "activated",
};

/** A plain number, which is a reading. An ISO timestamp is not one of these. */
const A_READING = /^-?\d+([.,]\d+)?$/;

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
        // Every Home Assistant entity is `domain.name`. Something without one
        // is not an entity, and showing it under a denylist would mean showing
        // whatever malformed thing arrived.
        const [domain, name] = entityId.split(".");
        if (!domain || !name) {
            return;
        }
        if (HA_NOISE_DOMAINS.has(domain)) {
            return;
        }
        // A reading is a measurement, not an event, wherever it comes from. Only
        // plain numbers: an activation's state is a timestamp, which starts with
        // a digit and is very much worth showing.
        if (A_READING.test(state)) {
            return;
        }
        // Nothing to say about a thing whose state nobody knows, and both of
        // these arrive in bulk when an integration reloads.
        if (state === "unknown" || state === "unavailable") {
            return;
        }

        const verb = HA_ACTIVATION_VERBS[domain];
        const label = verb ? `${friendlyName} ${verb}` : `${friendlyName} → ${state}`;
        // Keyed on the state for a condition, and on the moment for an
        // activation: pressing the same button twice is two events, where a
        // light reporting "on" twice in a second is one.
        const key = `${entityId}:${state}`;
        const now = Date.now();
        // Evict entries past the dedup window so `recent` can't grow unbounded
        // over a long session in a large HA install.
        for (const [k, t] of recent) {
            if (now - t >= DEDUP_WINDOW_MS) {
                recent.delete(k);
            }
        }
        if (now - (recent.get(key) ?? 0) < DEDUP_WINDOW_MS) {
            return;
        }
        recent.set(key, now);
        push({ type: "health", label, agentName: "ha", timestamp: now });
    };
}

/** A parsed HA state-change from a raw `homeassistant/state_changes/...` MQTT message. */
export interface HaRawEvent {
    entityId: string;
    state: string;
    friendlyName: string;
}

/**
 * Parse a raw `homeassistant/state_changes[/{domain}/{entity_id}]` MQTT message
 * (the ha-state-bridge transport) into an HA event, or null when the topic isn't
 * an HA state topic or carries no state. The entity id falls back to the last two
 * topic segments (per-entity form); the friendly name to the entity id.
 */
export function parseHaRawEvent(topic: string, payload: unknown): HaRawEvent | null {
    if (!topic.startsWith("homeassistant/state_changes")) {
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
