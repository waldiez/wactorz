/**
 * SPDX-License-Identifier: Apache-2.0
 */
/** The character's drawing: its colours, each part there is, the two crops, and the image's address. */
import { describe, expect, it } from "vitest";

import { KINDS, lookFor, type Mood } from "../../ui/avatars/cast";
import { avatarSvg, avatarUrl, drawnCount, MAX_DRAWN, type Look } from "../../ui/avatars/draw";
import { boxOf, placedBox, PROP_NAMES, prop, type Box } from "../../ui/avatars/props";
import { bright, deep, isColour, luminance, shadeOf, tint } from "../../ui/avatars/svg";

const MOODS: readonly Mood[] = ["running", "initializing", "late", "stopped", "failed"];
const BASE: Look = lookFor("anyone", { kind: "function" }, "running");

/** `svg` read as a document, so text that is not well-formed fails here. */
function parsed(svg: string): Document {
    const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
    expect(doc.querySelector("parsererror")).toBeNull();
    return doc;
}

/** How far a stroke may reach past the points a shape is drawn through. */
const STROKE_SLACK = 3;

/** Every point a path's commands pass through or bend toward, absolute and relative alike. */
function pathPoints(d: string): [number, number][] {
    const tokens = d.match(/[a-zA-Z]|-?\d*\.?\d+(?:e-?\d+)?/g) ?? [];
    const points: [number, number][] = [];
    let [x, y] = [0, 0];
    let command = "";
    let i = 0;
    const next = (): number => Number(tokens[i++]);
    while (i < tokens.length) {
        if (/[a-zA-Z]/.test(tokens[i]!)) {
            command = tokens[i++]!;
        }
        const relative = command === command.toLowerCase();
        const [ox, oy] = relative ? [x, y] : [0, 0];
        switch (command.toUpperCase()) {
            case "Z":
                continue;
            case "H":
                x = ox + next();
                break;
            case "V":
                y = oy + next();
                break;
            case "Q":
                points.push([ox + next(), oy + next()]);
                [x, y] = [ox + next(), oy + next()];
                break;
            case "C":
                points.push([ox + next(), oy + next()], [ox + next(), oy + next()]);
                [x, y] = [ox + next(), oy + next()];
                break;
            case "A":
                i += 5; // radii, rotation and flags: the arc's end is what is checked
                [x, y] = [ox + next(), oy + next()];
                break;
            default: // M, L, T
                [x, y] = [ox + next(), oy + next()];
        }
        points.push([x, y]);
    }
    return points;
}

/** The points an element of a prop's drawing reaches. A rotated part is checked by its turning point. */
function pointsOf(node: Element): [number, number][] {
    const n = (name: string): number => Number(node.getAttribute(name) ?? 0);
    const turn = node.getAttribute("transform")?.match(/rotate\(\S+ (\S+) (\S+)\)/);
    if (turn) {
        return [[Number(turn[1]), Number(turn[2])]];
    }
    switch (node.tagName) {
        case "rect":
            return [
                [n("x"), n("y")],
                [n("x") + n("width"), n("y") + n("height")],
            ];
        case "circle":
            return [
                [n("cx") - n("r"), n("cy") - n("r")],
                [n("cx") + n("r"), n("cy") + n("r")],
            ];
        case "ellipse":
            return [
                [n("cx") - n("rx"), n("cy") - n("ry")],
                [n("cx") + n("rx"), n("cy") + n("ry")],
            ];
        case "path":
            return pathPoints(node.getAttribute("d") ?? "");
        default:
            return [];
    }
}

function inside([x, y]: [number, number], [x0, y0, x1, y1]: Box): boolean {
    return (
        x >= x0 - STROKE_SLACK && x <= x1 + STROKE_SLACK && y >= y0 - STROKE_SLACK && y <= y1 + STROKE_SLACK
    );
}

describe("where a prop goes", () => {
    it("draws every prop within the box it says it is drawn in", () => {
        for (const name of PROP_NAMES) {
            const doc = parsed(`<svg xmlns="http://www.w3.org/2000/svg">${prop(name, "#3fbf5f")}</svg>`);
            const outside = [...doc.querySelectorAll("rect, circle, ellipse, path")]
                .flatMap(pointsOf)
                .filter(point => !inside(point, boxOf(name)));
            expect(outside, name).toEqual([]);
        }
    });

    it("places every prop wholly inside the round disc a character is shown on", () => {
        for (const name of PROP_NAMES) {
            const [x0, y0, x1, y1] = placedBox(name);
            for (const [x, y] of [
                [x0, y0],
                [x1, y0],
                [x0, y1],
                [x1, y1],
            ] as const) {
                expect(Math.hypot(x - 64, y - 64), name).toBeLessThanOrEqual(64);
            }
        }
    });

    it("scales the box a prop is drawn in onto its place, keeping its shape", () => {
        const drawn = prop("database", "#3fbf5f");
        const scale = Number(drawn.match(/scale\((\S+)\)/)?.[1]);
        const [x0, , x1] = boxOf("database");
        const [px0, , px1] = placedBox("database");
        expect(scale).toBeCloseTo((px1 - px0) / (x1 - x0), 2);
        expect(placedBox("thought")).not.toEqual(placedBox("clock"));
    });
});

