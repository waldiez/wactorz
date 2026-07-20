/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi } from "vitest";

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
