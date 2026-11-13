/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** The header's speaker, and the mute it turns on and off over what Settings → Sound has chosen. */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../io/AmbientManager", () => ({ ambient: { track: "none" } }));

import { ambient } from "../io/AmbientManager";
import { isMuted, MUTED_KEY, setMuted } from "../io/soundMute";
import { provideVoiceControls } from "../io/voiceControls";
import { listen } from "../events";
import { buildMuteButton, renderMuteButton, soundState } from "../ui/shell/muteButton";
import type { VoiceControls } from "../types/tts";
import { byRole } from "./queries";

/** What Settings → Sound has chosen, as the speaker reads it; the test changes it. */
const chosen: { -readonly [K in keyof VoiceControls]: VoiceControls[K] } = {
    beepEnabled: false,
    ttsEnabled: false,
    voices: [],
    selectedVoice: "",
    toggleBeep: () => false,
    toggleTTS: () => false,
    setVoice: () => {},
};

/** Choose the sounds: a beep, read-aloud, an ambient track. */
function choose(beep: boolean, speech = false, track = "none"): void {
    chosen.beepEnabled = beep;
    chosen.ttsEnabled = speech;
    (ambient as { track: string }).track = track;
}

describe("the mute", () => {
    beforeEach(() => localStorage.clear());

    it("is off until it is turned on, and kept in this browser", () => {
        expect(isMuted()).toBe(false);
        setMuted(true);
        expect([isMuted(), localStorage.getItem(MUTED_KEY)]).toEqual([true, "1"]);
        setMuted(false);
        expect([isMuted(), localStorage.getItem(MUTED_KEY)]).toEqual([false, null]);
    });

    it("says when it changes, and only then", () => {
        const heard = vi.fn();
        const stop = listen("af-mute-changed", heard);
        setMuted(true);
        setMuted(true);
        setMuted(false);
        stop();
        expect(heard.mock.calls.map(c => c[0])).toEqual([{ muted: true }, { muted: false }]);
    });
});

describe("the header's speaker", () => {
    beforeEach(() => {
        localStorage.clear();
        document.body.replaceChildren();
        provideVoiceControls(chosen);
        history.replaceState(null, "", "#/overview");
    });
    afterEach(() => choose(false));

    const speaker = (name: string): HTMLButtonElement =>
        byRole(document, "button", name) as HTMLButtonElement;

    it("mutes what plays, and unmutes it, without changing what is chosen", () => {
        choose(true, false, "rain");
        document.body.appendChild(buildMuteButton());

        speaker("Mute sound").click();
        expect(isMuted()).toBe(true);

        speaker("Unmute sound").click();
        expect(isMuted()).toBe(false);
        expect([chosen.beepEnabled, ambient.track]).toEqual([true, "rain"]);
    });

    it("opens Settings → Sound when no sound is chosen, as there is nothing to mute", () => {
        document.body.appendChild(buildMuteButton());

        speaker("No sound is on: choose one in Settings").click();

        expect(location.hash).toBe("#/settings/sound");
        expect(isMuted()).toBe(false);
    });

    it("counts every kind of sound as something to mute", () => {
        expect(soundState()).toBe("silent");
        choose(false, true);
        expect(soundState()).toBe("sounding");
        choose(false, false, "cafe");
        expect(soundState()).toBe("sounding");
        setMuted(true);
        expect(soundState()).toBe("muted");
        choose(false);
        expect(soundState()).toBe("silent");
    });

    it("shows the state again when asked: muted elsewhere, or a sound chosen", () => {
        document.body.appendChild(buildMuteButton());
        expect(speaker("No sound is on: choose one in Settings")).toBeTruthy();

        choose(true);
        renderMuteButton(document);
        expect(speaker("Mute sound").title).toBe("Mute sound");

        setMuted(true);
        renderMuteButton(document);
        expect(speaker("Unmute sound")).toBeTruthy();
    });
});
