/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, afterEach } from "vitest";

// STT_ENABLED is a build-time const reading import.meta.env, so the case stubs
// the env and re-imports the module fresh (resetModules) to re-evaluate it.
describe("build-time feature flags", () => {
    afterEach(() => {
        vi.unstubAllEnvs();
        vi.resetModules();
    });

    it("STT_ENABLED: off by default, on when VITE_STT_ENABLED='true'", async () => {
        vi.stubEnv("VITE_STT_ENABLED", "");
        vi.resetModules();
        expect((await import("../io/SpeechToText")).STT_ENABLED).toBe(false);

        vi.stubEnv("VITE_STT_ENABLED", "true");
        vi.resetModules();
        expect((await import("../io/SpeechToText")).STT_ENABLED).toBe(true);
    });
});
