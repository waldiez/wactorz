/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Minimal full-screen image lightbox: click the backdrop, press Escape, or use
 * the close button. One instance at a time.
 *
 * It claims `role="dialog"` and `aria-modal`, which is a promise to keyboard and
 * screen-reader users that focus is inside and stays there. So focus is moved in
 * on open, Tab is cycled within, and the opener gets focus back on close —
 * otherwise the dialog is announced as modal while Tab walks off into the page
 * behind it, and a keyboard user who reaches the end of the document has no way
 * back to the only control that dismisses it.
 */

/** Elements inside the dialog that can hold focus, in tab order. */
function focusables(root: HTMLElement): HTMLElement[] {
    return [...root.querySelectorAll<HTMLElement>("button, [href], [tabindex]:not([tabindex='-1'])")];
}

/** The overlay, its image and its close button, assembled but not yet wired. */
function buildOverlay(url: string, alt: string): { overlay: HTMLElement; closeBtn: HTMLButtonElement } {
    const overlay = document.createElement("div");
    overlay.className = "af-lightbox";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", alt || "Image preview");

    const img = document.createElement("img");
    img.src = url;
    img.alt = alt;

    const closeBtn = document.createElement("button");
    closeBtn.className = "af-lightbox-close";
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "Close image preview");
    closeBtn.textContent = "✕";

    overlay.append(img, closeBtn);
    return { overlay, closeBtn };
}

/**
 * Closes the instance that is currently open, if any.
 *
 * Dropping the overlay element alone is not enough: its `keydown` handler lives
 * on `document` and would outlive it, leaving a detached dialog still answering
 * Escape and Tab and competing with the live one for focus.
 */
let closeActive: (() => void) | null = null;

export function openLightbox(url: string, alt = ""): void {
    closeActive?.();
    const { overlay, closeBtn } = buildOverlay(url, alt);

    // Whatever had focus when this opened is where focus belongs afterwards.
    const opener = document.activeElement as HTMLElement | null;

    const close = (): void => {
        overlay.remove();
        document.removeEventListener("keydown", onKey);
        closeActive = null;
        opener?.focus?.();
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
            close();
        } else if (e.key === "Tab") {
            trapTab(e);
        }
    };

    // Any click within the overlay dismisses it, as before — which covers the
    // close button too, since its click bubbles here.
    overlay.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    closeActive = close;
    document.body.appendChild(overlay);
    closeBtn.focus();
}
