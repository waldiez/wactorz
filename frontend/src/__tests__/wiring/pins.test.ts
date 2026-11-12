/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Boxes moved by hand: the layout keeps a pinned box where it was put and
 * runs the lines to it, the drawing grows to hold it, and the pins are kept
 * in this browser for each grouping, whatever junk storage holds.
 */
import { afterEach, describe, expect, it } from "vitest";

import { safeStorage } from "../../safeStorage";
import { PINS_KEY, WiringPins, parsePins } from "../../ui/wiring/wiringPins";
import { agentNode, buildGraph, DEFAULT_CHOICES, topicNode } from "../../wiring/graph";
import { layout, MARGIN, reroute } from "../../wiring/layout";
import { wiring } from "./fixtures";

const graph = () => buildGraph(wiring(), DEFAULT_CHOICES);

afterEach(() => localStorage.clear());

describe("a box moved by hand", () => {
    it("stands where it was put, with its lines run to it", () => {
        const placed = layout(graph(), false, new Map([[agentNode("watch"), { x: 40, y: 900 }]]));
        const watch = placed.placed.find(p => p.node.id === agentNode("watch"))!;

        expect([watch.x, watch.y, watch.pinned]).toEqual([40, 900, true]);
        expect(placed.placed.filter(p => p.pinned)).toHaveLength(1);
        const into = placed.routed.find(r => r.edge.to === agentNode("watch"))!;
        expect([into.x2, into.y2]).toEqual([40, 900 + watch.h / 2]);
        // Now left of the topic it reads: the line runs back, as a loop.
        expect(into.backwards).toBe(true);
        // The drawing grows to hold it.
        expect(placed.height).toBeGreaterThanOrEqual(900 + watch.h);
    });

    it("may be put left of and above the drawing's own corner, which then begins there", () => {
        const plain = layout(graph());
        expect([plain.left, plain.top]).toEqual([0, 0]);

        const placed = layout(graph(), false, new Map([[topicNode("air/alarms"), { x: -50, y: -9 }]]));

        expect(placed.placed.find(p => p.node.id === topicNode("air/alarms"))).toMatchObject({
            x: -50,
            y: -9,
        });
        // With the margin round it, as on every other side.
        expect([placed.left, placed.top]).toEqual([-50 - MARGIN, -9 - MARGIN]);
        expect([placed.width, placed.height]).toEqual([plain.width, plain.height]);
    });

    it("takes its lines with it when it moves again", () => {
        const placed = layout(graph());
        const detect = placed.placed.find(p => p.node.id === agentNode("detect"))!;

        detect.y += 300;

        const out = reroute(placed).filter(r => r.edge.from === agentNode("detect"));
        expect(out.length).toBeGreaterThan(1);
        for (const r of out) {
            expect(r.y1).toBeGreaterThan(detect.y);
            expect(r.y1).toBeLessThan(detect.y + detect.h);
        }
    });
});

describe("the pins kept", () => {
    it("are kept for each grouping, in this browser, and cleared together", () => {
        const pins = new WiringPins();
        pins.set(false, "agent:watch", { x: 10.4, y: 20.6 });
        pins.set(true, "agent:detect", { x: 1, y: 2 });

        const again = new WiringPins();
        expect(again.of(false)).toEqual(new Map([["agent:watch", { x: 10, y: 21 }]]));
        expect(again.any(true)).toBe(true);
        again.clear(false);
        expect(new WiringPins().any(false)).toBe(false);
        expect(new WiringPins().any(true)).toBe(true);
    });

    it("leave out whatever storage holds that is not a pin", () => {
        safeStorage.set(PINS_KEY, "not json");
        expect(new WiringPins().any(false)).toBe(false);

        const parsed = parsePins(
            JSON.stringify({
                none: { a: { x: 1, y: 2 }, b: { x: "1", y: 2 }, c: null, d: { x: Infinity, y: 1 } },
                machine: 3,
            }),
        );
        expect(parsed).toEqual({ none: { a: { x: 1, y: 2 } }, machine: {} });
        expect(parsePins("[1]")).toEqual({ none: {}, machine: {} });
        expect(parsePins("null")).toEqual({ none: {}, machine: {} });
    });
});
