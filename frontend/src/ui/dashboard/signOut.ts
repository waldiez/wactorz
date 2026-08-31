/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Ending the session, from the dashboard.
 *
 * A real form submission rather than `fetch`, so the browser follows the
 * server's redirect to the sign-in page by itself. That also keeps the
 * credential story intact: nothing here reads, holds or sends a token — the
 * cookie travels because the browser attaches it, and it is `HttpOnly`, so
 * script could not read it even to try.
 */
import { iconMarkup } from "./icons";
import { safeStorage } from "../../safeStorage";

/** Where the seeded server capability is kept. See `config/serverConfig.ts`. */
export const SIGN_OUT_KEY = "wactorz-can-sign-out";

/**
 * Whether this browser holds a session it could end.
 *
 * Answered by the server rather than guessed. It is not "is a key set": under
 * Home Assistant ingress the user was authenticated by HA and carries no
 * session here, so the control would end nothing while implying it had.
 */
export function canSignOut(): boolean {
    return safeStorage.get(SIGN_OUT_KEY) === "1";
}

/** Submit a POST to `/logout` and let the browser follow where it leads. */
export function signOut(): void {
    const base = window.__WACTORZ_INGRESS_PATH ?? "";
    const form = document.createElement("form");
    form.method = "post";
    form.action = `${base}/logout`;
    // Attached before submitting: a detached form is ignored by the browser.
    document.body.appendChild(form);
    form.submit();
}

/**
 * The header's sign-out button, hidden until the server says there is a session.
 *
 * Built either way and revealed later, like the Devices link: the header is
 * assembled before `/api/config` has answered, so a button that is only created
 * when the flag is already there is a button that never appears on a first
 * load — only on the reload after one.
 */
export function buildSignOutButton(): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.className = "af-view-btn af-view-btn-icon";
    btn.title = "Sign out";
    btn.setAttribute("aria-label", "Sign out");
    btn.innerHTML = iconMarkup("sign-out");
    btn.addEventListener("click", () => signOut());
    btn.classList.add("af-sign-out-btn");
    applySignOutVisibility(btn);
    return btn;
}

/** Show a sign-out button only when there is a session to end. */
function applySignOutVisibility(btn: HTMLElement): void {
    btn.style.display = canSignOut() ? "" : "none";
}

/** Reveal or hide every sign-out button under `root`, once /api/config lands. */
export function setSignOutVisible(root: HTMLElement): void {
    root.querySelectorAll<HTMLElement>(".af-sign-out-btn").forEach(applySignOutVisibility);
}
