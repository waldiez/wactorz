/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * The small thing a character holds, which names its agent's job: an envelope
 * for mail, a clock for a schedule, a gear for the installer.
 *
 * Drawn last, at the character's side, and left out at the small sizes where
 * it could not be read.
 */
import { GOLD, INK, SHADE, SKIN, stroke, tint } from "./svg";

const S2 = stroke(2);
const S26 = stroke(2.6);

/** One prop, drawn in `accent` and its lighter shade. */
type Prop = (accent: string, light: string) => string;

const CALENDAR_DAYS: [number, number, boolean][] = [
    [100, 111, false],
    [106, 111, false],
    [112, 111, true],
    [118, 111, false],
    [100, 117, false],
    [106, 117, false],
    [112, 117, false],
];

/** `part` drawn once every 45 degrees round the point (`cx`, `cy`). */
function around(part: string, cx: number, cy: number): string {
    return [0, 45, 90, 135, 180, 225, 270, 315]
        .map(angle => part.replace("/>", ` transform="rotate(${angle} ${cx} ${cy})"/>`))
        .join("");
}

const PROPS: Record<string, Prop> = {
    envelope: accent =>
        `<rect x="95" y="102" width="30" height="21" rx="3" fill="${SKIN}" ${S26}/>` +
        `<path d="M96 104 L110 115 L124 104" fill="none" ${S26}/>` +
        `<circle cx="110" cy="115" r="3" fill="${accent}" ${stroke(1.6)}/>`,
    calendar: (accent, light) =>
        `<rect x="96" y="100" width="28" height="24" rx="3.5" fill="${SKIN}" ${S26}/>` +
        `<path d="M96 107 V103.5 Q96 100 99.5 100 H120.5 Q124 100 124 103.5 V107 Z" fill="${accent}" ${S26}/>` +
        `<path d="M103 97 V102 M117 97 V102" ${S26}/>` +
        CALENDAR_DAYS.map(
            ([x, y, today]) =>
                `<rect x="${x}" y="${y}" width="4" height="3" rx="1" fill="${today ? accent : light}"/>`,
        ).join(""),
    clock: (_accent, light) =>
        `<circle cx="110" cy="111" r="13" fill="${SKIN}" ${S26}/>` +
        `<circle cx="110" cy="111" r="9.5" fill="${light}"/>` +
        `<path d="M110 111 V104 M110 111 L115.5 114" fill="none" ${S26}/>` +
        `<circle cx="110" cy="111" r="1.6" fill="${INK}"/>`,
    magnifier: (_accent, light) =>
        `<path d="M115 116 L123 124" ${stroke(5)}/>` +
        `<circle cx="108" cy="109" r="9" fill="${light}" ${S26}/>` +
        `<path d="M103 106 Q105 102 109 102" fill="none" stroke="${SKIN}" stroke-width="2" stroke-linecap="round"/>`,
    gear: accent =>
        around(`<rect x="107" y="96" width="6" height="7" rx="1.2" fill="${accent}" ${S2}/>`, 110, 111) +
        `<circle cx="110" cy="111" r="10" fill="${accent}" ${S26}/><circle cx="110" cy="111" r="3.6" fill="${SKIN}" ${S2}/>`,
    books: (accent, light) =>
        `<rect x="94" y="116" width="30" height="8" rx="2" fill="${accent}" ${S2}/>` +
        `<rect x="97" y="108" width="26" height="8" rx="2" fill="${light}" ${S2}/>` +
        `<rect x="95" y="100" width="27" height="8" rx="2" fill="#ef5b6b" ${S2}/>` +
        `<path d="M98 104 H112 M101 112 H115 M98 120 H110" stroke="${SKIN}" stroke-width="1.6" stroke-linecap="round"/>`,
    book: (accent, light) =>
        `<path d="M95 104 Q102.5 100 110 104 Q117.5 100 125 104 V123 Q117.5 119 110 123 Q102.5 119 95 123 Z" fill="${SKIN}" ${S26}/>` +
        `<path d="M110 104 V123" ${stroke(2.2)}/>` +
        `<path d="M99 108.5 Q102.5 107 106 108.5 M99 113.5 Q102.5 112 106 113.5 M114 108.5 Q117.5 107 121 108.5 M114 113.5 Q117.5 112 121 113.5" fill="none" stroke="${accent}" stroke-width="1.8" stroke-linecap="round"/>` +
        `<path d="M116 100 V107 L118.5 105 L121 107 V101" fill="${light}" ${stroke(1.4)}/>`,
    slides: (accent, light) =>
        `<path d="M100 116 L96 126 M120 116 L124 126 M110 116 V124" ${S26}/>` +
        `<rect x="94" y="96" width="32" height="21" rx="2.5" fill="${SKIN}" ${S26}/>` +
        `<rect x="99" y="106" width="4" height="7" fill="${accent}"/><rect x="105" y="102" width="4" height="11" fill="${accent}"/>` +
        `<rect x="111" y="108" width="4" height="5" fill="${light}"/><rect x="117" y="100" width="4" height="13" fill="${accent}"/>`,
    button: (accent, light) =>
        `<path d="M95 98 Q92 102 95 106 M125 98 Q128 102 125 106" fill="none" ${S2}/>` +
        `<circle cx="110" cy="112" r="12" fill="${SKIN}" ${S26}/>` +
        `<circle cx="110" cy="112" r="7" fill="${accent}" ${S2}/>` +
        `<circle cx="108" cy="110" r="2" fill="${light}"/>`,
    mic: (accent, light) =>
        `<path d="M101 107 Q101 118 110 118 Q119 118 119 107 M110 118 V124 M104 124 H116" fill="none" ${S26}/>` +
        `<rect x="104.5" y="95" width="11" height="18" rx="5.5" fill="${accent}" ${S26}/>` +
        `<path d="M107 100 H113 M107 104 H113" stroke="${light}" stroke-width="1.6" stroke-linecap="round"/>`,
    spike: accent =>
        `<rect x="95" y="99" width="30" height="25" rx="4" fill="${SKIN}" ${S26}/>` +
        `<path d="M99 117 L104 115 L108 117 L112 103 L115 116 L121 114" fill="none" stroke="${accent}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>` +
        `<circle cx="112" cy="103" r="2.6" fill="#ef4444" ${stroke(1.4)}/>`,
    pin: accent =>
        `<ellipse cx="110" cy="123" rx="9" ry="2.6" fill="${SHADE}"/>` +
        `<path d="M110 123 Q98 110 98 105 a12 12 0 0 1 24 0 Q122 110 110 123 Z" fill="${accent}" ${S26}/>` +
        `<circle cx="110" cy="105" r="4.2" fill="${SKIN}" ${S2}/>`,
    switch: accent =>
        `<rect x="95" y="104" width="30" height="16" rx="8" fill="${accent}" ${S26}/>` +
        `<circle cx="117" cy="112" r="5.6" fill="${SKIN}" ${S2}/>`,
    sync: accent =>
        `<circle cx="110" cy="111" r="13" fill="${SKIN}" ${S26}/>` +
        `<path d="M103 109 a7.5 7.5 0 0 1 13.5 -3 M117 113 a7.5 7.5 0 0 1 -13.5 3" fill="none" stroke="${accent}" stroke-width="2.6" stroke-linecap="round"/>` +
        `<path d="M117.5 101.5 L117 106.5 L112 105.5 M102.5 120.5 L103 115.5 L108 116.5" fill="none" stroke="${accent}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>`,
    function: (_accent, light) =>
        `<rect x="96" y="98" width="27" height="26" rx="5" fill="${light}" ${S26}/>` +
        `<path d="M116 103 Q109 101 109 108 V117 Q109 121 104 120 M104 109 H115" fill="none" ${S26}/>`,
    code: accent =>
        `<rect x="95" y="98" width="30" height="26" rx="4" fill="${INK}" ${S26}/>` +
        `<rect x="95" y="98" width="30" height="5" rx="2" fill="${accent}"/>` +
        `<path d="M103.5 106.5 L99 111 L103.5 116.5 M116.5 105.5 L121 111 L116.5 116.5 M112.5 103.5 L107.5 118.5" fill="none" stroke="${SKIN}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>`,
    wave: accent =>
        `<rect x="95" y="99" width="30" height="25" rx="4" fill="${SKIN}" ${S26}/>` +
        `<path d="M99 112 Q102 103 105 112 T111 112 T117 112 T121 110" fill="none" stroke="${accent}" stroke-width="2.6" stroke-linecap="round"/>` +
        `<circle cx="99" cy="112" r="1.6" fill="${INK}"/><circle cx="111" cy="112" r="1.6" fill="${INK}"/><circle cx="121" cy="110" r="1.6" fill="${INK}"/>`,
    sun: () =>
        around(`<path d="M110 96 V99.5" ${stroke(2.4)}/>`, 110, 108) +
        `<circle cx="110" cy="108" r="7.5" fill="${GOLD}" ${S2}/>` +
        `<path d="M100 124 a5 5 0 0 1 0 -10 a7 7 0 0 1 13 -2 a5 5 0 0 1 6 6 a4 4 0 0 1 -1 6 Z" fill="${SKIN}" ${S2}/>`,
    clipboard: accent =>
        `<rect x="97" y="99" width="25" height="26" rx="3" fill="#c9a26b" ${S26}/>` +
        `<rect x="100" y="104" width="19" height="18" rx="1.5" fill="${SKIN}" ${stroke(1.6)}/>` +
        `<rect x="104" y="96" width="11" height="6" rx="2" fill="#9aa3b5" ${S2}/>` +
        `<path d="M103 109 l2 2 l3 -4 M110 109 H116 M103 116 l2 2 l3 -4 M110 116 H116" fill="none" stroke="${accent}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`,
    thought: () =>
        `<path d="M103 21 a6 6 0 0 1 5 -10 a7 7 0 0 1 12 1 a6 6 0 0 1 3 10 a6 6 0 0 1 -9 4 a7 7 0 0 1 -11 -5 Z" fill="${SKIN}" ${stroke(2.4)}/>` +
        `<circle cx="104" cy="31" r="2.4" fill="${SKIN}" ${stroke(1.8)}/>` +
        `<circle cx="100.5" cy="37.5" r="1.5" fill="${SKIN}" ${stroke(1.4)}/>` +
        [107.5, 113, 118.5].map(x => `<circle cx="${x}" cy="17.5" r="1.7" fill="${INK}"/>`).join(""),
    database: (accent, light) =>
        [116, 109, 102]
            .map(
                y =>
                    `<path d="M4 ${y} v6 q10 5 20 0 v-6" fill="${accent}" ${S2}/>` +
                    `<ellipse cx="14" cy="${y}" rx="10" ry="3.4" fill="${light}" ${S2}/>`,
            )
            .join("") + '<path d="M0 124.5 H34" stroke="#cfc8a8" stroke-width="3.4" stroke-linecap="round"/>',
    bolt: () =>
        `<circle cx="111" cy="111" r="13" fill="${GOLD}" ${S26}/>` +
        `<path d="M113 102 L105 113 H111 L108.5 121 L117 109 H111 Z" fill="${INK}"/>`,
    house: () =>
        `<path d="M98 113 L110 101 L122 113 V124 H98 Z" fill="#41bdf5" ${S26}/>` +
        `<rect x="107" y="115" width="6" height="9" rx="1" fill="${SKIN}" ${stroke(1.8)}/>`,
    chart: accent =>
        `<rect x="96" y="99" width="28" height="25" rx="4" fill="${SKIN}" ${S26}/>` +
        `<path d="M101 117 L107 110 L112 114 L119 105" fill="none" stroke="${accent}" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/>`,
};

/** The names of the props there are. */
export const PROP_NAMES: readonly string[] = Object.keys(PROPS);

/** The prop named `kind` in `accent`, or nothing for a name that is not one. */
export function prop(kind: string, accent: string): string {
    return Object.hasOwn(PROPS, kind) ? PROPS[kind]!(accent, tint(accent, 0.45)) : "";
}
