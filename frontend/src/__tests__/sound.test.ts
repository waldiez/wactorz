/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** Settings → Sound: the beep, replies read aloud and their voice, the ambient track, and how they end a mute. */
import { describe, it, expect, vi, beforeEach } from "vitest";

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

import { soundSection } from "../ui/settings/sound";
import { tts } from "../ext/tts";
import { provideVoiceControls } from "../io/voiceControls";
import { ambient } from "../io/AmbientManager";
import { isMuted, setMuted } from "../io/soundMute";
import { emit, listen } from "../events";

/** The section drawn as Settings draws it. */
const draw = (): HTMLElement => soundSection().build();

/** Press the beep or TTS toggle. */
const toggle = (root: HTMLElement, label: string): void =>
    [...root.querySelectorAll<HTMLButtonElement>(".af-audio-toggle")]
        .find(b => b.textContent?.includes(label))!
        .click();

describe("Settings → Sound", () => {
    beforeEach(() => {
        provideVoiceControls(tts as never);
        document.body.innerHTML = "";
        vi.clearAllMocks();
    });

    it("renders beep/TTS toggles and the voice list (names cleaned up)", () => {
        const pop = draw();
        expect(pop.querySelectorAll(".af-audio-toggle").length).toBe(2);
        const opts = [...pop.querySelectorAll<HTMLOptionElement>(".af-audio-select option")];
        // placeholder + 2 voices
        expect(opts.length).toBe(3);
        expect(opts.some(o => o.textContent === "Aria")).toBe(true); // "Microsoft …" and " Online…" stripped
    });

    it("says the default voice is in use, not that voices are loading, once they are in", () => {
        const pop = draw();
        const sel = pop.querySelector<HTMLSelectElement>(".af-audio-select")!;
        expect(sel.value).toBe("");
        expect(sel.selectedOptions[0]!.textContent).toBe("Default voice");
    });

    it("says voices are loading while there are none yet, and stops saying it when they come", () => {
        const loaded = tts.voices;
        (tts as { voices: unknown[] }).voices = [];
        const pop = draw();
        const sel = pop.querySelector<HTMLSelectElement>(".af-audio-select")!;
        expect(sel.selectedOptions[0]!.textContent).toBe("— loading voices… —");

        (tts as { voices: unknown[] }).voices = loaded;
        emit("tts-voices-loaded", { voices: loaded as never });

        expect(sel.selectedOptions[0]!.textContent).toBe("Default voice");
        expect(sel.options).toHaveLength(3);
    });

    it("beep toggle calls tts.toggleBeep", () => {
        const pop = draw();
        const beep = [...pop.querySelectorAll<HTMLButtonElement>(".af-audio-toggle")].find(b =>
            b.textContent?.includes("Beep"),
        )!;
        beep.click();
        expect(tts.toggleBeep).toHaveBeenCalled();
    });

    it("TTS toggle reveals the voice row", () => {
        const pop = draw();
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
        const pop = draw();
        const sel = pop.querySelector<HTMLSelectElement>(".af-audio-select")!;
        sel.value = "Google US";
        sel.dispatchEvent(new Event("change"));
        expect(tts.setVoice).toHaveBeenCalledWith("Google US");
    });

    it("re-populates the voice list on tts-voices-loaded and applies the saved voice", () => {
        (tts as { selectedVoice: string }).selectedVoice = "Google US";
        const pop = draw();
        const sel = pop.querySelector<HTMLSelectElement>(".af-audio-select")!;
        // Browser fires this once its async voice list resolves; populateVoices
        // re-runs, clearing + rebuilding the options and applying the saved voice.
        document.dispatchEvent(new Event("tts-voices-loaded"));
        expect([...sel.options].filter(o => o.value).length).toBe(2);
        expect(sel.value).toBe("Google US");
        (tts as { selectedVoice: string }).selectedVoice = "";
    });

    it("selecting an ambient track sets it and reveals the volume row", () => {
        const pop = draw();
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
        const pop = draw();
        const slider = pop.querySelector<HTMLInputElement>(".af-audio-slider")!;
        slider.value = "0.8";
        slider.dispatchEvent(new Event("input"));
        expect(ambient.setVolume).toHaveBeenCalledWith(0.8);
    });

    it("lets go of the voice list when the section is released", () => {
        const removed = vi.spyOn(document, "removeEventListener");
        const section = soundSection();
        section.build();
        section.release!();
        expect(removed.mock.calls.some(c => c[0] === "tts-voices-loaded")).toBe(true);
        removed.mockRestore();
    });

    it("follows the voice list once however often it is drawn", () => {
        const added = vi.spyOn(document, "addEventListener");
        const removed = vi.spyOn(document, "removeEventListener");
        const live = () =>
            added.mock.calls.filter(c => c[0] === "tts-voices-loaded").length -
            removed.mock.calls.filter(c => c[0] === "tts-voices-loaded").length;
        const section = soundSection();
        section.build();
        section.build();
        expect(live()).toBe(1);
        section.release!();
        expect(live()).toBe(0);
        added.mockRestore();
        removed.mockRestore();
    });
});

describe("Settings → Sound and the mute", () => {
    beforeEach(() => {
        provideVoiceControls(tts as never);
        localStorage.clear();
        vi.clearAllMocks();
    });

    const pressTrack = (root: HTMLElement, label: string): void =>
        [...root.querySelectorAll<HTMLButtonElement>(".af-audio-track-btn")]
            .find(b => b.textContent === label)!
            .click();

    it("ends a mute when a sound is turned on: that is asking to hear it", () => {
        setMuted(true);
        const root = draw();

        pressTrack(root, "Rain");

        expect(isMuted()).toBe(false);
        expect(ambient.setTrack).toHaveBeenCalledWith("rain");
    });

    it("keeps a mute when a sound is turned off", () => {
        setMuted(true);
        vi.mocked(tts.toggleBeep).mockReturnValueOnce(false);
        const root = draw();

        pressTrack(root, "Off");
        toggle(root, "Beep");

        expect(isMuted()).toBe(true);
    });

    it("says a sound choice changed, so the speaker can show it", () => {
        const heard = vi.fn();
        const stop = listen("af-sound-changed", heard);
        const root = draw();

        toggle(root, "TTS");
        pressTrack(root, "Off");

        expect(heard).toHaveBeenCalledTimes(2);
        stop();
    });

    it("is kept in this browser", () => {
        expect(soundSection().scope).toBe("browser");
    });
});
