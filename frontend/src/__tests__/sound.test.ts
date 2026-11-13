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
import { allByRole, byRole } from "./queries";

/** The section drawn as Settings draws it, on the page: a click on a control off it changes nothing. */
const draw = (): HTMLElement => document.body.appendChild(soundSection().build());

/** A switch, by its name. */
const switchOf = (root: HTMLElement, name: string): HTMLInputElement =>
    byRole(root, "switch", name) as HTMLInputElement;

/** Turn a switch over, as a click on it does. */
const flip = (root: HTMLElement, name: string): void => switchOf(root, name).click();

/** The voice list. */
const voiceList = (root: HTMLElement): HTMLSelectElement =>
    byRole(root, "combobox", "Voice") as HTMLSelectElement;

/** The row a control sits in, which is what hides. */
const rowOf = (control: HTMLElement): HTMLElement => control.closest<HTMLElement>(".af-setting-row")!;

/** Choose an ambient track by what it says. */
const pressTrack = (root: HTMLElement, label: string): void =>
    (byRole(root, "radio", label) as HTMLInputElement).click();

describe("Settings → Sound", () => {
    beforeEach(() => {
        provideVoiceControls(tts as never);
        document.body.innerHTML = "";
        vi.clearAllMocks();
    });

    it("has a switch for the beep and for reading aloud, and the voice list with names cleaned up", () => {
        const root = draw();
        expect(allByRole(root, "switch").map(b => b.getAttribute("aria-label"))).toEqual([
            "Beep",
            "Read replies aloud",
        ]);
        const opts = [...voiceList(root).options];
        // placeholder + 2 voices
        expect(opts.length).toBe(3);
        expect(opts.some(o => o.textContent === "Aria")).toBe(true); // "Microsoft …" and " Online…" stripped
    });

    it("says what each switch does, read out with it", () => {
        const root = draw();
        const beep = switchOf(root, "Beep");
        const hint = root.querySelector(`[id="${beep.getAttribute("aria-describedby")}"]`);
        expect(hint!.textContent).toBe("A short tone when an agent's message arrives.");
    });

    it("says the default voice is in use, not that voices are loading, once they are in", () => {
        const sel = voiceList(draw());
        expect(sel.value).toBe("");
        expect(sel.selectedOptions[0]!.textContent).toBe("Default voice");
    });

    it("says voices are loading while there are none yet, and stops saying it when they come", () => {
        const loaded = tts.voices;
        (tts as { voices: unknown[] }).voices = [];
        const sel = voiceList(draw());
        expect(sel.selectedOptions[0]!.textContent).toBe("Loading voices…");

        (tts as { voices: unknown[] }).voices = loaded;
        emit("tts-voices-loaded", { voices: loaded as never });

        expect(sel.selectedOptions[0]!.textContent).toBe("Default voice");
        expect(sel.options).toHaveLength(3);
    });

    it("turns the beep over", () => {
        const root = draw();
        flip(root, "Beep");
        expect(tts.toggleBeep).toHaveBeenCalled();
        expect(switchOf(root, "Beep").checked).toBe(true);
    });

    it("shows what the voice answers, which is off with nothing installed that speaks", () => {
        vi.mocked(tts.toggleBeep).mockReturnValueOnce(false);
        const root = draw();
        flip(root, "Beep");
        expect(switchOf(root, "Beep").checked).toBe(false);
    });

    it("shows the voice only while replies are read aloud", () => {
        const root = draw();
        const row = rowOf(voiceList(root));
        expect(row.hidden).toBe(true); // ttsEnabled=false initially
        flip(root, "Read replies aloud");
        expect(tts.toggleTTS).toHaveBeenCalled();
        expect(row.hidden).toBe(false); // shown (toggleTTS → true)
    });

    it("voice select change sets the voice", () => {
        const sel = voiceList(draw());
        sel.value = "Google US";
        sel.dispatchEvent(new Event("change"));
        expect(tts.setVoice).toHaveBeenCalledWith("Google US");
    });

    it("re-populates the voice list on tts-voices-loaded and applies the saved voice", () => {
        (tts as { selectedVoice: string }).selectedVoice = "Google US";
        const sel = voiceList(draw());
        // Browser fires this once its async voice list resolves; populateVoices
        // re-runs, clearing + rebuilding the options and applying the saved voice.
        document.dispatchEvent(new Event("tts-voices-loaded"));
        expect([...sel.options].filter(o => o.value).length).toBe(2);
        expect(sel.value).toBe("Google US");
        (tts as { selectedVoice: string }).selectedVoice = "";
    });

    it("offers the ambient tracks as one choice, and shows the volume once one plays", () => {
        const root = draw();
        const group = byRole(root, "radiogroup", "Ambient track");
        expect(allByRole(group, "radio").map(r => (r as HTMLInputElement).checked)).toEqual([true, false]);
        const volume = rowOf(byRole(root, "slider", "Ambient volume"));
        expect(volume.hidden).toBe(true); // track is "none"

        pressTrack(root, "Rain");

        expect(ambient.setTrack).toHaveBeenCalledWith("rain");
        expect(volume.hidden).toBe(false);
    });

    it("volume slider sets ambient volume", () => {
        const slider = byRole(draw(), "slider", "Ambient volume") as HTMLInputElement;
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

        pressTrack(root, "Rain");
        setMuted(true);
        pressTrack(root, "Off");
        flip(root, "Beep");

        expect(isMuted()).toBe(true);
    });

    it("says a sound choice changed, so the speaker can show it", () => {
        const heard = vi.fn();
        const stop = listen("af-sound-changed", heard);
        const root = draw();

        flip(root, "Read replies aloud");
        pressTrack(root, "Rain");

        expect(heard).toHaveBeenCalledTimes(2);
        stop();
    });

    it("is kept in this browser", () => {
        expect(soundSection().scope).toBe("browser");
    });
});
