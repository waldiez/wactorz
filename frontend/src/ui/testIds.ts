/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The `data-testid` values the dashboard ships, in one list.
 *
 * Tests find an element by its role and accessible name where it has them, the
 * way a person using a screen reader finds it. A test id marks what has no such
 * name — a card's state line, a message's sender, the parts of a toast — so the
 * tests do not lean on class names, which belong to the styles and move with
 * them.
 *
 * The end-to-end harness (`e2e/harness/browser.py`) uses these same strings; a
 * unit test checks that every one it names is listed here.
 *
 * Nothing here imports from the rest of the UI, so `dom.ts` can type with it.
 */

/** Every fixed test id. */
export const TEST_IDS = [
    "connection",
    "agent-card",
    "agent-state",
    "node",
    "node-agents",
    "node-machine",
    "confirm-dialog",
    "confirm-title",
    "confirm-message",
    "confirm-ok",
    "confirm-cancel",
    "toast",
    "toast-title",
    "toast-kind",
    "toast-message",
    "chat-agent-list",
    "chat-title",
    "agent-message",
    "user-message",
    "message-from",
    "message-body",
    "message-waiting",
    "chat-option",
    "history-panel",
    "history-body",
] as const;

/** A view's root, by the view's key: built-in views and extension views alike. */
export type ViewTestId = `view-${string}`;

/** A value `testId()` accepts. */
export type TestId = (typeof TEST_IDS)[number] | ViewTestId;
