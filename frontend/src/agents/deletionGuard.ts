/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Deletion guard for the live agent grid — mirrors the backend's
 * `wactorz/web/runtime.py::deleted_agent_ids`.
 *
 * When the user deletes an agent there's a brief "stop-window" where trailing
 * MQTT events (heartbeat / status / spawn) can still arrive and blink the card
 * back. We suppress those, but must re-admit a deliberate **re-spawn** — which
 * reuses the same agent id and so would otherwise stay suppressed forever.
 *
 * The discriminator is the message timestamp (same model as the backend): an
 * event strictly newer than the deletion (+ a small grace that absorbs an
 * in-flight heartbeat) is a re-spawn and re-admits the agent. Paths that carry
 * no timestamp (status / WS snapshot / REST) rely on the failsafe expiry so the
 * guard can never stick forever.
 */
export interface DeletionGuard {
    /** Mark `id` as just-deleted (starts the guard window). */
    // Arrow-property (not method) types: these are closures over the guard's map,
    // safe to destructure — the type shape tells eslint's unbound-method so.
    markDeleted: (id: string) => void;
    /** True while `id` should be suppressed. A `msgTs` newer than the deletion
     *  (+grace) re-admits it (and clears the entry); the failsafe also clears it. */
    isDeleted: (id: string, msgTs?: number) => boolean;
}

/**
 * Create a {@link DeletionGuard}. `graceMs` absorbs an in-flight heartbeat from
 * the stop-window before an event counts as a re-spawn; `failsafeMs` caps how
 * long a timestamp-less suppression can last; `now` is injectable for tests.
 */
export function createDeletionGuard(
    graceMs = 3_000, // absorb an in-flight heartbeat from the stop-window
    failsafeMs = 30_000, // hard ceiling for timestamp-less paths
    now: () => number = () => Date.now(),
): DeletionGuard {
    const deletedAt = new Map<string, number>();
    return {
        markDeleted(id) {
            deletedAt.set(id, now());
        },
        isDeleted(id, msgTs) {
            const at = deletedAt.get(id);
            if (at === undefined) {
                return false;
            }
            if (msgTs !== undefined && msgTs > at + graceMs) {
                deletedAt.delete(id); // re-spawn — its events are newer than the deletion
                return false;
            }
            if (now() - at > failsafeMs) {
                deletedAt.delete(id); // failsafe: never suppress forever
                return false;
            }
            return true;
        },
    };
}
