/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Where the layout puts each node: a column after what feeds it, cycles
 * broken the same way every time with every line still drawn, the operator
 * last, and the same graph always laid out the same; and where each line
 * meets a box: at a handle of its own, spread along the side.
 */
import { describe, expect, it } from "vitest";

import {
    agentNode,
    boxLines,
    buildGraph,
    DEFAULT_CHOICES,
    OPERATOR,
    topicNode,
    type Graph,
} from "../../wiring/graph";
import {
    backEdges,
    columns,
    handleOffset,
    HANDLE_GAP,
    HANDLE_INSET,
    heightOf,
    layout,
    reroute,
    type Layout,
    type Placed,
} from "../../wiring/layout";
import { agent, topic, wiring } from "./fixtures";

const graphOf = (w = wiring()): Graph => buildGraph(w, DEFAULT_CHOICES);

const boxOf = (drawing: Layout, id: string): Placed => drawing.placed.find(p => p.node.id === id)!;

/** An agent publishing `count` topics, each read by one agent of its own. */
function fan(count: number): Graph {
    const names = Array.from({ length: count }, (_, i) => `out/${String(i).padStart(2, "0")}`);
    return graphOf({
        agents: [
            agent("hub", { publishes: names }),
            ...names.map((name, i) => agent(`reader-${i}`, { subscribes: [name] })),
        ],
        topics: names.map((name, i) => topic(name, { producers: ["hub"], consumers: [`reader-${i}`] })),
        sources: [],
        warnings: [],
    });
}

describe("the layout", () => {
    it("puts a topic after its producer and an agent after what it reads", () => {
        const column = columns(graphOf());

        expect(column.get(topicNode("air/kitchen/score"))).toBeGreaterThan(column.get(agentNode("detect"))!);
        expect(column.get(agentNode("watch"))).toBeGreaterThan(column.get(topicNode("air/kitchen/score"))!);
        expect(column.get(topicNode("plans/today"))).toBe(0);
    });

    it("puts the operator in the last column", () => {
        const column = columns(graphOf());
        const last = Math.max(...column.values());

        expect(column.get(OPERATOR)).toBe(last);
    });

    it("breaks a cycle on a reading line, keeping a topic after its producer, and draws every line", () => {
        const loop = {
            agents: [agent("a", { publishes: ["x"] }), agent("b", { publishes: ["y"] })],
            topics: [
                topic("x", { producers: ["a"], consumers: ["b"] }),
                topic("y", { producers: ["b"], consumers: ["a"] }),
            ],
            sources: [],
            warnings: [],
        };
        const graph = graphOf(loop);

        expect([...backEdges(graph.nodes, graph.edges)]).toEqual([`${topicNode("y")}>${agentNode("a")}`]);
        const placed = layout(graph);
        expect(placed.routed).toHaveLength(graph.edges.length);
        expect(placed.routed.filter(r => r.backwards).map(r => r.edge.to)).toEqual([agentNode("a")]);
    });

    it("lays out an agent reading its own topic with the topic after it", () => {
        const self = {
            agents: [agent("loop", { publishes: ["events/x"], subscribes: ["events/#"] })],
            topics: [topic("events/x", { producers: ["loop"], consumers: ["loop"] })],
            sources: [],
            warnings: [],
        };
        const column = columns(graphOf(self));

        expect(column.get(topicNode("events/x"))).toBe(1);
        expect(column.get(agentNode("loop"))).toBe(0);
    });

    it("places the same graph the same way every time, with no two boxes overlapping", () => {
        const first = layout(graphOf());
        const second = layout(graphOf());

        expect(second).toEqual(first);
        for (const a of first.placed) {
            for (const b of first.placed) {
                if (a !== b && a.column === b.column) {
                    expect(a.y + a.h <= b.y || b.y + b.h <= a.y).toBe(true);
                }
            }
        }
        expect(first.width).toBeGreaterThan(0);
        expect(first.height).toBeGreaterThan(0);
    });

    it("gives each machine a band of its own when grouped", () => {
        const grouped = layout(graphOf(), true);

        expect(grouped.lanes.map(l => l.machine)).toEqual(["", "edge", "main"]);
        const edge = grouped.lanes.find(l => l.machine === "edge")!;
        const detect = grouped.placed.find(p => p.node.id === agentNode("detect"))!;
        expect(detect.y).toBeGreaterThanOrEqual(edge.y);
        expect(detect.y + detect.h).toBeLessThanOrEqual(edge.y + edge.h);
    });

    it("makes a box as tall as its lines, so tall boxes in one column never overlap", () => {
        const crowded = wiring();
        crowded.agents[0]!.models = [
            { key: "a", name: "first-model", active: 1, shadow: 2 },
            { key: "b", name: "second-model", active: 3, shadow: null },
            { key: "c", name: "third-model", active: null, shadow: null },
        ];
        crowded.agents[2]!.asks = { open: 2, answered: 0, channels: [] };
        const graph = graphOf(crowded);
        const placed = layout(graph);
        const detect = placed.placed.find(p => p.node.id === agentNode("detect"))!;
        const plain = placed.placed.find(p => p.node.id === agentNode("planner"))!;

        expect(detect.h).toBe(heightOf(detect.node));
        expect(boxLines(detect.node)).toHaveLength(5);
        expect(detect.h).toBeGreaterThan(plain.h);
        for (const a of placed.placed) {
            for (const b of placed.placed) {
                if (a !== b && a.column === b.column) {
                    expect(a.y + a.h <= b.y || b.y + b.h <= a.y).toBe(true);
                }
            }
        }
        // A box's one line in meets the middle of its side.
        const into = placed.routed.filter(r => r.edge.to === agentNode("detect"));
        expect(into).toHaveLength(1);
        expect(into[0]).toMatchObject({ y2: detect.y + detect.h / 2, x2: detect.x });
    });

    it("lays out an empty graph without failing", () => {
        expect(layout({ nodes: [], edges: [] }).placed).toEqual([]);
    });
});

