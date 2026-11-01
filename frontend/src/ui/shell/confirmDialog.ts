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
 *
 * `confirmWithReason` asks the same, with a box for why, which the action
 * records beside itself.
 */

import { uid } from "../../ids";
import { button, dialog, el, testId, trapTab } from "../dom";

/** What to ask, and what to call the button that agrees to it. */
export interface ConfirmOptions {
    /** The question, in a few words — "Delete agent?". */
    title: string;
    /** The consequence in full, including whatever it names. Rendered as text. */
    message: string;
    /** Label for the confirming button. Say the verb, not "OK". */
    confirmLabel: string;
}

/** The longest reason the server keeps with an action: a sentence, not a document. */
export const MAX_REASON_CHARS = 500;

/** The overlay, its two buttons and, when asked for, the box for a reason; assembled but not wired. */
function buildOverlay(
    opts: ConfirmOptions,
    asksWhy: boolean,
): {
    overlay: HTMLElement;
    confirmBtn: HTMLButtonElement;
    cancelBtn: HTMLButtonElement;
    reason: HTMLTextAreaElement | null;
} {
    const overlay = testId(dialog(el("div", "af-confirm-backdrop"), opts.title), "confirm-dialog");

    // Dialog buttons wear the same chrome as a card's own controls. The title,
    // message and label are set as text: an agent is named by whoever spawned
    // it, and the name reaches the browser over MQTT.
    const cancelBtn = testId(button("af-mini-btn af-confirm-cancel", "Cancel"), "confirm-cancel");
    const confirmBtn = testId(button("af-mini-btn danger af-confirm-ok", opts.confirmLabel), "confirm-ok");
    const actions = el("div", "af-confirm-actions");
    actions.append(cancelBtn, confirmBtn);

    // The message is the dialog's description, read after its name when it opens.
    const message = testId(el("div", "af-confirm-message", opts.message), "confirm-message");
    message.id = uid("af-confirm-message");
    overlay.setAttribute("aria-describedby", message.id);

    const box = el("div", "af-confirm");
    box.append(testId(el("div", "af-confirm-title", opts.title), "confirm-title"), message);
    const reason = asksWhy ? reasonBox() : null;
    if (reason) {
        box.appendChild(reason.parentElement ?? reason);
    }
    box.appendChild(actions);
    overlay.appendChild(box);
    return { overlay, confirmBtn, cancelBtn, reason };
}

/** A labelled box for why, which may stay empty. */
function reasonBox(): HTMLTextAreaElement {
    const field = el("label", "af-confirm-reason");
    const input = el("textarea", "af-confirm-reason-input");
    input.rows = 2;
    input.maxLength = MAX_REASON_CHARS;
    field.append(el("span", "", "Reason (optional)"), input);
    return input;
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
    return ask(opts, false).then(answer => answer !== null);
}

/**
 * As `confirmDialog`, with a box for why: the reason given, trimmed ("" when
 * none was), or null for every way of not confirming.
 */
export function confirmWithReason(opts: ConfirmOptions): Promise<string | null> {
    return ask(opts, true);
}

/** Open the dialog; the reason when confirmed ("" without a box), null otherwise. */
function ask(opts: ConfirmOptions, asksWhy: boolean): Promise<string | null> {
    closeActive?.();
    const { overlay, confirmBtn, cancelBtn, reason } = buildOverlay(opts, asksWhy);

    // Whatever had focus when this opened is where focus belongs afterwards.
    const opener = document.activeElement as HTMLElement | null;

    return new Promise<string | null>(resolve => {
        const settle = (answer: string | null): void => {
            overlay.remove();
            document.removeEventListener("keydown", onKey);
            closeActive = null;
            opener?.focus?.();
            resolve(answer);
        };

        const onKey = (e: KeyboardEvent): void => {
            if (e.key === "Escape") {
                settle(null);
            } else if (e.key === "Tab") {
                trapTab(overlay, e);
            }
        };

        // Only the backdrop dismisses: a click inside the box would otherwise
        // bubble here and cancel the dialog the user is still reading.
        overlay.addEventListener("click", e => {
            if (e.target === overlay) {
                settle(null);
            }
        });
        cancelBtn.addEventListener("click", () => settle(null));
        confirmBtn.addEventListener("click", () => settle(reason ? reason.value.trim() : ""));
        document.addEventListener("keydown", onKey);
        closeActive = () => settle(null);
        document.body.appendChild(overlay);
        cancelBtn.focus();
    });
}
