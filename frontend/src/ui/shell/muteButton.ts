/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The header's speaker: one click to silence this browser, another to hear it
 * again, as Settings → Sound has it.
 *
 * Where nothing in Settings → Sound makes sound, muting would silence nothing
 * and unmuting would bring nothing back; the speaker then opens Settings →
 * Sound instead, the only place a sound can be turned on.
 */
import { ambient } from "../../io/AmbientManager";
import { isMuted, setMuted } from "../../io/soundMute";
import { voiceControls } from "../../io/voiceControls";
import { formatRoute } from "../../router";
import { iconButton } from "../dom";
import { iconMarkup, type IconName } from "./icons";

/** What the speaker shows: sound that plays, sound that is muted, or none chosen. */
export type SoundState = "sounding" | "muted" | "silent";

/** Whether this browser makes sound now, is muted, or has no sound chosen at all. */
export function soundState(): SoundState {
    const voice = voiceControls();
    if (!voice.beepEnabled && !voice.ttsEnabled && ambient.track === "none") {
        return "silent";
    }
    return isMuted() ? "muted" : "sounding";
}

/** What the speaker says, and shows, in each state. */
const LOOK: Record<SoundState, { label: string; icon: IconName }> = {
    sounding: { label: "Mute sound", icon: "volume" },
    muted: { label: "Unmute sound", icon: "volume-off" },
    silent: { label: "No sound is on: choose one in Settings", icon: "volume-none" },
};

/** Where the speaker goes when there is nothing to mute. */
const SOUND_SETTINGS = formatRoute({ view: "settings", path: ["sound"] });

/** Show the sound's state now on `btn`. */
function draw(btn: HTMLButtonElement): void {
    const { label, icon } = LOOK[soundState()];
    btn.title = label;
    btn.setAttribute("aria-label", label);
    btn.innerHTML = iconMarkup(icon);
}

/** The header's speaker, drawn from the sound's state now. */
export function buildMuteButton(): HTMLButtonElement {
    const btn = iconButton("af-view-btn af-view-btn-icon af-mute-btn", "", "");
    btn.addEventListener("click", () => {
        const state = soundState();
        if (state === "silent") {
            location.hash = SOUND_SETTINGS;
            return;
        }
        setMuted(state === "sounding");
        draw(btn);
    });
    draw(btn);
    return btn;
}

/** Show the sound's state on every speaker under `root`: it was muted, or a sound chosen. */
export function renderMuteButton(root: ParentNode): void {
    root.querySelectorAll<HTMLButtonElement>(".af-mute-btn").forEach(draw);
}
