/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The head of an agent's character, part by part: antenna, ears, skull and
 * tuft behind the face; lenses, eyes, frames and mouth on it.
 *
 * Proportions follow the original Waldiez characters: a wide head, big lenses
 * with white space round the pupils, and ears behind the head at eye level.
 * Every position is in the drawing's own units, a 128 square.
 */
import { bright, deep, GOLD, headStroke as stroke, INK, SHADE, SKIN, SW, tint } from "./svg";

/** Which antennae a character has: none, one from the crown, a pair, or one beside an ear. */
export type Antenna = 0 | 1 | 2 | "left" | "right";
/** What a character wears over its eyes. */
export type Specs = "round" | "square" | "shades" | "monocle" | "none";
/** How a character's eyes look, which is how its mood shows. */
export type Eyes = "awake" | "happy" | "thinking" | "busy" | "asleep" | "crashed";
/** A character's mouth, and what grows round it. */
export type Mouth = "smile" | "blush" | "beard" | "mustache" | "open";

// The head ellipse.
const HX = 64;
const HY = 55;
const HRX = 35;
const HRY = 33;
// Lens centres, the eye line and the lens radius; pupils sit a touch above the lens centre.
const LX = 48.5;
const RX = 79.5;
const EY = 56;
const LR = 12;
const PY = EY - 0.9;
const PR = 5.1;
/** Frames are a little lighter than the head's outline. */
const FRAME = 3.2;
// The mouth: its corners, the corner line, its depth and its weight.
const MX0 = 58.2;
const MX1 = 69.8;
const MY = 72.7;
const MD = 3.2;
const MSW = 4.3;
// Square lenses: nearly square, the bridge at mid-height.
const SQ_W = 23;
const SQ_H = 22.5;
const SQ_Y = EY - 0.5;
// Ears: centred just outside the head's edge, on the eye line.
const EAR_DX = 35.2;
const EAR_Y = 57.6;
const EAR_RX = 10.4;
const EAR_RY = 10.6;

/** The transform that draws the head's parts at their size. */
export const HEAD_TRANSFORM = `translate(${HX} 56.5) scale(1.1) translate(-${HX} -${HY})`;

const ANTENNAE: Record<string, () => string> = {
    "1": () =>
        `<path d="M${HX} 26 V13" fill="none" ${stroke(3)}/>` +
        `<circle cx="${HX}" cy="10" r="4.2" fill="${SKIN}" ${stroke(3)}/>`,
    "2": () =>
        `<path d="M48 30 L38 12 M80 30 L90 12" fill="none" ${stroke()}/>` +
        `<circle cx="37" cy="9.5" r="4.5" fill="${SKIN}" ${stroke(3)}/>` +
        `<circle cx="91" cy="9.5" r="4.5" fill="${SKIN}" ${stroke(3)}/>`,
    left: () => sideAntenna(HX - 38.5),
    right: () => sideAntenna(HX + 38.5),
};

function sideAntenna(x: number): string {
    return (
        `<path d="M${x} 52 V33" fill="none" ${stroke(3)}/>` +
        `<ellipse cx="${x}" cy="28.5" rx="4" ry="4.6" fill="${SKIN}" ${stroke(3)}/>`
    );
}

/** The antennae, drawn behind the head. */
export function antenna(which: Antenna): string {
    return ANTENNAE[String(which)]?.() ?? "";
}

