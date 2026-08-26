/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import { STT_KEY, sttMode, micOffered, SpeechToText } from "../io/SpeechToText";
import { safeStorage } from "../safeStorage";

describe("the speech-to-text branch", () => {
    beforeEach(() => {
        safeStorage.remove(STT_KEY);
    });

    afterEach(() => {
        safeStorage.remove(STT_KEY);
        vi.restoreAllMocks();
    });

    it("is off until the server says otherwise", () => {
        expect(sttMode()).toBe("off");
    });

    it.each(["browser", "server", "host"] as const)("reads back %s", mode => {
        safeStorage.set(STT_KEY, mode);

        expect(sttMode()).toBe(mode);
    });

    it("treats a branch it does not know as off", () => {
        // A newer server naming a branch this bundle cannot drive: offering
        // nothing beats offering a control that goes nowhere.
        safeStorage.set(STT_KEY, "quantum");

        expect(sttMode()).toBe("off");
    });
});

describe("whether the composer offers a microphone", () => {
    beforeEach(() => {
        safeStorage.remove(STT_KEY);
    });

    afterEach(() => {
        safeStorage.remove(STT_KEY);
        vi.restoreAllMocks();
    });

    it("does not when there is no branch", () => {
        vi.spyOn(SpeechToText, "isSupported").mockReturnValue(true);

        expect(micOffered()).toBe(false);
    });

    it.each(["browser", "server"] as const)("does in %s when the browser can record", mode => {
        vi.spyOn(SpeechToText, "isSupported").mockReturnValue(true);
        safeStorage.set(STT_KEY, mode);

        expect(micOffered()).toBe(true);
    });

    it.each(["browser", "server"] as const)("does not in %s when it cannot", mode => {
        vi.spyOn(SpeechToText, "isSupported").mockReturnValue(false);
        safeStorage.set(STT_KEY, mode);

        expect(micOffered()).toBe(false);
    });

    it("does not in host, which the server drives instead", () => {
        // Not an oversight: host capture never touches getUserMedia, so this
        // button is the wrong control for it and the branch supplies its own.
        vi.spyOn(SpeechToText, "isSupported").mockReturnValue(true);
        safeStorage.set(STT_KEY, "host");

        expect(micOffered()).toBe(false);
    });
});
