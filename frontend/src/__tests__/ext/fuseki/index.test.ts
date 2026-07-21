/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";

describe("ext/fuseki barrel (index.ts)", () => {
    it("register is a noop when url is null", async () => {
        const registerView = vi.fn();
        const mod = await import("../../../ext/fuseki");
        mod.register({
            url: null,
            dataset: "wactorz",
            onRender: vi.fn(),
            registerView,
        });
        expect(registerView).not.toHaveBeenCalled();
    });

    it("register calls registerView with Graph builder when url is set", async () => {
        const registerView = vi.fn();
        const mod = await import("../../../ext/fuseki");
        mod.register({
            url: "http://fuseki:3030",
            dataset: "wactorz",
            onRender: vi.fn(),
            registerView,
        });
        expect(registerView).toHaveBeenCalledWith("fuseki", "hexagon", "Graph", expect.any(Function));
        // Call the builder to cover the thunk.
        const calls = registerView.mock.calls[0];
        expect(calls).toBeDefined();
        const builder = calls![3] as () => HTMLElement;
        const el = builder();
        expect(el).toBeInstanceOf(HTMLElement);
        expect(el.querySelector(".af-fuseki-sidebar")).not.toBeNull();
    });

    it("registers its /api/config entries (url + dataset seed into storage)", async () => {
        localStorage.clear();
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
        await import("../../../ext/fuseki"); // module load registers the entries
        const { seedServerConfig } = await import("../../../config/serverConfig");
        const origFetch = globalThis.fetch;
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ fuseki: { url: "http://fuseki:3030", dataset: "myds" } }),
        })) as unknown as typeof fetch;
        try {
            await seedServerConfig();
        } finally {
            globalThis.fetch = origFetch;
        }
        expect(localStorage.getItem("wactorz-fuseki-url")).toBe("http://fuseki:3030");
        expect(localStorage.getItem("wactorz-fuseki-dataset")).toBe("myds");
    });
});