/** Half-discs tucked behind the head: a white cap, a bright band, a deep base. */
export function ears(accent: string): string {
    const hi = bright(accent);
    const lo = deep(accent);
    const w = EAR_RX + 1;
    const h = EAR_RY + 1;
    return [-1, 1]
        .map(side => {
            const id = side < 0 ? "earL" : "earR";
            // Drawn in local coordinates where +x points away from the head.
            return (
                `<g transform="translate(${HX + side * EAR_DX} ${EAR_Y}) scale(${side} 1)">` +
                `<clipPath id="${id}"><ellipse rx="${EAR_RX}" ry="${EAR_RY}"/></clipPath>` +
                `<g clip-path="url(#${id})">` +
                `<rect x="${-w}" y="${-h}" width="${2 * w}" height="${2 * h}" fill="${SKIN}"/>` +
                `<path d="M${-w} -4.6 Q0 -2.2 ${w} -3.1 V${h} H${-w} Z" fill="${hi}"/>` +
                `<path d="M${-w} 3.1 Q0 2.8 ${w} 0.9 V${h} H${-w} Z" fill="${lo}"/>` +
                "</g>" +
                `<ellipse rx="${EAR_RX}" ry="${EAR_RY}" fill="none" ${stroke()}/>` +
                "</g>"
            );
        })
        .join("");
}

// The tuft is a leaf lying flat on the crown. Its top edge leaves the head's
// outline at a tangent and runs straight to a tip just past the silhouette;
// the underside hooks back down and left as a heavier, tapering line.
const TUFT_TOP = "M58 22.5 L74 19.8 Q77.6 19.2 77.3 22.6";
const TUFT_UNDER = "Q76.4 31.4 66 34.1 Q61 35.4 56.2 34.2";

function tuft(accent: string): string {
    return (
        // The white leaf also hides the stretch of head outline it lies over.
        `<path d="${TUFT_TOP} ${TUFT_UNDER} Z" fill="${SKIN}"/>` +
        `<path d="M57 31.6 Q62.5 30.6 66.5 28.8 Q72.5 26 75.6 21.6 Q75 29.4 66 32.8 Q61 34 57 31.6 Z" fill="${bright(accent)}"/>` +
        `<path d="M56.6 33.8 Q61.5 34.4 66 33.2 Q73.6 30.6 76.6 24 Q73.4 29.2 66 31.5 Q61.5 32.7 56.6 33.8 Z" fill="${INK}"/>` +
        `<path d="${TUFT_TOP} ${TUFT_UNDER}" fill="none" ${stroke()}/>`
    );
}

/** The skull, shaded under the chin, with the tuft on its crown unless a hat covers it. */
export function skull(accent: string, hatted: boolean): string {
    return (
        `<clipPath id="head"><ellipse cx="${HX}" cy="${HY}" rx="${HRX}" ry="${HRY}"/></clipPath>` +
        `<g clip-path="url(#head)">` +
        `<rect x="20" y="15" width="90" height="80" fill="${SHADE}"/>` +
        `<ellipse cx="${HX}" cy="${HY - 3.6}" rx="${HRX}" ry="${HRY - 1}" fill="${SKIN}"/>` +
        "</g>" +
        `<ellipse cx="${HX}" cy="${HY}" rx="${HRX}" ry="${HRY}" fill="none" ${stroke()}/>` +
        (hatted ? "" : tuft(accent))
    );
}

function pupils(): string {
    return (
        `<circle cx="${LX}" cy="${PY}" r="${PR}" fill="${INK}"/><circle cx="${RX}" cy="${PY}" r="${PR}" fill="${INK}"/>` +
        `<circle cx="${LX + 1.6}" cy="${PY - 1.8}" r="1.3" fill="${SKIN}"/>` +
        `<circle cx="${RX + 1.6}" cy="${PY - 1.8}" r="1.3" fill="${SKIN}"/>`
    );
}

/** Two arcs, one per eye, bowed up by `rise` (down when negative) from `dy` under the pupil line. */
function lids(dy: number, rise: number, weight: number): string {
    const arc = (x: number): string => `M${x - 5.5} ${PY + dy} Q${x} ${PY + rise} ${x + 5.5} ${PY + dy}`;
    return `<path d="${arc(LX)} ${arc(RX)}" fill="none" ${stroke(weight)}/>`;
}

