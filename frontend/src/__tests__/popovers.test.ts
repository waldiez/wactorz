/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../ext/tts", () => ({
    tts: {
        beepEnabled: false,
        ttsEnabled: false,
        toggleBeep: vi.fn(() => true),
        toggleTTS: vi.fn(() => true),
        voices: [{ name: "Microsoft Aria Online (Natural)" }, { name: "Google US" }],
        selectedVoice: "",
        setVoice: vi.fn(),
    },
}));
vi.mock("../io/AmbientManager", () => ({
    ambient: { track: "none", volume: 0.4, setVolume: vi.fn(), setTrack: vi.fn() },
    AMBIENT_TRACKS: [
        { id: "none", label: "Off" },
        { id: "rain", label: "Rain" },
    ],
}));
vi.mock("../ui/ToastManager", () => ({ toast: { show: vi.fn() } }));

import { buildAudioPopover, buildResetPopover, type AudioPopover } from "../ui/dashboard/popovers";
import { buildHeader, releaseHeaderPopovers } from "../ui/dashboard/header";
import { tts } from "../ext/tts";
import { ambient } from "../io/AmbientManager";
import { toast } from "../ui/ToastManager";

describe("buildAudioPopover", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
    });

    it("renders beep/TTS toggles and the voice list (names cleaned up)", () => {
        const pop = buildAudioPopover();
        expect(pop.querySelectorAll(".af-audio-toggle").length).toBe(2);
        const opts = [...pop.querySelectorAll<HTMLOptionElement>(".af-audio-select option")];
        // placeholder + 2 voices
        expect(opts.length).toBe(3);
        expect(opts.some(o => o.textContent === "Aria")).toBe(true); // "Microsoft …" and " Online…" stripped
    });

    it("beep toggle calls tts.toggleBeep", () => {
        const pop = buildAudioPopover();
        const beep = [...pop.querySelectorAll<HTMLButtonElement>(".af-audio-toggle")].find(b =>
            b.textContent?.includes("Beep"),
        )!;
        beep.click();
        expect(tts.toggleBeep).toHaveBeenCalled();
    });

    it("TTS toggle reveals the voice row", () => {
        const pop = buildAudioPopover();
        const voiceRow = pop.querySelector<HTMLElement>(".af-audio-select")!.parentElement!;
        expect(voiceRow.style.display).toBe("none"); // ttsEnabled=false initially
        const ttsBtn = [...pop.querySelectorAll<HTMLButtonElement>(".af-audio-toggle")].find(b =>
            b.textContent?.includes("TTS"),
        )!;
        ttsBtn.click();
        expect(tts.toggleTTS).toHaveBeenCalled();
        expect(voiceRow.style.display).toBe(""); // shown (toggleTTS → true)
    });

    it("voice select change sets the voice", () => {
        const pop = buildAudioPopover();
        const sel = pop.querySelector<HTMLSelectElement>(".af-audio-select")!;
        sel.value = "Google US";
        sel.dispatchEvent(new Event("change"));
        expect(tts.setVoice).toHaveBeenCalledWith("Google US");
    });

    it("re-populates the voice list on tts-voices-loaded and applies the saved voice", () => {
        (tts as { selectedVoice: string }).selectedVoice = "Google US";
        const pop = buildAudioPopover();
        const sel = pop.querySelector<HTMLSelectElement>(".af-audio-select")!;
        // Browser fires this once its async voice list resolves; populateVoices
        // re-runs, clearing + rebuilding the options and applying the saved voice.
        document.dispatchEvent(new Event("tts-voices-loaded"));
        expect([...sel.options].filter(o => o.value).length).toBe(2);
        expect(sel.value).toBe("Google US");
        (tts as { selectedVoice: string }).selectedVoice = "";
    });

    it("selecting an ambient track sets it and reveals the volume row", () => {
        const pop = buildAudioPopover();
        const volRow = pop.querySelector<HTMLElement>(".af-audio-vol-row")!;
        expect(volRow.style.display).toBe("none"); // track is "none"
        const rain = [...pop.querySelectorAll<HTMLButtonElement>(".af-audio-track-btn")].find(
            b => b.textContent === "Rain",
        )!;
        rain.click();
        expect(ambient.setTrack).toHaveBeenCalledWith("rain");
        expect(volRow.style.display).toBe("");
    });

    it("volume slider sets ambient volume", () => {
        const pop = buildAudioPopover();
        const slider = pop.querySelector<HTMLInputElement>(".af-audio-slider")!;
        slider.value = "0.8";
        slider.dispatchEvent(new Event("input"));
        expect(ambient.setVolume).toHaveBeenCalledWith(0.8);
    });

    it("exposes a _release hook that removes its tts-voices-loaded listener", () => {
        const removed = vi.spyOn(document, "removeEventListener");
        const pop = buildAudioPopover();
        (pop as AudioPopover)._release();
        expect(removed.mock.calls.some(c => c[0] === "tts-voices-loaded")).toBe(true);
        removed.mockRestore();
    });

    it("loses its tts-voices-loaded listener when the header is released (nav rebuild)", () => {
        const added = vi.spyOn(document, "addEventListener");
        const removed = vi.spyOn(document, "removeEventListener");
        const liveVoices = () =>
            added.mock.calls.filter(c => c[0] === "tts-voices-loaded").length -
            removed.mock.calls.filter(c => c[0] === "tts-voices-loaded").length;

        const header = buildHeader({
            view: "overview",
            connState: "live",
            onSetView: vi.fn(),
            haUrl: null,
            extraViews: [],
        });
        document.body.appendChild(header);
        expect(liveVoices()).toBe(1);

        releaseHeaderPopovers(); // what _rebuildNav calls before replacing the header
        expect(liveVoices()).toBe(0);
        added.mockRestore();
        removed.mockRestore();
    });
});

