/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { seedHaConfigFromServer } from "../ui/dashboard/haConfig";

describe("seedHaConfigFromServer", () => {
    const origFetch = globalThis.fetch;
    beforeEach(() => {
        localStorage.clear();
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
    });
    afterEach(() => {
        globalThis.fetch = origFetch;
        vi.restoreAllMocks();
    });

    it("seeds url and token from the server when localStorage is empty", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: { url: "http://ha.local", token: "tok" } }),
        })) as unknown as typeof fetch;

        expect(await seedHaConfigFromServer()).toBe(true);
        expect(localStorage.getItem("wactorz-ha-url")).toBe("http://ha.local");
        expect(localStorage.getItem("wactorz-ha-token")).toBe("tok");
    });

    it("never overwrites values the user already set (manual wins)", async () => {
        localStorage.setItem("wactorz-ha-url", "http://manual");
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: { url: "http://server", token: "tok" } }),
        })) as unknown as typeof fetch;

        const changed = await seedHaConfigFromServer();
        expect(localStorage.getItem("wactorz-ha-url")).toBe("http://manual"); // untouched
        expect(localStorage.getItem("wactorz-ha-token")).toBe("tok"); // newly seeded
        expect(changed).toBe(true);
    });

    it("returns false when nothing needs seeding", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: {} }),
        })) as unknown as typeof fetch;
        expect(await seedHaConfigFromServer()).toBe(false);
    });

    it("returns false on a non-OK response", async () => {
        globalThis.fetch = vi.fn(async () => ({ ok: false })) as unknown as typeof fetch;
        expect(await seedHaConfigFromServer()).toBe(false);
    });

    it("returns false when fetch throws (server not ready)", async () => {
        globalThis.fetch = vi.fn(async () => {
            throw new Error("conn refused");
        }) as unknown as typeof fetch;
        expect(await seedHaConfigFromServer()).toBe(false);
    });
});
