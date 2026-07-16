/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";

describe("ext/swid barrel (index.ts)", () => {
    it("register calls registerView with Identity builder", async () => {
        const registerView = vi.fn();
        const mod = await import("../../../ext/swid");
        mod.register({
            onRender: vi.fn(),
            registerView,
        });
        expect(registerView).toHaveBeenCalledWith("identity", "key", "Identity", expect.any(Function));
        // Call the builder to cover the thunk.
        const calls = registerView.mock.calls[0];
        expect(calls).toBeDefined();
        const builder = calls![3] as () => HTMLElement;
        const el = builder();
        expect(el).toBeInstanceOf(HTMLElement);
    });
});
