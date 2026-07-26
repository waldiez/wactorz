/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { seedServerConfig, seedKeyFromServer, registerConfigEntry } from "../config/serverConfig";

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

    it("seeds the core HA URL from the server when localStorage is empty", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: { url: "http://ha.local" } }),
        })) as unknown as typeof fetch;

        expect(await seedServerConfig()).toBe(true);
        expect(localStorage.getItem("wactorz-ha-url")).toBe("http://ha.local");
    });

    it("seeds extension-registered entries (registerConfigEntry)", async () => {
        registerConfigEntry(
            "wactorz-myext-flag",
            c => (c.myext as Record<string, unknown> | undefined)?.flag as string | undefined,
        );
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: { url: "http://ha.local" }, myext: { flag: "on" } }),
        })) as unknown as typeof fetch;

        await seedServerConfig();
        expect(localStorage.getItem("wactorz-myext-flag")).toBe("on");
    });

    it("ignores unregistered server fields (whitelist)", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: { url: "http://ha.local" }, rogue: { secret: "x" } }),
        })) as unknown as typeof fetch;

        await seedServerConfig();
        expect(localStorage.getItem("wactorz-rogue-secret")).toBeNull();
        expect(localStorage.getItem("rogue")).toBeNull();
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

    it("returns false when the HA URL is unchanged (no rewrite)", async () => {
        localStorage.setItem("wactorz-ha-url", "http://ha.local");
        localStorage.setItem("wactorz-ha-url__server", "http://ha.local");
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({ ha: { url: "http://ha.local" } }),
        })) as unknown as typeof fetch;

        expect(await seedServerConfig()).toBe(false);
    });

    it("returns false when the server sends no url", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => ({}),
        })) as unknown as typeof fetch;

        expect(await seedServerConfig()).toBe(false);
        expect(localStorage.getItem("wactorz-ha-url")).toBeNull();
    });

    it("returns false on a non-OK response", async () => {
        globalThis.fetch = vi.fn(async () => ({ ok: false })) as unknown as typeof fetch;
        expect(await seedServerConfig()).toBe(false);
    });

    it("returns false when fetch throws (server not ready)", async () => {
        globalThis.fetch = vi.fn(async () => {
            throw new Error("ECONNREFUSED");
        }) as unknown as typeof fetch;
        expect(await seedServerConfig()).toBe(false);
    });
});
