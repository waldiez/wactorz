/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Where the layout puts each node: a column after what feeds it, cycles
 * broken the same way every time with every line still drawn, the operator
 * last, and the same graph always laid out the same.
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
import { backEdges, columns, heightOf, layout } from "../../wiring/layout";
import { agent, topic, wiring } from "./fixtures";

const graphOf = (w = wiring()): Graph => buildGraph(w, DEFAULT_CHOICES);

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
        // Each line ends at the middle of the side of the box it meets.
        for (const r of placed.routed.filter(r => r.edge.to === agentNode("detect"))) {
            expect(r.y2).toBe(detect.y + detect.h / 2);
            expect(r.x2).toBe(detect.x);
        }
    });

    it("lays out an empty graph without failing", () => {
        expect(layout({ nodes: [], edges: [] }).placed).toEqual([]);
    });
});