const EYES: Record<Eyes, () => string> = {
    awake: pupils,
    happy: () => lids(2, -5, 2.8),
    thinking: () =>
        `<circle cx="${LX + 3}" cy="${PY - 3}" r="4.6" fill="${INK}"/><circle cx="${RX + 3}" cy="${PY - 3}" r="4.6" fill="${INK}"/>` +
        `<path d="M${LX - 9.5} ${EY - 16.5} Q${LX - 1.5} ${EY - 20.5} ${LX + 7.5} ${EY - 17.5}" fill="none" ${stroke(2.4)}/>`,
    busy: () =>
        pupils() +
        `<path d="M95 34 Q99 40 99 43 a4 4 0 0 1 -8 0 Q91 40 95 34 Z" fill="#9fdcff" ${stroke(2)}/>`,
    asleep: () => lids(1, 5, 2.6),
    crashed: () => {
        const d = 3.5;
        const cross = (x: number): string =>
            `M${x - d} ${PY - d} L${x + d} ${PY + d} M${x + d} ${PY - d} L${x - d} ${PY + d}`;
        return `<path d="${cross(LX)} ${cross(RX)}" ${stroke(2.6)}/>`;
    },
};

/** The eyes, in the mood `kind` names. */
export function eyes(kind: Eyes): string {
    return EYES[kind]();
}

/** The tint inside round lenses, which goes under the eyes; their frames go over. */
function roundLenses(accent: string): string {
    return [LX, RX]
        .map(cx => {
            const id = `lens${Math.trunc(cx)}`;
            return (
                `<clipPath id="${id}"><circle cx="${cx}" cy="${EY}" r="${LR}"/></clipPath>` +
                `<g clip-path="url(#${id})"><rect x="${cx - LR}" y="${EY}" width="${2 * LR}" height="${LR}" fill="${bright(accent)}"/>` +
                `<circle cx="${cx}" cy="${EY - 3.4}" r="${LR}" fill="${SKIN}"/></g>`
            );
        })
        .join("");
}

function roundFrames(): string {
    return (
        `<circle cx="${LX}" cy="${EY}" r="${LR}" fill="none" ${stroke(FRAME)}/>` +
        `<circle cx="${RX}" cy="${EY}" r="${LR}" fill="none" ${stroke(FRAME)}/>` +
        `<path d="M${LX - LR} ${EY - 1} L29.5 ${EY - 1} M${RX + LR} ${EY - 1} L98.5 ${EY - 1}" ${stroke(FRAME)}/>` +
        `<path d="M${LX + LR} ${EY - 1} L${RX - LR} ${EY - 1}" ${stroke(FRAME)}/>`
    );
}

function squareSpecs(accent: string): string {
    const y = SQ_Y - SQ_H / 2;
    const frame = (cx: number): string =>
        `<rect x="${cx - SQ_W / 2}" y="${y}" width="${SQ_W}" height="${SQ_H}" rx="1.8" fill="none" ${stroke()}/>`;
    const glint = [LX, RX]
        .map(cx => {
            const x = cx - SQ_W / 2;
            const id = `sq${Math.trunc(cx)}`;
            return (
                `<clipPath id="${id}"><rect x="${x}" y="${y}" width="${SQ_W}" height="${SQ_H}" rx="1.8"/></clipPath>` +
                `<g clip-path="url(#${id})"><rect x="${x}" y="${y + SQ_H - 3.4}" width="${SQ_W}" height="3.4" fill="${tint(accent, 0.45)}"/></g>`
            );
        })
        .join("");
    return (
        glint +
        frame(LX) +
        frame(RX) +
        `<path d="M${LX + SQ_W / 2} ${SQ_Y} L${RX - SQ_W / 2} ${SQ_Y} M${LX - SQ_W / 2} ${SQ_Y} L29.5 ${SQ_Y} M${RX + SQ_W / 2} ${SQ_Y} L98.5 ${SQ_Y}" ${stroke()}/>`
    );
}

