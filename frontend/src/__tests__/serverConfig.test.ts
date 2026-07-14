/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { seedServerConfig, seedKeyFromServer } from "../ui/dashboard/serverConfig";

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

describe("seedServerConfig", () => {
    const origFetch = globalThis.fetch;
    beforeEach(() => {
        localStorage.clear();
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
    });
    afterEach(() => {
        globalThis.fetch = origFetch;
        vi.restoreAllMocks();
    });

    it("seeds all config fields from the server when localStorage is empty", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({
                ha: { url: "http://ha.local" },
                fuseki: { url: "http://fuseki.local:3030", dataset: "myds" },
            }),
        })) as unknown as typeof fetch;

        expect(await seedServerConfig()).toBe(true);
        expect(localStorage.getItem("wactorz-ha-url")).toBe("http://ha.local");
        expect(localStorage.getItem("wactorz-fuseki-url")).toBe("http://fuseki.local:3030");
        expect(localStorage.getItem("wactorz-fuseki-dataset")).toBe("myds");
    });

    it("never persists a token, even if the server still sends one", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: { url: "http://ha.local", token: "tok" } }),
        })) as unknown as typeof fetch;

        await seedServerConfig();
        expect(localStorage.getItem("wactorz-ha-url")).toBe("http://ha.local");
        expect(localStorage.getItem("wactorz-ha-token")).toBeNull(); // token is never stored
    });

    it("returns true only when the HA URL changed", async () => {
        // Pre-seed only fuseki — HA URL is still missing, so return true.
        localStorage.setItem("wactorz-fuseki-url", "http://old-fuseki");
        localStorage.setItem("wactorz-fuseki-url__server", "http://old-fuseki");
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({
                ha: { url: "http://ha.local" },
                fuseki: { url: "http://old-fuseki" },
            }),
        })) as unknown as typeof fetch;

        expect(await seedServerConfig()).toBe(true);
        expect(localStorage.getItem("wactorz-ha-url")).toBe("http://ha.local");
    });

    it("returns false when nothing changed", async () => {
        localStorage.setItem("wactorz-ha-url", "http://server");
        localStorage.setItem("wactorz-ha-url__server", "http://server");
        localStorage.setItem("wactorz-fuseki-url", "http://fuseki");
        localStorage.setItem("wactorz-fuseki-url__server", "http://fuseki");
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({
                ha: { url: "http://server" },
                fuseki: { url: "http://fuseki" },
            }),
        })) as unknown as typeof fetch;

        expect(await seedServerConfig()).toBe(false);
    });

    it("adopts a changed fuseki URL from an updated .env", async () => {
        // Pre-seed with old fuseki URL so HA stays unchanged but fuseki changes.
        localStorage.setItem("wactorz-ha-url", "http://ha");
        localStorage.setItem("wactorz-ha-url__server", "http://ha");
        localStorage.setItem("wactorz-fuseki-url", "http://old-fuseki");
        localStorage.setItem("wactorz-fuseki-url__server", "http://old-fuseki");
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({
                ha: { url: "http://ha" },
                fuseki: { url: "http://new-fuseki" },
            }),
        })) as unknown as typeof fetch;

        expect(await seedServerConfig()).toBe(false); // HA didn't change
        expect(localStorage.getItem("wactorz-fuseki-url")).toBe("http://new-fuseki");
        expect(localStorage.getItem("wactorz-fuseki-url__server")).toBe("http://new-fuseki");
    });

    it("returns false when the server sends no ha url", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: {}, fuseki: { url: "http://f" } }),
        })) as unknown as typeof fetch;
        expect(await seedServerConfig()).toBe(false);
        // Fuseki still gets seeded even though HA didn't change.
        expect(localStorage.getItem("wactorz-fuseki-url")).toBe("http://f");
    });

    it("returns false on a non-OK response", async () => {
        globalThis.fetch = vi.fn(async () => ({ ok: false })) as unknown as typeof fetch;
        expect(await seedServerConfig()).toBe(false);
    });

    it("returns false when fetch throws (server not ready)", async () => {
        globalThis.fetch = vi.fn(async () => {
            throw new Error("conn refused");
        }) as unknown as typeof fetch;
        expect(await seedServerConfig()).toBe(false);
    });
});
