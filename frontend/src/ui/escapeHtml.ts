/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * HTML-escape arbitrary text before interpolating it into an `innerHTML`
 * template. Used for values that may contain markup characters — e.g. uploaded
 * attachment filenames (`a<b.png`) — so they render literally instead of being
 * parsed as HTML.
 */
const ENTITIES: Record<string, string> = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
};

export function escapeHtml(value: unknown): string {
    return String(value ?? "").replace(/[&<>"']/g, c => ENTITIES[c] ?? c);
}
