/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The body of an agent's character, its neckwear and its hat.
 *
 * After the original Waldiez characters: a lens-shaped bust with pointed
 * sleeve tips, whose flat collar band sits in front of the chin.
 */
import { deep, GOLD, headStroke, INK, mirrored, SHADE, SKIN, stroke, tint } from "./svg";

/** What a character wears on its body. */
export type Outfit = "vest" | "suit" | "stripes";
/** What sits at a character's collar. */
export type Neckwear = "gem" | "tie" | "bowtie" | "none";
/** What a character wears on its head; the tuft hides under any of them. */
export type Hat = "" | "gradcap" | "hardhat" | "tophat" | "captain" | "beanie" | "headset";

/** The underside of the collar band; its top edge covers the chin. */
const BAND = 94.6;
// The garment's straight sides; sleeves show outside them.
const VEST_L = 33;
const VEST_R = 95;
const VEST_W = VEST_R - VEST_L;
const TORSO =
    "M46 91.6 H82 C90.5 91.6 103 100.4 104.6 106 Q105 108.2 106.4 109.8 " +
    "A65.4 65.4 0 0 1 21.6 109.8 Q23 108.2 23.4 106 C25 100.4 37.5 91.6 46 91.6 Z";
/** The shirt front: straight sides under the collar, then a V down to the hem. */
const SHIRT = `M49.3 ${BAND} L50.1 103.4 L52 106.6 L56.9 114.2 L64 122.4 L71.1 114.2 L76 106.6 L77.9 103.4 L78.7 ${BAND} Z`;
/** The suit's lapel line. */
const NAVY = "#2b3d76";
/** The centre line of whatever sits at the collar. */
const NECK_Y = 100.6;

const OUTFITS: Record<Outfit, (accent: string) => string> = {
    vest: accent =>
        `<rect x="${VEST_L}" y="86" width="${VEST_W}" height="44" fill="${INK}"/>` +
        // Lapel panels: lit along the top, deeper along the hem.
        `<g fill="${accent}">${mirrored("M36.9 93.8 L41.8 93.8 L48.9 111 L36.9 107.8 Z")}</g>` +
        `<g fill="${tint(accent, 0.45)}">${mirrored("M36.9 93.8 L41.8 93.8 L42.7 96 L36.9 96 Z")}</g>` +
        `<g fill="${deep(accent)}">${mirrored("M36.9 105.9 L48.1 108.9 L48.9 111 L36.9 107.8 Z")}</g>`,
    suit: () =>
        `<rect x="10" y="86" width="108" height="44" fill="${INK}"/>` +
        // Notched lapels, drawn as a lighter line.
        `<g fill="none" stroke="${NAVY}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">` +
        mirrored("M42.2 94.6 L37.4 102 L44.2 100.7 L39.7 106.8 L63 127.2") +
        "</g>",
    stripes: accent =>
        `<rect x="${VEST_L}" y="86" width="${VEST_W}" height="44" fill="${accent}"/>` +
        [99.4, 108.9, 118.4]
            .map(y => `<rect x="${VEST_L}" y="${y}" width="${VEST_W}" height="4.8" fill="#000000"/>`)
            .join("") +
        `<path d="M${VEST_L} 86 V130 M${VEST_R} 86 V130" ${stroke(2.6)}/>` +
        // The opening's dark edge, over the stripes.
        `<path d="${SHIRT}" fill="${INK}" stroke="${INK}" stroke-width="9" stroke-linejoin="miter" stroke-miterlimit="8"/>`,
};

/** The bust in its outfit: sleeves, garment, collar band, shirt front and two buttons. */
export function body(outfit: Outfit, accent: string): string {
    return (
        `<clipPath id="torso"><path d="${TORSO}"/></clipPath>` +
        `<path d="${TORSO}" fill="${SHADE}"/>` +
        '<g clip-path="url(#torso)">' +
        // White sleeves, leaving a sliver of shade along the hem.
        `<path d="${TORSO}" fill="${SKIN}" transform="translate(0 -2.2)"/>` +
        OUTFITS[outfit](accent) +
        `<rect x="${VEST_L}" y="86" width="${VEST_W}" height="${BAND - 86}" fill="${INK}"/>` +
        `<path d="${SHIRT}" fill="${SKIN}"/>` +
        `<circle cx="64" cy="109.8" r="1.8" fill="${INK}"/><circle cx="64" cy="116.2" r="1.7" fill="${INK}"/>` +
        "</g>" +
        `<path d="${TORSO}" fill="none" ${stroke()}/>`
    );
}

/** Collar points: a heavy W hanging from the band. A bow tie covers them. */
function collar(): string {
    return `<path d="M47 ${BAND} L53.7 103.8 L61.3 ${BAND} M81 ${BAND} L74.3 103.8 L66.7 ${BAND}" fill="none" ${stroke(3.7)}/>`;
}

function knot(accent: string): string {
    return (
        `<ellipse cx="64" cy="${NECK_Y}" rx="3.4" ry="3.95" fill="${accent}" ${stroke(2.7)}/>` +
        `<ellipse cx="64" cy="${NECK_Y + 1.5}" rx="2" ry="1.1" fill="${deep(accent)}"/>` +
        `<ellipse cx="63.4" cy="${NECK_Y - 1.1}" rx="1" ry="1.1" fill="${tint(accent, 0.5)}"/>`
    );
}

