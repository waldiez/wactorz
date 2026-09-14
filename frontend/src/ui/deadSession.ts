/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Say so when this page can no longer reach Home Assistant.
 *
 * The dashboard is served under an ingress prefix that Home Assistant owns, and
 * a page outlives that prefix in more than one way. Removing the add-on revokes
 * it outright, so reinstalling, or switching between the `wactorz` and
 * `wactorz-ultra` variants, strands every tab opened beforehand. A tab can also
 * lose its footing while the add-on is perfectly healthy.
 *
 * Which of those happened is not something a status code settles, so this says
 * neither. It describes what the page can observe -- requests under this prefix
 * have stopped being answered -- and the one remedy common to every cause:
 * open the panel again, which starts from whatever is current.
 *
 * Nothing about the failure is otherwise visible. The page still renders and its
 * polls fail in the background with nobody reading the result, which is how
 * someone ends up concluding the add-on is broken.
 *
 * Deliberately terminal. Recovering a prefix is not something this page can do,
 * so offering a retry would promise something it cannot deliver.
 */

/** The one overlay, so repeated failures do not stack them. */
let shown = false;

/** What the overlay says: what happened, and the one thing that fixes it. */
function buildCard(target: Document): HTMLElement {
    const card = target.createElement("div");
    card.className = "af-dead-session-card";

    const title = target.createElement("h2");
    title.id = "af-dead-session-title";
    title.textContent = "This page has lost its connection to Home Assistant";

    const body = target.createElement("p");
    body.textContent =
        "Its link into Home Assistant is no longer valid, so nothing here will update and anything you send will not arrive.";

    const how = target.createElement("p");
    how.className = "af-dead-session-how";
    how.textContent = "Close this tab and open Wactorz again from the Home Assistant sidebar.";

    card.append(title, body, how);
    return card;
}

/** Show the overlay, once, and say whether this call is what showed it. */
export function showDeadSession(target: Document = document): boolean {
    if (shown) {
        return false;
    }
    shown = true;

    const overlay = target.createElement("div");
    overlay.className = "af-dead-session";
    overlay.setAttribute("role", "alertdialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "af-dead-session-title");

    overlay.appendChild(buildCard(target));
    target.body.appendChild(overlay);
    return true;
}

/** Let a test start from a page that has not shown it. */
export function resetDeadSession(): void {
    shown = false;
}
