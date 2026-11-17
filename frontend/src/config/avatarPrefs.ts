/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * How this browser shows agents' avatars: where, on which disc, and how an
 * agent's state shows round it. Kept in this browser, like the other choices
 * of Settings; what is stored is read as untrusted, so a value this version
 * does not know falls back to the default.
 */
import { safeStorage } from "../safeStorage";

/** A part of the dashboard that can show avatars. */
export type AvatarPlace = "cards" | "chat" | "wiring" | "notifications";

/** Every place, in the order Settings lists them. */
export const AVATAR_PLACES: readonly AvatarPlace[] = ["cards", "chat", "wiring", "notifications"];

/**
 * How an agent's state shows on its avatar: a dot on its corner, with a ring
 * in the disc's own shade; the ring itself in the state's colour, and no dot;
 * or both, the dot and the ring in the state's colour.
 */
export type AvatarStyle = "dot" | "state" | "both";

/** Every way the state shows, in the order Settings lists them. */
export const AVATAR_STYLES: readonly AvatarStyle[] = ["dot", "state", "both"];

/** The discs Settings offers by name; any other disc is a colour, as `#rrggbb`. */
export const DISC_PRESETS = ["purple", "light", "violet", "lilac", "none"] as const;

/** One of the discs offered by name. */
export type DiscPreset = (typeof DISC_PRESETS)[number];

/** This browser's avatar choices. */
export interface AvatarPrefs {
    /** Whether avatars show at all. Off, no place shows them, and what each place was set to is kept. */
    on: boolean;
    /** Whether each place shows avatars; a place that does not looks as it does without them. */
    where: Record<AvatarPlace, boolean>;
    /** What sits behind each character: a preset's name, or a colour as `#rrggbb`. */
    disc: string;
    /** How the agent's state shows. */
    style: AvatarStyle;
}

/** Where the choices are kept, as JSON. */
export const AVATAR_PREFS_KEY = "wactorz-avatars";

/** What everyone sees before changing anything. */
export function defaultAvatarPrefs(): AvatarPrefs {
    return {
        on: true,
        where: { cards: true, chat: true, wiring: true, notifications: true },
        disc: "purple",
        style: "dot",
    };
}

/** Whether `value` is a disc there is: a preset's name or a colour. */
export function isDisc(value: unknown): value is string {
    return (
        typeof value === "string" &&
        ((DISC_PRESETS as readonly string[]).includes(value) || /^#[0-9a-fA-F]{6}$/.test(value))
    );
}

/** The choices in `raw`, each kept only when it is one there is. */
function readPrefs(raw: unknown): AvatarPrefs {
    const prefs = defaultAvatarPrefs();
    if (raw === null || typeof raw !== "object") {
        return prefs;
    }
    const stored = raw as { on?: unknown; where?: unknown; disc?: unknown; style?: unknown };
    if (typeof stored.on === "boolean") {
        prefs.on = stored.on;
    }
    const where = (stored.where !== null && typeof stored.where === "object" ? stored.where : {}) as Record<
        string,
        unknown
    >;
    for (const place of AVATAR_PLACES) {
        if (typeof where[place] === "boolean") {
            prefs.where[place] = where[place];
        }
    }
    if (isDisc(stored.disc)) {
        prefs.disc = stored.disc;
    }
    const style = AVATAR_STYLES.find(known => known === stored.style);
    if (style !== undefined) {
        prefs.style = style;
    }
    return prefs;
}

/** This browser's avatar choices, the defaults where it has made none. */
export function loadAvatarPrefs(): AvatarPrefs {
    const stored = safeStorage.get(AVATAR_PREFS_KEY);
    if (stored === null) {
        return defaultAvatarPrefs();
    }
    try {
        return readPrefs(JSON.parse(stored));
    } catch {
        return defaultAvatarPrefs();
    }
}

/** Keep `prefs` for this browser. */
export function saveAvatarPrefs(prefs: AvatarPrefs): void {
    safeStorage.set(AVATAR_PREFS_KEY, JSON.stringify(prefs));
}
