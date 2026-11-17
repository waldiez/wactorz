/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Settings → Avatars: where agents' avatars show, what sits behind each
 * character, and how an agent's state shows on it.
 *
 * Each choice is kept for this browser and put on the page at once: the
 * sample at the top of the section is drawn by the same styles as every
 * avatar, so it shows what was just chosen.
 */
import {
    AVATAR_PLACES,
    AVATAR_STYLES,
    DISC_PRESETS,
    loadAvatarPrefs,
    saveAvatarPrefs,
    type AvatarPlace,
    type AvatarPrefs,
    type AvatarStyle,
} from "../../config/avatarPrefs";
import type { SettingsSection } from "../../types/settings";
import { applyAvatarPrefs, avatar, discColour, type Mood } from "../avatars";
import { el, named } from "../dom";
import { choiceGroup, settingGroup, settingRow, settingSwitch } from "./layout";

/** What each place is called, and what its switch says it does. */
const PLACES: Record<AvatarPlace, { label: string; hint: string }> = {
    cards: { label: "Overview cards", hint: "Beside each agent's name, with its state on the corner." },
    chat: { label: "Chat", hint: "In the agent list, the header and before each reply." },
    wiring: { label: "Wiring", hint: "On each agent's box and in its details." },
    notifications: { label: "Notifications", hint: "On a reply's or a question's notice, and in the Inbox." },
};

/** What each disc offered by name is called. */
const DISC_LABELS: Record<(typeof DISC_PRESETS)[number], string> = {
    purple: "Purple",
    light: "Light",
    violet: "Violet",
    lilac: "Lilac",
    none: "None",
};

/** What each way of showing the state is called. */
const STYLE_LABELS: Record<AvatarStyle, string> = { dot: "Dot", state: "Ring", both: "Both" };

/** The moods the sample shows, with the word under each and its dot's colour. */
const SAMPLE: readonly { mood: Mood; word: string; dot: string }[] = [
    { mood: "running", word: "running", dot: "#34d399" },
    { mood: "initializing", word: "starting", dot: "#60a5fa" },
    { mood: "late", word: "late", dot: "#fbbf24" },
    { mood: "stopped", word: "stopped", dot: "#4b5563" },
    { mood: "failed", word: "failed", dot: "#f87171" },
];

/** Keep `prefs` and show the page by them. */
function chose(prefs: AvatarPrefs): void {
    saveAvatarPrefs(prefs);
    applyAvatarPrefs(prefs);
}

/** One agent in each mood, drawn as the choices have it. Shown, not read out: the words are under each. */
function buildSample(): HTMLElement {
    const sample = el("div", "af-avatar-sample");
    for (const { mood, word, dot } of SAMPLE) {
        const face = el("span", "af-avatar-sample-face");
        const state = el("span", "af-avatar-sample-dot");
        state.style.background = dot;
        state.setAttribute("aria-hidden", "true");
        face.append(avatar({ name: "sample", avatar: { kind: "function" } }, mood, "card"), state);
        const item = el("div", "af-avatar-sample-item");
        item.append(face, el("span", "", word));
        sample.appendChild(item);
    }
    return sample;
}

/** One switch for all of them, and a switch per place, which wait while avatars are off. */
function buildWhere(prefs: AvatarPrefs): HTMLElement {
    const places = AVATAR_PLACES.map(place => {
        const { label } = PLACES[place];
        const box = settingSwitch(`avatars-${place}`, label, prefs.where[place], on => {
            prefs.where[place] = on;
            chose(prefs);
        });
        box.disabled = !prefs.on;
        return box;
    });
    const all = settingSwitch("avatars-on", "Show avatars", prefs.on, on => {
        prefs.on = on;
        places.forEach(box => (box.disabled = !on));
        chose(prefs);
    });
    return settingGroup(
        "Where",
        settingRow("Show avatars", "Off, no place shows them; what each place is set to is kept.", all),
        ...AVATAR_PLACES.map((place, i) => settingRow(PLACES[place].label, PLACES[place].hint, places[i]!)),
    );
}

/** The discs by name, and a colour of one's own beside them. */
function buildDisc(prefs: AvatarPrefs): HTMLElement {
    const isPreset = (DISC_PRESETS as readonly string[]).includes(prefs.disc);
    const colour = named(el("input", "af-avatar-colour"), "avatars-colour", "Any colour");
    colour.type = "color";
    colour.value = discColour(prefs.disc) ?? "#cb95fa";
    colour.classList.toggle("chosen", !isPreset);
    const presets = choiceGroup(
        "avatars-disc",
        "Disc",
        DISC_PRESETS.map(value => ({ value, label: DISC_LABELS[value] })),
        isPreset ? prefs.disc : "",
        value => {
            prefs.disc = value;
            colour.classList.remove("chosen");
            chose(prefs);
        },
    );
    colour.addEventListener("input", () => {
        prefs.disc = colour.value;
        colour.classList.add("chosen");
        // A colour of one's own is none of the named ones.
        presets.querySelectorAll<HTMLInputElement>("input").forEach(radio => (radio.checked = false));
        chose(prefs);
    });
    const control = el("div", "af-avatar-disc");
    control.append(presets, colour);
    return settingRow(
        "Disc",
        "What sits behind each character: one of these, any colour, or nothing.",
        control,
        { describes: presets },
    );
}

/** How the state shows. */
function buildStyle(prefs: AvatarPrefs): HTMLElement {
    const styles = choiceGroup(
        "avatars-style",
        "State",
        AVATAR_STYLES.map(value => ({ value, label: STYLE_LABELS[value] })),
        prefs.style,
        value => {
            prefs.style = AVATAR_STYLES.find(style => style === value) ?? "dot";
            chose(prefs);
        },
    );
    return settingRow(
        "State",
        "A dot on the avatar's corner, the ring round the disc in the state's colour, or both.",
        styles,
    );
}

/** The Avatars section. */
export function avatarsSection(): SettingsSection {
    return {
        key: "avatars",
        label: "Avatars",
        line: "On or off, where they show, the disc, the state",
        scope: "browser",
        icon: "smile",
        build(): HTMLElement {
            const prefs = loadAvatarPrefs();
            const body = el("div", "af-settings-body");
            body.append(
                buildSample(),
                buildWhere(prefs),
                settingGroup("Look", buildDisc(prefs), buildStyle(prefs)),
            );
            return body;
        },
    };
}
