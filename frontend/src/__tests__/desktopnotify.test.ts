/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { initNotifications, desktopNotifyBackground, clearUnreadBadge } from "../io/DesktopNotify";

// These are intentional no-op stubs on this branch; the contract is simply that
// they exist, return void, and never throw, so callers (main.ts) stay unchanged.
describe("DesktopNotify no-op stubs", () => {
    it("initNotifications returns undefined and does not throw", () => {
        expect(initNotifications()).toBeUndefined();
    });

    it("desktopNotifyBackground returns undefined and does not throw", () => {
        expect(desktopNotifyBackground("title", "body")).toBeUndefined();
    });

    it("clearUnreadBadge returns undefined and does not throw", () => {
        expect(clearUnreadBadge()).toBeUndefined();
    });
});
