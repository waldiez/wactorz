/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * What every part of an agent's character is drawn with: the palette, one
 * outline weight, and the shades of an accent colour.
 *
 * A character is an SVG written as text and shown as an image. Nothing a
 * server sends is written into it as it came: a colour is checked to be one
 * (`isColour`) and every other choice is looked up among the parts this folder
 * draws, so the text is always this folder's own.
 */

/** The outline and every dark fill. */
export const INK = "#16204a";
/** The face, the shirt and whatever else is white. */
export const SKIN = "#ffffff";
/** The warm grey under the chin and on the sleeves. */
export const SHADE = "#e6e3d2";
/** Buttons, a captain's badge, a hard hat. */
export const GOLD = "#f5b82e";
/** The outline's weight, in the drawing's units. */
export const SW = 3.6;
/** The head is drawn this much larger than its coordinates. */
export const HEAD_K = 1.1;

/** Whether `value` is a colour as the drawing writes one: `#rrggbb`. */
export function isColour(value: unknown): value is string {
    return typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value);
}

type Rgb = [number, number, number];

function channels(hex: string): Rgb {
    return [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16)) as Rgb;
}

function toHex(rgb: Rgb): string {
    return `#${rgb.map(c => Math.round(c).toString(16).padStart(2, "0")).join("")}`;
}

/** `hex` mixed toward white by `k`, from 0 (itself) to 1 (white). */
export function tint(hex: string, k: number): string {
    return toHex(channels(hex).map(c => c + (255 - c) * k) as Rgb);
}

/** `hex` mixed toward black by `k`, from 0 (itself) to 1 (black). */
export function shadeOf(hex: string, k: number): string {
    return toHex(channels(hex).map(c => c * (1 - k)) as Rgb);
}

/** How light a colour reads, from 0 (black) to 1 (white): its relative luminance. */
export function luminance(hex: string): number {
    const [r, g, b] = channels(hex).map(c => {
        const v = c / 255;
        return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    }) as Rgb;
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Hue, lightness and saturation of a colour, each from 0 to 1. */
function toHls([r, g, b]: Rgb): Rgb {
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const light = (max + min) / 2;
    if (max === min) {
        return [0, light, 0];
    }
    const range = max - min;
    const saturation = light <= 0.5 ? range / (max + min) : range / (2 - max - min);
    const [rc, gc, bc] = [r, g, b].map(c => (max - c) / range) as Rgb;
    const hue = r === max ? bc - gc : g === max ? 2 + rc - bc : 4 + gc - rc;
    return [(((hue / 6) % 1) + 1) % 1, light, saturation];
}

function hueChannel(m1: number, m2: number, hue: number): number {
    const h = ((hue % 1) + 1) % 1;
    if (h < 1 / 6) {
        return m1 + (m2 - m1) * h * 6;
    }
    if (h < 0.5) {
        return m2;
    }
    return h < 2 / 3 ? m1 + (m2 - m1) * (2 / 3 - h) * 6 : m1;
}

function fromHls([hue, light, saturation]: Rgb): Rgb {
    if (saturation === 0) {
        return [light, light, light];
    }
    const m2 = light <= 0.5 ? light * (1 + saturation) : light + saturation - light * saturation;
    const m1 = 2 * light - m2;
    return [hueChannel(m1, m2, hue + 1 / 3), hueChannel(m1, m2, hue), hueChannel(m1, m2, hue - 1 / 3)];
}

/** `hex` with its lightness moved by `lighter` and its saturation scaled by `richer`. */
function relight(hex: string, lighter: number, richer: number): string {
    const [hue, light, saturation] = toHls(channels(hex).map(c => c / 255) as Rgb);
    const lit = Math.min(0.82, Math.max(0, light + lighter));
    const rgb = fromHls([hue, lit, Math.min(1, saturation * richer)]);
    return toHex(rgb.map(c => c * 255) as Rgb);
}

/** The vivid light band of an accent: mid-ear, the stripe in the tuft, the lens glint. */
export function bright(hex: string): string {
    return relight(hex, 0.1, 1.3);
}

/** The darker tone of an accent, at the bottom of an ear. */
export function deep(hex: string): string {
    return relight(hex, -0.12, 1.15);
}

/** The outline's attributes at weight `w`, for a part drawn at scale `k`, so every line stays one weight. */
export function stroke(w: number = SW, k = 1): string {
    return `stroke="${INK}" stroke-width="${(w / k).toFixed(2)}" stroke-linecap="round" stroke-linejoin="round"`;
}

/** The outline's attributes for a part of the head, which is drawn larger than its coordinates. */
export function headStroke(w: number = SW): string {
    return stroke(w, HEAD_K);
}

/** A path twice: as written, and flipped about the drawing's centre line. */
export function mirrored(d: string): string {
    return `<path d="${d}"/><path d="${d}" transform="translate(128 0) scale(-1 1)"/>`;
}