function shades(): string {
    const t = EY - 7;
    const lens = (cx: number): string =>
        `M${cx - 12.5} ${t} H${cx + 12.5} V${t + 6} Q${cx + 12.5} ${t + 16} ${cx} ${t + 16} Q${cx - 12.5} ${t + 16} ${cx - 12.5} ${t + 6} Z`;
    return (
        `<path d="${lens(LX)} ${lens(RX)}" fill="${INK}" ${stroke()}/>` +
        `<path d="M${LX - 8.5} ${t + 4} L${LX - 2.5} ${t + 4} M${RX - 8.5} ${t + 4} L${RX - 2.5} ${t + 4}" stroke="${SKIN}" stroke-width="2" stroke-linecap="round" opacity=".75"/>` +
        `<path d="M${LX + 12.5} ${t + 2} L${RX - 12.5} ${t + 2} M${LX - 12.5} ${t + 2} L29.5 ${t + 3} M${RX + 12.5} ${t + 2} L98.5 ${t + 3}" ${stroke()}/>`
    );
}

function monocle(): string {
    return (
        `<circle cx="${RX}" cy="${EY}" r="${LR}" fill="none" stroke="${GOLD}" stroke-width="${SW}"/>` +
        `<circle cx="${RX}" cy="${EY}" r="${LR + 1.8}" fill="none" stroke="${INK}" stroke-width="1"/>` +
        `<path d="M${RX + 8} ${EY + 9} Q98 80 92 92" fill="none" stroke="${GOLD}" stroke-width="1.6" stroke-dasharray="1.5 2.5"/>`
    );
}

const OVER_THE_EYES: Record<Specs, (accent: string) => string> = {
    round: roundFrames,
    square: squareSpecs,
    shades,
    monocle,
    none: () => "",
};

/** What of the glasses is drawn before the eyes: the tint inside round lenses. */
export function underEyes(specs: Specs, accent: string): string {
    return specs === "round" ? roundLenses(accent) : "";
}

/** What of the glasses is drawn over the eyes: the frames, or the whole of any other kind. */
export function overEyes(specs: Specs, accent: string): string {
    return OVER_THE_EYES[specs](accent);
}

/** The smile: a short, heavy arc just under the lenses. */
function smile(): string {
    const half = (MX1 - MX0) / 2;
    const r = (half * half + MD * MD) / (2 * MD);
    return `<path d="M${MX0} ${MY} A${r.toFixed(2)} ${r.toFixed(2)} 0 0 0 ${MX1} ${MY}" fill="none" ${stroke(MSW)}/>`;
}

const MOUTHS: Record<Mouth, () => string> = {
    smile,
    blush: () =>
        smile() +
        '<ellipse cx="37" cy="71" rx="5" ry="3" fill="#ff9fb2" opacity=".7"/>' +
        '<ellipse cx="91" cy="71" rx="5" ry="3" fill="#ff9fb2" opacity=".7"/>',
    beard: () =>
        `<path d="M31 64 Q32 92 64 92 Q96 92 97 64 Q90 82 76 74 Q64 70 52 74 Q38 82 31 64 Z" fill="#c9c4b4" ${stroke(2.8)}/>` +
        `<path d="M58 78 Q64 82 70 78" fill="none" ${stroke(2.6)}/>`,
    mustache: () =>
        '<g transform="translate(0 -1.2)">' +
        `<path d="M64 74 Q57 69 50 72 Q46 75 42 72 Q45 80 53 78.5 Q60 77.5 64 75.5 ` +
        `Q68 77.5 75 78.5 Q83 80 86 72 Q82 75 78 72 Q71 69 64 74 Z" fill="${INK}"/></g>`,
    open: () => {
        const y = MY - 0.8;
        return (
            `<path d="M${MX0 - 0.9} ${y} Q64 ${y + 10} ${MX1 + 0.9} ${y} Z" fill="${INK}" ${stroke(2.6)}/>` +
            `<path d="M60.7 ${y + 5} Q64 ${y + 3.6} 67.3 ${y + 5} Q64 ${y + 7.4} 60.7 ${y + 5} Z" fill="#ef7b8b"/>`
        );
    },
};

/** The mouth, and the beard or moustache round it. */
export function mouth(kind: Mouth): string {
    return MOUTHS[kind]();
}
