/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { seedHaConfigFromServer, seedKeyFromServer } from "../ui/dashboard/haConfig";

describe("seedKeyFromServer", () => {
    beforeEach(() => localStorage.clear());

    it("does nothing for an empty value", () => {
        expect(seedKeyFromServer("k", "")).toBe(false);
        expect(seedKeyFromServer("k", undefined)).toBe(false);
        expect(seedKeyFromServer("k", null)).toBe(false);
        expect(localStorage.getItem("k")).toBeNull();
    });

    it("seeds the key and records the server baseline on first sight", () => {
        expect(seedKeyFromServer("k", "v")).toBe(true);
        expect(localStorage.getItem("k")).toBe("v");
        expect(localStorage.getItem("k__server")).toBe("v");
    });

    it("preserves a local edit while the server value is unchanged", () => {
        seedKeyFromServer("k", "server1"); // baseline = server1
        localStorage.setItem("k", "manual"); // user edits in Settings
        expect(seedKeyFromServer("k", "server1")).toBe(false); // same server value → no write
        expect(localStorage.getItem("k")).toBe("manual");
    });

    it("adopts a changed server value (propagates an updated .env)", () => {
        seedKeyFromServer("k", "server1");
        localStorage.setItem("k", "manual");
        expect(seedKeyFromServer("k", "server2")).toBe(true); // .env changed → overwrite
        expect(localStorage.getItem("k")).toBe("server2");
        expect(localStorage.getItem("k__server")).toBe("server2");
    });
});

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

    it("seeds the url from the server when localStorage is empty", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: { url: "http://ha.local" } }),
        })) as unknown as typeof fetch;

        expect(await seedHaConfigFromServer()).toBe(true);
        expect(localStorage.getItem("wactorz-ha-url")).toBe("http://ha.local");
    });

    it("never persists a token, even if the server still sends one", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: { url: "http://ha.local", token: "tok" } }),
        })) as unknown as typeof fetch;

        await seedHaConfigFromServer();
        expect(localStorage.getItem("wactorz-ha-url")).toBe("http://ha.local");
        expect(localStorage.getItem("wactorz-ha-token")).toBeNull(); // token is never stored
    });

    it("leaves an unchanged url alone (no rewrite)", async () => {
        localStorage.setItem("wactorz-ha-url", "http://server");
        localStorage.setItem("wactorz-ha-url__server", "http://server"); // already seeded once
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: { url: "http://server" } }),
        })) as unknown as typeof fetch;

        expect(await seedHaConfigFromServer()).toBe(false);
        expect(localStorage.getItem("wactorz-ha-url")).toBe("http://server");
    });

    it("returns false when the server sends no url", async () => {
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
