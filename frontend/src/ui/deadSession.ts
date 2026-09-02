/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */

/** Whether the terminal ingress-session notice is already on screen. */
let shown = false;

/** Show the recovery instructions when a Home Assistant ingress session is gone. */
export function showDeadSession(target: Document = document): boolean {
    if (shown) {
        return false;
    }
    shown = true;
    const overlay = target.createElement("div");
    overlay.className = "af-dead-session";
    overlay.setAttribute("role", "alertdialog");
    overlay.setAttribute("aria-modal", "true");

    const card = target.createElement("section");
    card.className = "af-dead-session-card";
    const title = target.createElement("h2");
    title.textContent = "This page has lost its connection to Home Assistant";
    const body = target.createElement("p");
    body.textContent =
        "Its link into Home Assistant is no longer valid, so nothing here will update and anything you send will not arrive.";
    const how = target.createElement("p");
    how.className = "af-dead-session-how";
    how.textContent = "Close this tab and open Wactorz again from the Home Assistant sidebar.";
    card.append(title, body, how);
    overlay.append(card);
    target.body.append(overlay);
    return true;
}

/** Reset module state for an isolated test document. */
export function resetDeadSession(): void {
    shown = false;
}
