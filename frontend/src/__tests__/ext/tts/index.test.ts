/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { seedServerConfig } from "../../../config/serverConfig";
import { safeStorage } from "../../../safeStorage";

describe("ext/tts barrel (index.ts)", () => {
    it("exports register, tts singleton, TTSManager class, and TTSVoice type", async () => {
        const mod = await import("../../../ext/tts");
        expect(mod.register).toBeTypeOf("function");
        expect(mod.tts).toBeDefined();
        expect(mod.TTSManager).toBeDefined();
        // TTSVoice is a type — it compiles if the import succeeds
    });

    it("register calls setApiBase and conditionally calls init", async () => {
        const mod = await import("../../../ext/tts");
        const spyApi = vi.spyOn(mod.tts, "setApiBase");
        const spyInit = vi.spyOn(mod.tts, "init").mockResolvedValue();

        // available: false — init not called
        mod.register({ apiBase: "/ha", available: false });
        expect(spyApi).toHaveBeenCalledWith("/ha");
        expect(spyInit).not.toHaveBeenCalled();

        spyApi.mockClear();
        spyInit.mockClear();

        // available: true — init called
        mod.register({ apiBase: "", available: true });
        expect(spyApi).toHaveBeenCalledWith("");
        expect(spyInit).toHaveBeenCalledOnce();
    });
});

/**
 * The module registers its `/api/config` field at import time, and a mistake
 * there is silent: a wrong key, or an optional chain that throws on a missing
 * block, leaves TTS looking unavailable with nothing logged.
 *
 * Driven through `seedServerConfig` rather than by mocking the registrar, so
 * the route the value actually takes is the one under test.
 */
describe("ext/tts config seeding", () => {
    const origFetch = globalThis.fetch;

    beforeEach(async () => {
        localStorage.clear();
        // Importing the module is what registers the entry.
        await import("../../../ext/tts");
    });
    afterEach(() => {
        globalThis.fetch = origFetch;
    });

    /** Serve one /api/config payload and run the seed. */
    async function seed(cfg: Record<string, unknown>): Promise<void> {
        globalThis.fetch = vi.fn(async () => ({
            ok: true,
            json: async () => cfg,
        })) as unknown as typeof fetch;
        await seedServerConfig();
    }

    it("records TTS as available when the backend offers it", async () => {
        await seed({ tts: { available: true } });

        expect(safeStorage.get("wactorz-tts-available")).toBe("1");
    });

    it("records TTS as unavailable when the backend says so", async () => {
        await seed({ tts: { available: false } });

        expect(safeStorage.get("wactorz-tts-available")).toBe("0");
    });

    it("records unavailable when the backend omits the tts block", async () => {
        // An older backend, or one with the extension disabled, sends no `tts`
        // key at all — the optional chain must yield "0", not throw.
        await seed({});

        expect(safeStorage.get("wactorz-tts-available")).toBe("0");
    });
});
