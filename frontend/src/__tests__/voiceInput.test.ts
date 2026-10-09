/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, beforeEach } from "vitest";
import {
    STT_AVAILABLE_KEY,
    VOICE_MODE_KEY,
    currentSupport,
    micOffered,
    resolveVoiceEngine,
    setVoiceMode,
    unavailableReason,
    voiceMode,
    type VoiceSupport,
} from "../io/voiceInput";
import { safeStorage } from "../safeStorage";
import { listen } from "../events";

const everything: VoiceSupport = { server: true, browser: true, recorder: true };
const browserOnly: VoiceSupport = { server: false, browser: true, recorder: true };
const serverOnly: VoiceSupport = { server: true, browser: false, recorder: true };
const nothing: VoiceSupport = { server: false, browser: false, recorder: false };

describe("resolveVoiceEngine", () => {
    it("auto prefers the server when it can transcribe", () => {
        expect(resolveVoiceEngine("auto", everything)).toBe("server");
    });

    it("auto falls back to the browser", () => {
        expect(resolveVoiceEngine("auto", browserOnly)).toBe("browser");
    });

    it("the server needs a browser that can record, as well as a recognizer", () => {
        expect(resolveVoiceEngine("server", { ...everything, recorder: false })).toBeNull();
        expect(resolveVoiceEngine("auto", { server: true, browser: true, recorder: false })).toBe("browser");
    });

    it("an explicit choice never silently becomes the other engine", () => {
        expect(resolveVoiceEngine("browser", serverOnly)).toBeNull();
        expect(resolveVoiceEngine("server", browserOnly)).toBeNull();
    });

    it("off is off", () => {
        expect(resolveVoiceEngine("off", everything)).toBeNull();
    });
});

describe("micOffered", () => {
    it("offers the mic when some engine could work and it is not off", () => {
        expect(micOffered("auto", browserOnly)).toBe(true);
        expect(micOffered("off", everything)).toBe(false);
        expect(micOffered("auto", nothing)).toBe(false);
    });
});

describe("unavailableReason", () => {
    it("points a browser without recognition at Chrome or the server", () => {
        expect(unavailableReason("browser", serverOnly)).toContain("Chrome or Edge");
    });

    it("points an unconfigured server at the key to set", () => {
        expect(unavailableReason("server", browserOnly)).toContain("DEEPGRAM_API_KEY");
    });

    it("explains a browser that cannot record at all", () => {
        expect(unavailableReason("server", { server: true, browser: true, recorder: false })).toContain(
            "cannot record",
        );
    });
});

describe("the saved choice", () => {
    beforeEach(() => {
        safeStorage.remove(VOICE_MODE_KEY);
    });

    it("defaults to auto, including when what is stored is not a mode", () => {
        expect(voiceMode()).toBe("auto");
        safeStorage.set(VOICE_MODE_KEY, "telepathy");
        expect(voiceMode()).toBe("auto");
    });

    it("is saved and announced", () => {
        const seen: string[] = [];
        const handler = listen("af-voice-mode", d => seen.push(d.mode));
        setVoiceMode("browser");
        document.removeEventListener("af-voice-mode", handler);

        expect(voiceMode()).toBe("browser");
        expect(seen).toEqual(["browser"]);
    });

    it("reads the server's answer from storage", () => {
        safeStorage.set(STT_AVAILABLE_KEY, "1");
        expect(currentSupport().server).toBe(true);
        safeStorage.set(STT_AVAILABLE_KEY, "0");
        expect(currentSupport().server).toBe(false);
    });
});
