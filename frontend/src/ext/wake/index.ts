/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Wake extension — frontend module.
 *
 * Mirrors ``wactorz/ext/wake/`` on the backend. Nothing here listens: the phrase
 * is heard by the machine, and a browser is not involved in a turn a room starts.
 * What the page needs is only what to say about it — whether it is on, whether
 * the model it needs is there, and which phrase wakes it.
 */

import { registerConfigEntry } from "../../config/serverConfig";
import { safeStorage } from "../../safeStorage";

/** Where the seeded state is kept. */
export const WAKE_KEY = "wactorz-wake-mode";
export const WAKE_READY_KEY = "wactorz-wake-ready";
export const WAKE_PHRASES_KEY = "wactorz-wake-phrases";

// This extension's /api/config fields, namespaced under "wake" by the backend
// seam — registered at module load so seedServerConfig() picks them up.
const wake = (c: Record<string, unknown>): Record<string, unknown> | undefined =>
    c["wake"] as Record<string, unknown> | undefined;

registerConfigEntry(WAKE_KEY, c => wake(c)?.["mode"] as string | undefined);
registerConfigEntry(WAKE_READY_KEY, c => (wake(c)?.["ready"] ? "1" : "0"));
registerConfigEntry(WAKE_PHRASES_KEY, c => ((wake(c)?.["phrases"] as string[]) ?? []).join(", "));

/** Whether a phrase starts a turn on the machine. */
export function waking(): boolean {
    return safeStorage.get(WAKE_KEY) === "on";
}

/**
 * Whether the deployment has what it needs to wake.
 *
 * The model is weights fetched at deploy time rather than shipped with the code,
 * so a deployment can be set to wake and have nothing to wake with. Saying so is
 * better than a switch that turns on and does nothing.
 */
export function wakeReady(): boolean {
    return safeStorage.get(WAKE_READY_KEY) === "1";
}

/** The phrases that wake it, as configured. */
export function wakePhrases(): string {
    return safeStorage.get(WAKE_PHRASES_KEY) ?? "";
}
