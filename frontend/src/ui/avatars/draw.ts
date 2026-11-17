/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * An agent's character, composed from its parts into one SVG and handed out as
 * the address of an image.
 *
 * A base character plus slots: antenna, ears, body, neckwear, head, eyes,
 * glasses, mouth, hat and prop, in one accent colour. Its mood is in the eyes
 * and mouth. The image is a `data:` address, which the page's content policy
 * allows for images, so nothing is fetched and nothing is parsed as markup.
 */
import { body, hat, neckwear, type Hat, type Neckwear, type Outfit } from "./body";
import {
    antenna,
    ears,
    eyes,
    HEAD_TRANSFORM,
    mouth,
    overEyes,
    skull,
    underEyes,
    type Antenna,
    type Eyes,
    type Mouth,
    type Specs,
} from "./head";
import { prop } from "./props";

/** Everything that decides how one character is drawn. */
export interface Look {
    /** Its one colour, as `#rrggbb`: ears, tuft, lapels, neckwear and prop. */
    accent: string;
    /** What it wears. */
    outfit: Outfit;
    /** What sits at its collar. */
    neck: Neckwear;
    /** What it wears over its eyes. */
    specs: Specs;
    /** Its mouth. */
    face: Mouth;
    /** Its eyes. */
    look: Eyes;
    /** Its hat, or "" for the tuft. */
    top: Hat;
    /** Its antennae; a hat hides them. */
    ant: Antenna;
    /** What it holds, by name, or "" for nothing. */
    prop: string;
}

/** How much of the character an image shows. */
export type Crop = "full" | "head";

/** The whole drawing, and the head and shoulders of it for where a prop could not be read. */
const VIEW_BOX: Record<Crop, string> = { full: "0 0 128 128", head: "6 0 116 116" };

/** Sums of the drawing's positions carry binary noise in their last digits; two decimals are what is drawn. */
function tidy(svg: string): string {
    return svg.replace(/-?\d+\.\d{3,}/g, n => String(Math.round(parseFloat(n) * 100) / 100));
}

/** The character `look` describes, as SVG text. The head crop leaves the prop out. */
export function avatarSvg(look: Look, crop: Crop = "full"): string {
    const hatted = look.top !== "";
    const behind = antenna(hatted ? 0 : look.ant) + ears(look.accent) + skull(look.accent, hatted);
    const face =
        underEyes(look.specs, look.accent) +
        eyes(look.look) +
        overEyes(look.specs, look.accent) +
        mouth(look.face) +
        hat(look.top, look.accent);
    return tidy(
        `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${VIEW_BOX[crop]}">` +
            `<g transform="${HEAD_TRANSFORM}">${behind}</g>` +
            `<g>${body(look.outfit, look.accent)}${neckwear(look.neck, look.accent)}</g>` +
            `<g transform="${HEAD_TRANSFORM}">${face}</g>` +
            (crop === "full" ? prop(look.prop, look.accent) : "") +
            "</svg>",
    );
}

/** Drawn characters by what they were drawn from: a page shows the same few many times. */
const drawn = new Map<string, string>();

/** The address of the image of `look`: the same string for the same look, so an `<img>` given it again does not reload. */
export function avatarUrl(look: Look, crop: Crop = "full"): string {
    const key = `${crop}|${JSON.stringify(look)}`;
    let url = drawn.get(key);
    if (url === undefined) {
        url = `data:image/svg+xml,${encodeURIComponent(avatarSvg(look, crop))}`;
        drawn.set(key, url);
    }
    return url;
}
