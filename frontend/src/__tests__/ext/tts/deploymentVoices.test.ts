/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../../../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { TTSManager, tts } from "../../../ext/tts";
import { buildVoiceSection } from "../../../ui/dashboard/voiceSettings";
import { safeStorage } from "../../../safeStorage";

const SERVER = [{ name: "en-GB-SoniaNeural", locale: "en-GB", gender: "Female" }];

function serverAnswers(voices: unknown): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => voices }));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
}

describe("the voices a deployment speaks in", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        (globalThis as any).speechSynthesis = {
            speak: vi.fn(),
            cancel: vi.fn(),
            getVoices: () => [{ name: "Google UK English Female", lang: "en-GB" }],
            addEventListener: vi.fn(),
        };
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.restoreAllMocks();
    });

    it("are fetched on host, though this page plays nothing", async () => {
        serverAnswers(SERVER);
        const m = new TTSManager();
        m.setMode("host");

        await m.init();

        expect(m.deploymentVoices).toEqual(SERVER);
        expect(m.voices).toEqual([]);
    });

    it("stay the server's when this browser covers for a server that cannot speak", async () => {
        serverAnswers(SERVER);
        const m = new TTSManager();
        m.setMode("server");
        m.setServerAvailable(false);

        await m.init();

        // This browser plays in its own voice; the deployment's stays the server's.
        expect(m.voices.map(v => v.name)).toEqual(["Google UK English Female"]);
        expect(m.deploymentVoices).toEqual(SERVER);
    });

    it("are not asked for when the browser does the speaking", async () => {
        const fetchMock = serverAnswers(SERVER);
        const m = new TTSManager();
        m.setMode("browser");

        await m.init();

        expect(fetchMock).not.toHaveBeenCalled();
        expect(m.deploymentVoices).toEqual([]);
    });
});

describe("choosing the deployment's voice in Settings", () => {
    afterEach(() => {
        vi.restoreAllMocks();
        for (const key of ["wactorz-stt-mode", "wactorz-tts-mode"]) {
            safeStorage.remove(key);
        }
    });

    const voicePicker = (section: HTMLElement | null): HTMLSelectElement | undefined =>
        [...(section?.querySelectorAll("select") ?? [])].find(s => s.getAttribute("aria-label") === "Voice");

    it("offers the deployment's voices, never this browser's", () => {
        safeStorage.set("wactorz-stt-mode", "off");
        safeStorage.set("wactorz-tts-mode", "host");
        vi.spyOn(tts, "voices", "get").mockReturnValue([{ name: "Google UK English Female" } as never]);
        vi.spyOn(tts, "deploymentVoices", "get").mockReturnValue(SERVER as never);

        const options = [...voicePicker(buildVoiceSection(""))!.options].map(o => o.value);

        // Saved as the deployment's, a browser's voice would fail every reply.
        expect(options).toEqual(["", "en-GB-SoniaNeural"]);
    });

    it("is not offered when the browser does the speaking", () => {
        safeStorage.set("wactorz-stt-mode", "off");
        safeStorage.set("wactorz-tts-mode", "browser");

        expect(voicePicker(buildVoiceSection(""))).toBeUndefined();
    });
});
