/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import {
    STT_KEY,
    STT_AVAILABLE_KEY,
    STT_LIVE_KEY,
    sttMode,
    micOffered,
    liveOffered,
    SpeechToText,
} from "../../../ext/stt";
import { safeStorage } from "../../../safeStorage";

describe("the speech-to-text branch", () => {
    beforeEach(() => {
        safeStorage.remove(STT_KEY);
        safeStorage.remove(STT_AVAILABLE_KEY);
    });

    afterEach(() => {
        safeStorage.remove(STT_KEY);
        safeStorage.remove(STT_AVAILABLE_KEY);
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
        safeStorage.remove(STT_AVAILABLE_KEY);
    });

    afterEach(() => {
        safeStorage.remove(STT_KEY);
        safeStorage.remove(STT_AVAILABLE_KEY);
        vi.restoreAllMocks();
    });

    it("does not when there is no branch", () => {
        vi.spyOn(SpeechToText, "isSupported").mockReturnValue(true);

        expect(micOffered()).toBe(false);
    });

    it("does not in browser, which has no transcriber on the client", () => {
        vi.spyOn(SpeechToText, "isSupported").mockReturnValue(true);
        safeStorage.set(STT_KEY, "browser");
        safeStorage.set(STT_AVAILABLE_KEY, "1");

        // The button's only path sends the clip to the server, which is the one
        // place this branch says the audio must not go.
        expect(micOffered()).toBe(false);
    });

    it("does in server when a recogniser is reachable", () => {
        vi.spyOn(SpeechToText, "isSupported").mockReturnValue(true);
        safeStorage.set(STT_KEY, "server");
        safeStorage.set(STT_AVAILABLE_KEY, "1");

        expect(micOffered()).toBe(true);
    });

    it("does not in server when the server has no recogniser", () => {
        vi.spyOn(SpeechToText, "isSupported").mockReturnValue(true);
        safeStorage.set(STT_KEY, "server");
        safeStorage.set(STT_AVAILABLE_KEY, "0");

        // Configured for recognition without the dependency installed: offering
        // a button that answers 503 every time is worse than offering none.
        expect(micOffered()).toBe(false);
    });

    it("does not in server when the browser cannot record", () => {
        vi.spyOn(SpeechToText, "isSupported").mockReturnValue(false);
        safeStorage.set(STT_KEY, "server");
        safeStorage.set(STT_AVAILABLE_KEY, "1");

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

describe("whether that microphone shows words as they are spoken", () => {
    /** A deployment where everything the batch branch needs is already true. */
    function ready(): void {
        vi.spyOn(SpeechToText, "isSupported").mockReturnValue(true);
        safeStorage.set(STT_KEY, "server");
        safeStorage.set(STT_AVAILABLE_KEY, "1");
        Reflect.set(window, "AudioContext", class {});
        Reflect.set(navigator, "mediaDevices", { getUserMedia: () => {} });
    }

    beforeEach(() => {
        safeStorage.remove(STT_LIVE_KEY);
    });

    afterEach(() => {
        safeStorage.remove(STT_KEY);
        safeStorage.remove(STT_AVAILABLE_KEY);
        safeStorage.remove(STT_LIVE_KEY);
        Reflect.deleteProperty(window, "AudioContext");
        Reflect.deleteProperty(navigator, "mediaDevices");
        vi.restoreAllMocks();
    });

    it("does when the recogniser streams and the browser can capture", () => {
        ready();
        safeStorage.set(STT_LIVE_KEY, "1");

        expect(liveOffered()).toBe(true);
    });

    it("does not when the recogniser only takes whole recordings", () => {
        ready();
        safeStorage.set(STT_LIVE_KEY, "0");

        // The button still works; it sends the recording at the end instead.
        expect(liveOffered()).toBe(false);
    });

    it("does not when the browser cannot deliver frames", () => {
        ready();
        safeStorage.set(STT_LIVE_KEY, "1");
        Reflect.deleteProperty(window, "AudioContext");

        expect(liveOffered()).toBe(false);
    });

    it("does not when no microphone is offered at all", () => {
        ready();
        safeStorage.set(STT_LIVE_KEY, "1");
        safeStorage.set(STT_AVAILABLE_KEY, "0");

        expect(liveOffered()).toBe(false);
    });
});
