/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * This browser's avatar choices, put on the page.
 *
 * Every place builds its avatars whether or not it shows them; the choices are
 * written on the page's root as attributes and two colours, and the styles do
 * the rest. So a choice made in Settings shows everywhere at once, with
 * nothing drawn again.
 */
import { AVATAR_PLACES, type AvatarPrefs, type DiscPreset } from "../../config/avatarPrefs";
import { isColour, luminance, shadeOf, tint } from "./svg";

/** A disc offered by name: the colour it is, and how it is filled. */
const DISCS: Record<Exclude<DiscPreset, "none">, { colour: string; fill: string }> = {
    purple: { colour: "#cb95fa", fill: "radial-gradient(circle at 50% 40%,#dcb8fc,#b47ef0)" },
    light: { colour: "#e1e6f3", fill: "radial-gradient(circle at 50% 40%,#f4f6fc,#cfd6ea)" },
    violet: { colour: "#a78bfa", fill: "radial-gradient(circle at 50% 40%,#d4c8fe,#a78bfa)" },
    lilac: { colour: "#e9ddfb", fill: "radial-gradient(circle at 50% 40%,#efe4fe,#cdb4f7)" },
};

/** Below this a disc reads as dark, and a darker ring round it would not show. */
const DARK_DISC = 0.2;

/** How a disc is drawn: its fill and the colour of the ring round it. */
export interface DiscPaint {
    /** The disc's background. */
    fill: string;
    /** The ring: a darker shade of the disc, or a lighter one round a dark disc. */
    ring: string;
}

/** The ring round a disc of `colour`. */
export function ringColour(colour: string): string {
    return luminance(colour) > DARK_DISC ? shadeOf(colour, 0.45) : tint(colour, 0.45);
}

/** How the disc `disc` names is drawn; null for none, and for a name that is not a disc. */
export function discPaint(disc: string): DiscPaint | null {
    if (isColour(disc)) {
        return {
            fill: `radial-gradient(circle at 50% 40%,${tint(disc, 0.3)},${disc})`,
            ring: ringColour(disc),
        };
    }
    if (disc !== "none" && Object.hasOwn(DISCS, disc)) {
        const preset = DISCS[disc as keyof typeof DISCS];
        return { fill: preset.fill, ring: ringColour(preset.colour) };
    }
    return null;
}

/** The colour a preset disc is, for showing it in Settings; null for none. */
export function discColour(disc: string): string | null {
    if (isColour(disc)) {
        return disc;
    }
    return disc !== "none" && Object.hasOwn(DISCS, disc) ? DISCS[disc as keyof typeof DISCS].colour : null;
}

/** Show avatars on `root`'s page as `prefs` says. */
export function applyAvatarPrefs(prefs: AvatarPrefs, root: HTMLElement = document.documentElement): void {
    root.dataset["avatars"] = AVATAR_PLACES.filter(place => prefs.on && prefs.where[place]).join(" ");
    root.dataset["avatarStyle"] = prefs.style;
    const paint = discPaint(prefs.disc);
    root.dataset["avatarDisc"] = paint ? "on" : "none";
    if (paint) {
        root.style.setProperty("--af-avatar-disc", paint.fill);
        root.style.setProperty("--af-avatar-ring", paint.ring);
    } else {
        root.style.removeProperty("--af-avatar-disc");
        root.style.removeProperty("--af-avatar-ring");
    }
}