function bowtie(accent: string): string {
    const y = NECK_Y + 0.5;
    const wing = `M60.6 ${y - 2.7} L52.6 ${y - 5.5} Q51.2 ${y - 5.7} 51.3 ${y - 4.3} Q50.7 ${y} 51.3 ${y + 4.3} Q51.2 ${y + 5.7} 52.6 ${y + 5.5} L60.6 ${y + 2.7} Z`;
    const fold = `M58.9 ${y - 1.7} H57.5 V${y + 1.7} H58.9`;
    const under = `M52.5 ${y + 2.7} L59.6 ${y + 1.4} L59.6 ${y + 2.1} L52.7 ${y + 4.3} Z`;
    return (
        `<g fill="${accent}" ${stroke(2.6)}>${mirrored(wing)}</g>` +
        `<g fill="${deep(accent)}">${mirrored(under)}</g>` +
        `<g fill="none" ${stroke(1.3)}>${mirrored(fold)}</g>` +
        `<circle cx="64" cy="${y}" r="3.2" fill="${accent}" ${stroke(2.6)}/>` +
        `<circle cx="63.3" cy="${y - 0.9}" r="1" fill="${tint(accent, 0.5)}"/>`
    );
}

const NECKWEAR: Record<Neckwear, (accent: string) => string> = {
    gem: accent => collar() + knot(accent),
    tie: accent =>
        collar() +
        `<path d="M60.6 104.4 L67.4 104.4 L68 118.6 L64 123.2 L60 118.6 Z" fill="${accent}" ${stroke(2.4)}/>` +
        `<path d="M61 118.8 L67 118.8 L64 122.2 Z" fill="${deep(accent)}"/>` +
        knot(accent),
    bowtie,
    none: collar,
};

/** What sits at the collar, over the body. */
export function neckwear(kind: Neckwear, accent: string): string {
    return NECKWEAR[kind](accent);
}

const HATS: Record<Exclude<Hat, "">, (accent: string) => string> = {
    gradcap: accent =>
        `<path d="M42 26 V36 Q64 44 86 36 V26 Z" fill="${INK}" ${headStroke()}/>` +
        `<path d="M26 22 L64 9 L102 22 L64 35 Z" fill="${INK}" ${headStroke()}/>` +
        `<path d="M64 22 L95 25 V37" fill="none" stroke="${accent}" stroke-width="2" stroke-linecap="round"/>` +
        `<circle cx="95" cy="39" r="3" fill="${accent}" ${headStroke(1.6)}/>` +
        `<circle cx="64" cy="22" r="2.4" fill="${accent}"/>`,
    hardhat: () =>
        `<path d="M30 36 Q30 9 64 9 Q98 9 98 36 Z" fill="${GOLD}" ${headStroke()}/>` +
        `<path d="M58 10 Q56 22 58 34 H70 Q72 22 70 10" fill="#ffd56b" ${headStroke(2.4)}/>` +
        `<rect x="23" y="32" width="82" height="8" rx="4" fill="${GOLD}" ${headStroke()}/>`,
    tophat: accent =>
        `<path d="M45 30 L47 2 H81 L83 30 Z" fill="${INK}" ${headStroke()}/>` +
        `<path d="M46 21 H82 L82.6 28 H45.4 Z" fill="${accent}"/>` +
        `<ellipse cx="64" cy="31" rx="31" ry="5" fill="${INK}" ${headStroke()}/>`,
    captain: () =>
        `<path d="M30 30 Q22 11 64 7 Q106 11 98 30 Z" fill="${SKIN}" ${headStroke()}/>` +
        `<path d="M34 23 Q64 18 94 23" fill="none" stroke="${SHADE}" stroke-width="2"/>` +
        `<path d="M31 29 H97 Q97 38 91 38 H37 Q31 38 31 29 Z" fill="${INK}" ${headStroke()}/>` +
        `<path d="M38 33.5 H90" stroke="${GOLD}" stroke-width="2.2"/>` +
        `<circle cx="64" cy="25" r="5.5" fill="${GOLD}" ${headStroke(2.6)}/>` +
        `<path d="M36 38 Q64 46 92 38" fill="#2c3870" ${headStroke(2.6)}/>`,
    beanie: accent => {
        const light = tint(accent, 0.35);
        return (
            `<path d="M30 38 Q29 10 64 9 Q99 10 98 38 Z" fill="${accent}" ${headStroke()}/>` +
            `<path d="M48 14 V34 M64 11 V34 M80 14 V34" stroke="${light}" stroke-width="2"/>` +
            `<rect x="27.5" y="31" width="73" height="10" rx="5" fill="${light}" ${headStroke()}/>` +
            `<circle cx="64" cy="9" r="5" fill="${SKIN}" ${headStroke(3)}/>`
        );
    },
    headset: accent =>
        `<path d="M25 47 Q24 13 64 13 Q104 13 103 47" fill="none" ${headStroke(4.5)}/>` +
        `<path d="M23 64 Q25 80 47 80" fill="none" ${headStroke(2.6)}/>` +
        `<circle cx="49" cy="80" r="3.2" fill="${accent}" ${headStroke(2.2)}/>`,
};

/** The hat, drawn with the head, over the face. */
export function hat(kind: Hat, accent: string): string {
    return kind ? HATS[kind](accent) : "";
}