describe("the handles", () => {
    it("spread a box's lines along its side, in the order of their other ends, so none cross", () => {
        const drawing = layout(fan(3));
        const hub = boxOf(drawing, agentNode("hub"));
        const out = drawing.routed.filter(r => r.edge.from === agentNode("hub"));

        expect(out).toHaveLength(3);
        // Each line leaves at a place of its own, centred on the side as a set.
        const leaves = out.map(r => r.y1).sort((a, b) => a - b);
        expect(leaves).toEqual([
            hub.y + hub.h / 2 - HANDLE_GAP,
            hub.y + hub.h / 2,
            hub.y + hub.h / 2 + HANDLE_GAP,
        ]);
        // The line to the higher box leaves higher.
        const byTarget = [...out].sort((a, b) => boxOf(drawing, a.edge.to).y - boxOf(drawing, b.edge.to).y);
        expect(byTarget.map(r => r.y1)).toEqual(leaves);
        expect(out.every(r => r.x1 === hub.x + hub.w)).toBe(true);
    });

    it("pack closer on a side with many lines, never past its ends", () => {
        const drawing = layout(fan(12));
        const hub = boxOf(drawing, agentNode("hub"));
        const leaves = drawing.routed.filter(r => r.edge.from === agentNode("hub")).map(r => r.y1);

        expect(new Set(leaves).size).toBe(12);
        expect(Math.min(...leaves)).toBeCloseTo(hub.y + HANDLE_INSET);
        expect(Math.max(...leaves)).toBeCloseTo(hub.y + hub.h - HANDLE_INSET);
    });

    it("sit in the middle alone, a gap apart in a few, and at one place on a side with no room", () => {
        expect(handleOffset(40, 0, 1)).toBe(20);
        expect([handleOffset(40, 0, 2), handleOffset(40, 1, 2)]).toEqual([14, 26]);
        expect([handleOffset(8, 0, 3), handleOffset(8, 2, 3)]).toEqual([4, 4]);
    });

    it("arrive in the order of where their lines come from", () => {
        const drawing = layout(graphOf());
        const kitchen = topicNode("sensors/air/kitchen");
        const readers = drawing.routed.filter(r => r.edge.from === kitchen);

        expect(readers).toHaveLength(2);
        const [upper, lower] = [...readers].sort(
            (a, b) => boxOf(drawing, a.edge.to).y - boxOf(drawing, b.edge.to).y,
        );
        expect(upper!.y1).toBeLessThan(lower!.y1);
    });

    it("sit on the curve of a rounded corner, not off it", () => {
        const drawing = layout(graphOf());
        const kitchen = boxOf(drawing, topicNode("sensors/air/kitchen"));
        const off = drawing.routed.filter(r => r.edge.from === kitchen.node.id);

        // Off the middle of a topic's round end, the border is further in.
        for (const r of off) {
            expect(r.x1).toBeLessThan(kitchen.x + kitchen.w);
            expect(r.x1).toBeGreaterThan(kitchen.x + kitchen.w - 4);
        }
        // An agent's side is straight where its handles are.
        const detect = boxOf(drawing, agentNode("detect"));
        const out = drawing.routed.filter(r => r.edge.from === detect.node.id);
        expect(out.length).toBeGreaterThan(1);
        expect(out.every(r => r.x1 === detect.x + detect.w)).toBe(true);
    });

    it("leave out a line with an end that is not on the graph", () => {
        const graph = fan(2);
        graph.edges.push({ ...graph.edges[0]!, to: topicNode("nowhere") });

        const drawing = layout(graph);

        expect(drawing.routed).toHaveLength(graph.edges.length - 1);
        expect(drawing.routed.some(r => r.edge.to === topicNode("nowhere"))).toBe(false);
        // The line left out takes no place on the side it would have left.
        const hub = boxOf(drawing, agentNode("hub"));
        const leaves = drawing.routed.filter(r => r.edge.from === hub.node.id).map(r => r.y1);
        expect(leaves.sort((a, b) => a - b)).toEqual([
            hub.y + hub.h / 2 - HANDLE_GAP / 2,
            hub.y + hub.h / 2 + HANDLE_GAP / 2,
        ]);
    });

    it("are put in order again when a box is moved past another", () => {
        const drawing = layout(fan(2));
        const hub = agentNode("hub");
        const first = boxOf(drawing, topicNode("out/00"));
        const leaves = (routed: Layout["routed"], to: string): number =>
            routed.find(r => r.edge.from === hub && r.edge.to === to)!.y1;
        expect(leaves(drawing.routed, first.node.id)).toBeLessThan(
            leaves(drawing.routed, topicNode("out/01")),
        );

        first.y += 500;

        const again = reroute(drawing);
        expect(again).toHaveLength(drawing.routed.length);
        expect(leaves(again, first.node.id)).toBeGreaterThan(leaves(again, topicNode("out/01")));
    });
});