describe("buildResetPopover", () => {
    const origFetch = globalThis.fetch;
    beforeEach(() => {
        document.body.innerHTML = "";
        vi.clearAllMocks();
        (window as unknown as Record<string, unknown>).__WACTORZ_INGRESS_PATH = "";
    });
    afterEach(() => {
        globalThis.fetch = origFetch;
        vi.useRealTimers();
    });

    it("renders a button per reset scope", () => {
        const pop = buildResetPopover();
        // 6 scopes
        expect(pop.querySelectorAll("button").length).toBe(6);
    });

    it("requires two clicks to fire, then POSTs the reset and toasts", async () => {
        globalThis.fetch = vi.fn(async () => ({ ok: true })) as unknown as typeof fetch;
        const pop = buildResetPopover();
        const btn = pop.querySelector<HTMLButtonElement>("button")!; // first = chat
        btn.click(); // arm
        expect(btn.querySelector("span")!.textContent).toBe("Confirm chat history?");
        expect(fetch).not.toHaveBeenCalled();
        btn.click(); // fire
        await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/reset", expect.anything()));
        expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ title: "Reset" }));
    });

    it("arming one button disarms the others", () => {
        const pop = buildResetPopover();
        const [first, second] = [...pop.querySelectorAll<HTMLButtonElement>("button")];
        first!.click();
        expect(first!.querySelector("span")!.textContent).toContain("Confirm");
        second!.click();
        // first disarmed back to its label, second now armed
        expect(first!.querySelector("span")!.textContent).toBe("Chat history");
        expect(second!.querySelector("span")!.textContent).toContain("Confirm");
    });

    it("auto-disarms after the timeout", () => {
        vi.useFakeTimers();
        const pop = buildResetPopover();
        const btn = pop.querySelector<HTMLButtonElement>("button")!;
        btn.click();
        expect(btn.querySelector("span")!.textContent).toContain("Confirm");
        vi.advanceTimersByTime(3000);
        expect(btn.querySelector("span")!.textContent).toBe("Chat history");
    });

    it("toasts an error with the server message when the reset response is not ok", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: false,
            status: 500,
            json: async () => ({ error: "boom" }),
        })) as unknown as typeof fetch;
        const pop = buildResetPopover();
        const btn = pop.querySelector<HTMLButtonElement>("button")!;
        btn.click(); // arm
        btn.click(); // fire
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(
                expect.objectContaining({ title: "Reset failed", message: "boom" }),
            ),
        );
    });

    it("falls back to the status code when the error body is unparseable", async () => {
        globalThis.fetch = vi.fn(async () => ({
            ok: false,
            status: 503,
            json: async () => {
                throw new Error("not json");
            },
        })) as unknown as typeof fetch;
        const pop = buildResetPopover();
        const btn = pop.querySelector<HTMLButtonElement>("button")!;
        btn.click(); // arm
        btn.click(); // fire
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(
                expect.objectContaining({ title: "Reset failed", message: "503" }),
            ),
        );
    });

    it("toasts an error when the reset request throws", async () => {
        globalThis.fetch = vi.fn(async () => {
            throw new Error("network down");
        }) as unknown as typeof fetch;
        const pop = buildResetPopover();
        const btn = pop.querySelector<HTMLButtonElement>("button")!;
        btn.click(); // arm
        btn.click(); // fire
        await vi.waitFor(() =>
            expect(toast.show).toHaveBeenCalledWith(expect.objectContaining({ title: "Reset failed" })),
        );
    });
});
