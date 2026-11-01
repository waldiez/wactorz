/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { beforeEach, describe, expect, it } from "vitest";
import { BLOCKED_PACKAGES_KEY } from "../config/keys";
import { buildBlockedNotice } from "../ui/overview/blockedNotice";
import { byRole } from "./queries";

describe("buildBlockedNotice", () => {
    beforeEach(() => localStorage.clear());

    it("says nothing when no package was left out", () => {
        expect(buildBlockedNotice()).toBeNull();
    });

    it("names each package and the setting", () => {
        localStorage.setItem(BLOCKED_PACKAGES_KEY, "acme,wactorz-mlops");

        const text = buildBlockedNotice()?.textContent;

        expect(text).toContain("acme, wactorz-mlops");
        expect(text).toContain("WACTORZ_EXTENSIONS");
    });

    it("stays away once dismissed, until the list changes", () => {
        localStorage.setItem(BLOCKED_PACKAGES_KEY, "acme");
        const root = document.createElement("div");
        const notice = buildBlockedNotice();
        root.append(notice as HTMLElement);

        byRole(root, "button", "Dismiss").click();

        expect(root.children).toHaveLength(0);
        expect(buildBlockedNotice()).toBeNull();

        localStorage.setItem(BLOCKED_PACKAGES_KEY, "acme,other");
        expect(buildBlockedNotice()).not.toBeNull();
    });
});
