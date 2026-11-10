/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Settings → Sound: the notification beep, replies read aloud and their voice,
 * and the ambient track.
 *
 * The header's speaker mutes and unmutes over these choices without changing
 * them. Turning a sound on here is asking to hear it, so it ends a mute: a
 * track chosen in silence would leave nobody knowing why it does not play.
 */
import { emit, listen } from "../../events";
import { ambient, AMBIENT_TRACKS } from "../../io/AmbientManager";
import { setMuted } from "../../io/soundMute";
import { voiceControls } from "../../io/voiceControls";
import { button, el, named, option } from "../dom";
import type { SettingsSection } from "./section";

/** A sound choice changed: say so, and end a mute when it turned a sound on. */
function chose(turnedOn: boolean): void {
    if (turnedOn) {
        setMuted(false);
    }
    emit("af-sound-changed");
}

/** Beep + TTS toggle buttons; the TTS toggle shows/hides `voiceRow`. */
function buildAudioToggles(voiceRow: HTMLElement): HTMLElement {
    const toggleRow = el("div", "af-audio-row");

    const beepBtn = button(`af-audio-toggle${voiceControls().beepEnabled ? " on" : ""}`, "🔔 Beep");
    beepBtn.title = "Notification beep";
    beepBtn.addEventListener("click", () => {
        const on = voiceControls().toggleBeep();
        beepBtn.classList.toggle("on", on);
        chose(on);
    });

    const ttsBtn = button(`af-audio-toggle${voiceControls().ttsEnabled ? " on" : ""}`, "🗣 TTS");
    ttsBtn.title = "Read replies aloud";
    ttsBtn.addEventListener("click", () => {
        const on = voiceControls().toggleTTS();
        ttsBtn.classList.toggle("on", on);
        voiceRow.style.display = on ? "" : "none";
        chose(on);
    });

    toggleRow.append(beepBtn, ttsBtn);
    return toggleRow;
}

/** Voice-select row, repopulated when the voice list loads. Returns the row plus
 *  a `release` that unhooks the `tts-voices-loaded` listener, so a section drawn
 *  again does not leave one behind mutating a detached select. */
function buildVoiceRow(): { row: HTMLElement; release: () => void } {
    const voiceRow = el("div", "af-audio-row");
    voiceRow.style.display = voiceControls().ttsEnabled ? "" : "none";

    const voiceSel = named(el("select", "af-audio-select"), "tts-voice", "TTS voice", "af-tts-voice");
    voiceSel.title = "TTS voice";
    // The empty value is also "no voice chosen", so it shows for as long as none
    // is: once the voices are in, it names what it then means.
    const unchosen = option("", "— loading voices… —");
    voiceSel.appendChild(unchosen);

    const populateVoices = (): void => {
        const voices = voiceControls().voices;
        if (!voices.length) {
            return;
        }
        unchosen.textContent = "Default voice";
        while (voiceSel.options.length > 1) {
            voiceSel.remove(1);
        }
        voices.forEach(v => {
            voiceSel.appendChild(
                option(v.name, v.name.replace(/^Microsoft\s+/, "").replace(/\s+Online.*$/i, "")),
            );
        });
        const saved = voiceControls().selectedVoice;
        if (saved) {
            voiceSel.value = saved;
        }
    };

    populateVoices();
    const stopFollowing = listen("tts-voices-loaded", () => populateVoices());
    voiceSel.addEventListener("change", () => voiceControls().setVoice(voiceSel.value));

    voiceRow.appendChild(voiceSel);
    return { row: voiceRow, release: stopFollowing };
}

/** Ambient volume slider row (visibility toggled by the track buttons). */
function buildVolumeRow(): HTMLElement {
    const volRow = el("div", "af-audio-row af-audio-vol-row");
    volRow.style.display = ambient.track === "none" ? "none" : "";

    const volIcon = el("span", "", "🔉");
    volIcon.style.fontSize = "14px";

    const volSlider = named(el("input", "af-audio-slider"), "ambient-volume", "Ambient volume");
    volSlider.type = "range";
    volSlider.min = "0";
    volSlider.max = "1";
    volSlider.step = "0.05";
    volSlider.value = String(ambient.volume);
    volSlider.addEventListener("input", () => ambient.setVolume(parseFloat(volSlider.value)));

    volRow.append(volIcon, volSlider);
    return volRow;
}

/** Ambient track buttons; selecting one updates the volume row's visibility. */
function buildAmbientTracks(volRow: HTMLElement): HTMLElement {
    const trackRow = el("div", "af-audio-tracks");

    AMBIENT_TRACKS.forEach(({ id, label }) => {
        const btn = button(`af-audio-track-btn${ambient.track === id ? " on" : ""}`, label);
        btn.addEventListener("click", () => {
            trackRow.querySelectorAll(".af-audio-track-btn").forEach(b => b.classList.remove("on"));
            btn.classList.add("on");
            // The track first: unmuting then starts it, rather than the one it replaces.
            ambient.setTrack(id);
            volRow.style.display = id === "none" ? "none" : "";
            chose(id !== "none");
        });
        trackRow.appendChild(btn);
    });
    return trackRow;
}

/** Ambient track buttons + volume slider (volume hidden when track is "none"). */
function buildAmbientRows(): DocumentFragment {
    const frag = document.createDocumentFragment();
    frag.appendChild(el("div", "af-audio-label", "Ambient"));
    const volRow = buildVolumeRow();
    frag.appendChild(buildAmbientTracks(volRow));
    frag.appendChild(volRow);
    return frag;
}

/** The Sound section, which follows the voice list while it is drawn. */
export function soundSection(): SettingsSection {
    let release = (): void => {};
    return {
        key: "sound",
        label: "Sound",
        line: "Beep, replies read aloud, ambient",
        scope: "browser",
        build(): HTMLElement {
            release();
            const body = el("div", "af-settings-body af-settings-sound");
            const voice = buildVoiceRow();
            release = voice.release;
            body.append(
                buildAudioToggles(voice.row),
                voice.row,
                el("div", "af-audio-divider"),
                buildAmbientRows(),
            );
            return body;
        },
        release(): void {
            release();
            release = () => {};
        },
    };
}
