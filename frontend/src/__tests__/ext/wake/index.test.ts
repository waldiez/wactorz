/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * What the page believes about waking, and where it got it.
 *
 * Nothing here listens: the phrase is heard by the machine. What the browser
 * holds is only what it needs in order to say what is happening, which makes the
 * seeding the whole of this module's behaviour.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";

import {
    WAKE_KEY,
    WAKE_READY_KEY,
    WAKE_PHRASES_KEY,
    waking,
    wakeReady,
    wakePhrases,
} from "../../../ext/wake";
import { safeStorage } from "../../../safeStorage";
import { seedServerConfig } from "../../../config/serverConfig";

const KEYS = [WAKE_KEY, WAKE_READY_KEY, WAKE_PHRASES_KEY];

/** Clear a seeded key and the baseline beside it.
 *
 * Seeding skips a value the server has already given, so that a local choice is
 * not clobbered on every poll. A test that cleared only the key would find the
 * second seeding of the same value silently skipped. */
function forget(): void {
    for (const key of KEYS) {
        safeStorage.remove(key);
        safeStorage.remove(`${key}__server`);
    }
}

function served(wake: unknown): void {
    globalThis.fetch = (() =>
        Promise.resolve({ ok: true, json: () => Promise.resolve({ wake }) })) as unknown as typeof fetch;
}

beforeEach(forget);
afterEach(forget);

describe("what the browser is told about waking", () => {
    it("takes the state, the readiness and the phrases from the server", async () => {
        served({ mode: "on", ready: true, phrases: ["hey waldiez", "hey wactorz"] });

        await seedServerConfig();

        expect(waking()).toBe(true);
        expect(wakeReady()).toBe(true);
        // Joined for reading, since this is shown to a person rather than parsed.
        expect(wakePhrases()).toBe("hey waldiez, hey wactorz");
    });

    it("reads a deployment with no model as not ready", async () => {
        // The model is weights fetched at deploy time, not shipped with the
        // code, so a deployment can be set to wake with nothing to wake with.
        served({ mode: "on", ready: false, phrases: [] });

        await seedServerConfig();

        expect(waking()).toBe(true);
        expect(wakeReady()).toBe(false);
        expect(wakePhrases()).toBe("");
    });

    it("says nothing is waking when the server says nothing at all", async () => {
        // A deployment that predates this, or one where the extension failed to
        // load: silence means off, not broken.
        served(undefined);

        await seedServerConfig();

        expect(waking()).toBe(false);
        expect(wakeReady()).toBe(false);
    });
});