describe("the colours of a character", () => {
    it("knows a colour from anything else", () => {
        expect(isColour("#Cb95fA")).toBe(true);
        for (const not of ["#fff", "red", "#12345g", "url(#x)", 7, null, undefined]) {
            expect(isColour(not)).toBe(false);
        }
    });

    it("mixes toward white and toward black", () => {
        expect(tint("#000000", 0.5)).toBe("#808080");
        expect(tint("#336699", 0)).toBe("#336699");
        expect(shadeOf("#ffffff", 0.5)).toBe("#808080");
        expect(shadeOf("#336699", 1)).toBe("#000000");
    });

    it("says how light a colour reads", () => {
        expect(luminance("#000000")).toBe(0);
        expect(luminance("#ffffff")).toBeCloseTo(1);
        expect(luminance("#cb95fa")).toBeGreaterThan(luminance("#1e294b"));
    });

    it("gives an accent a brighter and a deeper shade, of every hue", () => {
        for (const accent of ["#ff0000", "#00ff00", "#0000ff", "#3fbf5f", "#ffd23f", "#7c5cff", "#e0e0f0"]) {
            expect(isColour(bright(accent))).toBe(true);
            expect(isColour(deep(accent))).toBe(true);
            expect(luminance(deep(accent))).toBeLessThan(luminance(accent));
        }
        // A grey has no hue to keep: its shades are greys.
        expect(bright("#808080")).toMatch(/^#(..)\1\1$/);
        expect(deep("#808080")).toMatch(/^#(..)\1\1$/);
    });
});

describe("a character's drawing", () => {
    it("is well-formed for every ready-made character in every mood", () => {
        for (const kind of KINDS) {
            for (const mood of MOODS) {
                const doc = parsed(avatarSvg(lookFor("someone", { kind }, mood)));
                expect(doc.documentElement.getAttribute("viewBox")).toBe("0 0 128 128");
            }
        }
    });

    it("draws each choice of every part differently", () => {
        const choices: Partial<Look>[] = [
            ...(["vest", "suit", "stripes"] as const).map(outfit => ({ outfit })),
            ...(["gem", "tie", "bowtie", "none"] as const).map(neck => ({ neck })),
            ...(["round", "square", "shades", "monocle", "none"] as const).map(specs => ({ specs })),
            ...(["smile", "blush", "beard", "mustache", "open"] as const).map(face => ({ face })),
            ...(["awake", "happy", "thinking", "busy", "asleep", "crashed"] as const).map(look => ({ look })),
            ...(["", "gradcap", "hardhat", "tophat", "captain", "beanie", "headset", "refcap"] as const).map(
                top => ({
                    top,
                }),
            ),
            ...([0, 1, 2, "left", "right"] as const).map(ant => ({ ant })),
        ];
        const drawn = new Map<string, string>();
        for (const choice of choices) {
            const svg = avatarSvg({ ...BASE, ...choice });
            parsed(svg);
            drawn.set(JSON.stringify(choice), svg);
        }
        // Every group has the base's own choice in it once; the rest all differ from it and from each other.
        expect(new Set(drawn.values()).size).toBe(choices.length - 6);
    });

    it("draws every prop there is, and nothing for a name that is not one", () => {
        expect(PROP_NAMES.length).toBeGreaterThan(10);
        for (const name of PROP_NAMES) {
            const drawn = prop(name, "#3fbf5f");
            expect(drawn).not.toBe("");
            parsed(`<svg xmlns="http://www.w3.org/2000/svg">${drawn}</svg>`);
        }
        expect(prop("toString", "#3fbf5f")).toBe("");
        expect(prop("", "#3fbf5f")).toBe("");
    });

    it("hides the antennae and the tuft under a hat", () => {
        const bare = avatarSvg({ ...BASE, top: "", ant: 2 });
        const hatted = avatarSvg({ ...BASE, top: "beanie", ant: 2 });
        expect(bare).toContain("M48 30 L38 12");
        expect(hatted).not.toContain("M48 30 L38 12");
    });

    it("shows head and shoulders, without the prop, at the head crop", () => {
        const full = avatarSvg({ ...BASE, prop: "clock" });
        const head = avatarSvg({ ...BASE, prop: "clock" }, "head");
        expect(parsed(head).documentElement.getAttribute("viewBox")).toBe("6 0 116 116");
        expect(full.length).toBeGreaterThan(head.length);
        expect(head).toBe(avatarSvg({ ...BASE, prop: "" }, "head"));
    });

    it("writes no number longer than it is drawn", () => {
        expect(avatarSvg(BASE)).not.toMatch(/\d\.\d{3,}/);
    });

    it("keeps no more drawn characters than its bound, dropping the one used longest ago", () => {
        const kept = avatarUrl(BASE);
        const colour = (i: number): string => `#${i.toString(16).padStart(6, "0")}`;
        for (let i = 0; i < MAX_DRAWN * 2; i++) {
            avatarUrl({ ...BASE, accent: colour(i) });
            avatarUrl(BASE); // used all along, so never the one dropped
        }
        expect(drawnCount()).toBe(MAX_DRAWN);
        expect(avatarUrl(BASE)).toBe(kept);
    });

    it("gives the same address for the same look, and another for another", () => {
        const url = avatarUrl(BASE);
        expect(url.startsWith("data:image/svg+xml,")).toBe(true);
        expect(decodeURIComponent(url.slice("data:image/svg+xml,".length))).toBe(avatarSvg(BASE));
        expect(avatarUrl({ ...BASE })).toBe(url);
        expect(avatarUrl(BASE, "head")).not.toBe(url);
        expect(avatarUrl({ ...BASE, look: "asleep" })).not.toBe(url);
    });
});
