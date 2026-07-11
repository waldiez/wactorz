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
