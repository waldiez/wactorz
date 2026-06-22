/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect } from "vitest";
import { SpeechToText, STT_ENABLED } from "../io/SpeechToText";

describe("SpeechToText", () => {
    it("exposes the STT_ENABLED feature constant as a boolean", () => {
        expect(typeof STT_ENABLED).toBe("boolean");
    });

    it("reports unsupported when MediaRecorder/getUserMedia are absent (happy-dom)", () => {
        expect(SpeechToText.isSupported()).toBe(false);
    });

    it("is not recording before any capture starts", () => {
        expect(new SpeechToText().recording).toBe(false);
    });

    it("stopAndTranscribe returns '' when nothing was recorded", async () => {
        const text = await new SpeechToText().stopAndTranscribe();
        expect(text).toBe("");
    });
});
