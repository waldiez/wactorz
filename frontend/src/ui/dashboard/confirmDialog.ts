/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Modal confirmation for an action that cannot be undone.
 *
 * The platform's own `confirm()` is not usable here: it is drawn outside the
 * page, so it carries none of the dashboard's styling, and it blocks the whole
 * event loop while it is open — every timer, socket frame and repaint waits on
 * the user. This is a normal overlay that resolves a promise instead.
 *
 * It claims `role="dialog"` and `aria-modal`, which is a promise to keyboard and
 * screen-reader users that focus is inside and stays there. So focus is moved in
 * on open, Tab is cycled within, and the opener gets focus back on close.
 *
 * Cancel is the default: it takes focus, and Escape or a click on the backdrop
 * chooses it. Nothing destructive happens without the user aiming at it.
 */

import { button, el } from "../dom";

/** What to ask, and what to call the button that agrees to it. */
export interface ConfirmOptions {
    /** The question, in a few words — "Delete agent?". */
    title: string;
    /** The consequence in full, including whatever it names. Rendered as text. */
    message: string;
    /** Label for the confirming button. Say the verb, not "OK". */
    confirmLabel: string;
}

/** Elements inside the dialog that can hold focus, in tab order. */
function focusables(root: HTMLElement): HTMLElement[] {
    return [...root.querySelectorAll<HTMLElement>("button, [href], [tabindex]:not([tabindex='-1'])")];
}

/** The overlay and its two buttons, assembled but not yet wired. */
function buildOverlay(opts: ConfirmOptions): {
    overlay: HTMLElement;
    confirmBtn: HTMLButtonElement;
    cancelBtn: HTMLButtonElement;
} {
    const overlay = el("div", "af-confirm-backdrop");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", opts.title);

    // Dialog buttons wear the same chrome as a card's own controls. The title,
    // message and label are set as text: an agent is named by whoever spawned
    // it, and the name reaches the browser over MQTT.
    const cancelBtn = button("af-mini-btn af-confirm-cancel", "Cancel");
    const confirmBtn = button("af-mini-btn danger af-confirm-ok", opts.confirmLabel);
    const actions = el("div", "af-confirm-actions");
    actions.append(cancelBtn, confirmBtn);

    const box = el("div", "af-confirm");
    box.append(
        el("div", "af-confirm-title", opts.title),
        el("div", "af-confirm-message", opts.message),
        actions,
    );
    overlay.appendChild(box);
    return { overlay, confirmBtn, cancelBtn };
}

/**
 * Closes the instance that is currently open, if any.
 *
 * Dropping the overlay element alone is not enough: its `keydown` handler lives
 * on `document` and would outlive it, leaving a detached dialog still answering
 * Escape and Tab and competing with the live one for focus. It settles as a
 * cancel, so a caller awaiting the promise is never left waiting.
 */
let closeActive: (() => void) | null = null;

/**
 * Ask the question and resolve with the answer: `true` only if the user pressed
 * the confirming button, `false` for Cancel, Escape, a click outside the box,
 * or a second dialog opening over this one.
 */
export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
    closeActive?.();
    const { overlay, confirmBtn, cancelBtn } = buildOverlay(opts);

    // Whatever had focus when this opened is where focus belongs afterwards.
    const opener = document.activeElement as HTMLElement | null;

    return new Promise<boolean>(resolve => {
        const settle = (answer: boolean): void => {
            overlay.remove();
            document.removeEventListener("keydown", onKey);
            closeActive = null;
            opener?.focus?.();
            resolve(answer);
        };

        const trapTab = (e: KeyboardEvent): void => {
            const items = focusables(overlay);
            if (!items.length) {
                return;
            }
            e.preventDefault();
            const current = items.indexOf(document.activeElement as HTMLElement);
            const step = e.shiftKey ? -1 : 1;
            // -1 (focus outside the dialog) lands on the first item going forward
            // and the last going backward, which is where a trap should put it.
            const next = (current + step + items.length) % items.length;
            items[next]!.focus();
        };

        const onKey = (e: KeyboardEvent): void => {
            if (e.key === "Escape") {
                settle(false);
            } else if (e.key === "Tab") {
                trapTab(e);
            }
        };

        // Only the backdrop dismisses: a click inside the box would otherwise
        // bubble here and cancel the dialog the user is still reading.
        overlay.addEventListener("click", e => {
            if (e.target === overlay) {
                settle(false);
            }
        });
        cancelBtn.addEventListener("click", () => settle(false));
        confirmBtn.addEventListener("click", () => settle(true));
        document.addEventListener("keydown", onKey);
        closeActive = () => settle(false);
        document.body.appendChild(overlay);
        cancelBtn.focus();
    });
}
