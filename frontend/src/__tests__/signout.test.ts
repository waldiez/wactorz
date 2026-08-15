/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Signing out, and when the control exists at all.
 *
 * The credential never reaches JavaScript: the button submits a form and the
 * browser attaches the cookie itself, which it can do and script cannot,
 * because the cookie is `HttpOnly`.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

import {
    SIGN_OUT_KEY,
    buildSignOutButton,
    canSignOut,
    setSignOutVisible,
    signOut,
} from "../ui/dashboard/signOut";
import { safeStorage } from "../safeStorage";

beforeEach(() => {
    localStorage.clear();
    document.body.innerHTML = "";
    delete window.__WACTORZ_INGRESS_PATH;
});

describe("canSignOut", () => {
    it("is false until the server says otherwise", () => {
        // An install with no key, and the state before /api/config has answered.
        expect(canSignOut()).toBe(false);
    });

    it("is true when the server reports a session", () => {
        safeStorage.set(SIGN_OUT_KEY, "1");

        expect(canSignOut()).toBe(true);
    });
});

describe("buildSignOutButton", () => {
    it("is hidden when there is no session to end", () => {
        // Under Home Assistant ingress, or with no key at all, the control
        // would end nothing while implying it had.
        expect(buildSignOutButton().style.display).toBe("none");
    });

    it("is built even before the server has answered, and revealed after", () => {
        // The header is assembled before /api/config lands. A button created
        // only when the flag is already there never appears on a first load —
        // only on the reload after one, which is how the Devices link is done.
        const root = document.createElement("div");
        root.appendChild(buildSignOutButton());

        safeStorage.set(SIGN_OUT_KEY, "1");
        setSignOutVisible(root);

        expect(root.querySelector<HTMLElement>(".af-sign-out-btn")?.style.display).toBe("");
    });

    it("is labelled for anyone not seeing the icon", () => {
        safeStorage.set(SIGN_OUT_KEY, "1");

        const btn = buildSignOutButton();

        expect(btn.getAttribute("aria-label")).toBe("Sign out");
    });
});

describe("signOut", () => {
    it("posts a form rather than sending a credential from script", () => {
        const submit = vi.fn();
        vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(submit);

        signOut();

        const form = document.querySelector("form");
        expect(form?.method).toBe("post");
        expect(form?.getAttribute("action")).toBe("/logout");
        expect(submit).toHaveBeenCalledOnce();
    });

    it("honours an ingress path prefix", () => {
        vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(vi.fn());
        window.__WACTORZ_INGRESS_PATH = "/api/hassio_ingress/abc";

        signOut();

        expect(document.querySelector("form")?.getAttribute("action")).toBe("/api/hassio_ingress/abc/logout");
    });

    it("attaches the form to the page, or the browser ignores the submit", () => {
        vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(vi.fn());

        signOut();

        expect(document.body.querySelector("form")).not.toBeNull();
    });
});
