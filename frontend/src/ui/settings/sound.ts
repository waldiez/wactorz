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
import type { SettingsSection } from "../../types/settings";
import { el, named, option } from "../dom";
import { choiceGroup, settingGroup, settingRow, settingSwitch } from "./layout";

/** A sound choice changed: say so, and end a mute when it turned a sound on. */
function chose(turnedOn: boolean): void {
    if (turnedOn) {
        setMuted(false);
    }
    emit("af-sound-changed");
}

/**
 * A switch over one of the voice's toggles. The switch shows what the toggle
 * answers, which is not always what was asked: with nothing that speaks
 * installed, there is nothing to turn on.
 */
function toggleSwitch(name: string, label: string, on: boolean, toggle: () => boolean): HTMLInputElement {
    const box: HTMLInputElement = settingSwitch(name, label, on, () => {
        const now = toggle();
        box.checked = now;
        chose(now);
    });
    return box;
}

/** The voice list, filled again when the voices load, and the way to stop following them. */
function buildVoiceSelect(): { select: HTMLSelectElement; release: () => void } {
    const select = named(el("select", "af-cfg-input"), "tts-voice", "Voice", "af-tts-voice");
    // The empty value is also "no voice chosen", so it shows for as long as none
    // is: once the voices are in, it names what it then means.
    const unchosen = option("", "Loading voices…");
    select.appendChild(unchosen);

    const populate = (): void => {
        const voices = voiceControls().voices;
        if (!voices.length) {
            return;
        }
        unchosen.textContent = "Default voice";
        while (select.options.length > 1) {
            select.remove(1);
        }
        voices.forEach(v => {
            select.appendChild(
                option(v.name, v.name.replace(/^Microsoft\s+/, "").replace(/\s+Online.*$/i, "")),
            );
        });
        const saved = voiceControls().selectedVoice;
        if (saved) {
            select.value = saved;
        }
    };

    populate();
    const release = listen("tts-voices-loaded", () => populate());
    select.addEventListener("change", () => voiceControls().setVoice(select.value));
    return { select, release };
}

/** Beep, reading aloud, and the voice, which shows only while replies are read. */
function buildReplies(): { group: HTMLElement; release: () => void } {
    const voices = buildVoiceSelect();
    const voiceRow = settingRow("Voice", "Which voice reads the replies.", voices.select);
    voiceRow.hidden = !voiceControls().ttsEnabled;

    const beep = toggleSwitch("beep", "Beep", voiceControls().beepEnabled, () =>
        voiceControls().toggleBeep(),
    );
    const speak = toggleSwitch("tts", "Read replies aloud", voiceControls().ttsEnabled, () => {
        const on = voiceControls().toggleTTS();
        voiceRow.hidden = !on;
        return on;
    });
    const group = settingGroup(
        "Replies",
        settingRow("Beep", "A short tone when an agent's message arrives.", beep),
        settingRow("Read replies aloud", "Each reply is spoken as it arrives.", speak),
        voiceRow,
    );
    return { group, release: voices.release };
}

/** The ambient track, and its volume while one plays. */
function buildAmbient(): HTMLElement {
    const slider = named(el("input", "af-setting-slider"), "ambient-volume", "Ambient volume");
    slider.type = "range";
    slider.min = "0";
    slider.max = "1";
    slider.step = "0.05";
    slider.value = String(ambient.volume);
    slider.addEventListener("input", () => ambient.setVolume(parseFloat(slider.value)));
    const volumeRow = settingRow("Volume", "Lowered by itself while a reply is read.", slider);
    volumeRow.hidden = ambient.track === "none";

    const tracks = choiceGroup(
        "ambient-track",
        "Ambient track",
        AMBIENT_TRACKS.map(({ id, label }) => ({ value: id, label })),
        ambient.track,
        id => {
            const track = AMBIENT_TRACKS.find(t => t.id === id)?.id ?? "none";
            // The track first: unmuting then starts it, rather than the one it replaces.
            ambient.setTrack(track);
            volumeRow.hidden = track === "none";
            chose(track !== "none");
        },
    );
    return settingGroup(
        "Ambient",
        settingRow("Track", "Plays quietly in the background.", tracks),
        volumeRow,
    );
}

/** The Sound section, which follows the voice list while it is drawn. */
export function soundSection(): SettingsSection {
    let release = (): void => {};
    return {
        key: "sound",
        label: "Sound",
        line: "Beep, replies read aloud, ambient",
        scope: "browser",
        icon: "volume",
        build(): HTMLElement {
            release();
            const body = el("div", "af-settings-body");
            const replies = buildReplies();
            release = replies.release;
            body.append(replies.group, buildAmbient());
            return body;
        },
        release(): void {
            release();
            release = () => {};
        },
    };
}
