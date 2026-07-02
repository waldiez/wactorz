/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * localStorage access that never throws.
 *
 * `localStorage` raises in Safari private mode, when storage is disabled, or on
 * quota-exceeded. A throw at module top level (e.g. `main.ts`) aborts bootstrap
 * with a blank page, so these wrappers swallow those errors: reads return `null`
 * and writes become no-ops, degrading persistence quietly instead of breaking
 * the app.
 */
export const safeStorage = {
    /** Read a key, or `null` if it is absent or storage is unavailable. */
    get(key: string): string | null {
        try {
            return localStorage.getItem(key);
        } catch {
            return null;
        }
    },
    /** Persist a key, silently ignoring an unavailable or full store. */
    set(key: string, value: string): void {
        try {
            localStorage.setItem(key, value);
        } catch {
            /* storage unavailable — persistence is best-effort */
        }
    },
    /** Remove a key, silently ignoring an unavailable store. */
    remove(key: string): void {
        try {
            localStorage.removeItem(key);
        } catch {
            /* storage unavailable */
        }
    },
};
