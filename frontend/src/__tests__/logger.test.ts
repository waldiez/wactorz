/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { log } from "../io/logger";

describe("log", () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it("forwards warn and error to the console in any build", () => {
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
        const error = vi.spyOn(console, "error").mockImplementation(() => {});
        log.warn("w", 1);
        log.error("e", 2);
        expect(warn).toHaveBeenCalledWith("w", 1);
        expect(error).toHaveBeenCalledWith("e", 2);
    });

    it("forwards info and debug under the test (dev) build", () => {
        const info = vi.spyOn(console, "info").mockImplementation(() => {});
        const debug = vi.spyOn(console, "debug").mockImplementation(() => {});
        log.info("i");
        log.debug("d");
        // Vitest runs as a dev build (import.meta.env.DEV === true), so these surface.
        expect(info).toHaveBeenCalledWith("i");
        expect(debug).toHaveBeenCalledWith("d");
    });
});

describe("log in a production build", () => {
    afterEach(() => {
        vi.unstubAllEnvs();
        vi.restoreAllMocks();
        vi.resetModules();
    });

    it("drops info and debug, but still forwards warn and error", async () => {
        // DEV is read once at module load, so the module has to be re-imported
        // after stubbing — spying on an already-loaded `log` would keep the
        // dev-build behaviour and quietly test nothing.
        vi.stubEnv("DEV", false);
        vi.resetModules();
        const { log: prodLog } = await import("../io/logger");

        const info = vi.spyOn(console, "info").mockImplementation(() => {});
        const debug = vi.spyOn(console, "debug").mockImplementation(() => {});
        const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

        prodLog.info("i");
        prodLog.debug("d");
        prodLog.warn("w");

        expect(info).not.toHaveBeenCalled();
        expect(debug).not.toHaveBeenCalled();
        expect(warn).toHaveBeenCalledWith("w"); // real problems still surface
    });
});
