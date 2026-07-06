/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, afterEach } from "vitest";

// The flags are build-time consts that read import.meta.env, so each case stubs
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

    it("UPLOADS_ENABLED: off by default, on when VITE_UPLOADS_ENABLED='true'", async () => {
        vi.stubEnv("VITE_UPLOADS_ENABLED", "");
        vi.resetModules();
        expect((await import("../ui/dashboard/uploads")).UPLOADS_ENABLED).toBe(false);

        vi.stubEnv("VITE_UPLOADS_ENABLED", "true");
        vi.resetModules();
        expect((await import("../ui/dashboard/uploads")).UPLOADS_ENABLED).toBe(true);
    });
});
