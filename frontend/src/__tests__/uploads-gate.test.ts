/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Whether the attachment UI is available is the server's answer, not the
 * build's. The routes only exist when the backend has uploads on, so a
 * build-time flag could only guess — and it guessed wrong in both directions:
 * a drop zone whose every upload 404s, or a feature hidden from a deployment
 * that has it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import { seedServerConfig } from "../config/serverConfig";
import { uploadsEnabled, UPLOADS_KEY } from "../ui/dashboard/uploads";
import { safeStorage } from "../safeStorage";

function respondWith(cfg: Record<string, unknown>): void {
    vi.stubGlobal(
        "fetch",
        vi.fn(async () => ({ ok: true, json: async () => cfg })),
    );
}

describe("the uploads gate", () => {
    beforeEach(() => {
        safeStorage.remove(UPLOADS_KEY);
        safeStorage.remove(`${UPLOADS_KEY}__server`);
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it("is off before the server has been asked", () => {
        expect(uploadsEnabled()).toBe(false);
    });

    it("turns on when the server says it takes uploads", async () => {
        respondWith({ uploads: { enabled: true } });

        await seedServerConfig();

        expect(uploadsEnabled()).toBe(true);
    });

    it("stays off when the server says it does not", async () => {
        respondWith({ uploads: { enabled: false } });

        await seedServerConfig();

        expect(uploadsEnabled()).toBe(false);
    });

    it("turns back off once the server stops taking them", async () => {
        // The reason the value is seeded as "1"/"0" rather than "1"/absent:
        // an empty value is ignored, so an absent one would leave the previous
        // "1" in place and keep offering uploads after they were turned off.
        respondWith({ uploads: { enabled: true } });
        await seedServerConfig();

        respondWith({ uploads: { enabled: false } });
        await seedServerConfig();

        expect(uploadsEnabled()).toBe(false);
    });

    it("stays off when the server says nothing at all", async () => {
        // An older backend has no `uploads` key; it also has no upload routes.
        respondWith({ ha: { url: "http://ha.local" } });

        await seedServerConfig();

        expect(uploadsEnabled()).toBe(false);
    });

    it("stays off when the config fetch fails", async () => {
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => {
                throw new Error("offline");
            }),
        );

        await seedServerConfig();

        expect(uploadsEnabled()).toBe(false);
    });
});
