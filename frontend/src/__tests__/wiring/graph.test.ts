/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * What the graph shows of the wiring, as the person's choices say: the
 * system's own hidden at first, outside sources shown or not, and a warning
 * kind shown in its context. Counts are always of the whole wiring.
 */
import { describe, expect, it } from "vitest";

import {
    agentNode,
    buildGraph,
    DEFAULT_CHOICES,
    isInfrastructure,
    OPERATOR,
    sourceNode,
    topicNode,
    warningCounts,
    type GraphChoices,
} from "../../wiring/graph";
import { agent, topic, wiring } from "./fixtures";

const ids = (choices: Partial<GraphChoices> = {}) =>
    buildGraph(wiring(), { ...DEFAULT_CHOICES, ...choices }).nodes.map(n => n.id);

describe("the graph", () => {
    it("hides the system's own agents at first, with the topics only they touch", () => {
        const shown = ids();

        expect(shown).not.toContain(agentNode("main"));
        expect(shown).not.toContain(topicNode("homeassistant/#"));
        expect(shown).toContain(agentNode("detect"));
        expect(ids({ system: true })).toContain(agentNode("main"));
        expect(isInfrastructure({ name: "catalog" })).toBe(true);
        expect(isInfrastructure({ name: "home-assistant-state-bridge" })).toBe(false);
    });

    it("joins each topic to who publishes and reads it, and a source to what it feeds", () => {
        const graph = buildGraph(wiring(), DEFAULT_CHOICES);
        const edge = (from: string, to: string) => graph.edges.find(e => e.from === from && e.to === to);

        expect(edge(agentNode("detect"), topicNode("air/kitchen/score"))?.kind).toBe("publishes");
        expect(edge(topicNode("air/kitchen/score"), agentNode("watch"))?.kind).toBe("reads");
        expect(edge(sourceNode("sensors"), topicNode("sensors/air/kitchen"))?.kind).toBe("feeds");
        expect(edge(agentNode("detect"), topicNode("shadow/air/kitchen/score"))?.shadow).toBe(true);
    });

    it("marks a reader missing fields as a problem, saying which", () => {
        const graph = buildGraph(wiring(), DEFAULT_CHOICES);
        const read = graph.edges.find(
            e => e.from === topicNode("sensors/air/kitchen") && e.to === agentNode("comfort"),
        );

        expect(read).toMatchObject({ problem: true, label: "" });
        expect(graph.nodes.find(n => n.id === agentNode("comfort"))?.warnings).toHaveLength(1);
    });

    it("joins only an agent waiting on an answer to the operator, saying where it asked", () => {
        const graph = buildGraph(wiring(), DEFAULT_CHOICES);

        expect(graph.edges.filter(e => e.to === OPERATOR)).toEqual([
            expect.objectContaining({ from: agentNode("watch"), kind: "asks", label: "dashboard, telegram" }),
        ]);
        const answered = wiring();
        answered.agents[1]!.asks = { open: 0, answered: 3, channels: ["dashboard"] };
        expect(buildGraph(answered, DEFAULT_CHOICES).nodes.map(n => n.id)).not.toContain(OPERATOR);
    });

    it("hides what comes from outside, and the lines to it, when asked", () => {
        const graph = buildGraph(wiring(), { ...DEFAULT_CHOICES, outside: false });

        expect(graph.nodes.map(n => n.id)).not.toContain(topicNode("sensors/air/kitchen"));
        expect(graph.nodes.map(n => n.id)).not.toContain(sourceNode("sensors"));
        // The agents that read it are still there.
        expect(graph.nodes.map(n => n.id)).toContain(agentNode("comfort"));
    });

    it("shows a warning kind with everything joined to it, whatever is hidden", () => {
        const shown = ids({ focus: "field_mismatch", outside: false });

        expect(shown.sort()).toEqual(
            [
                topicNode("sensors/air/kitchen"),
                agentNode("detect"),
                agentNode("comfort"),
                sourceNode("sensors"),
            ].sort(),
        );
    });

    it("counts warnings over the whole wiring, whatever is shown", () => {
        expect(warningCounts(wiring())).toEqual({ unread: 1, field_mismatch: 1, no_producer: 1 });
    });

    it("keeps an agent with no topic, and gives the same graph for the same wiring", () => {
        const lonely = { ...wiring(), agents: [...wiring().agents, agent("idle")] };

        expect(buildGraph(lonely, DEFAULT_CHOICES).nodes.map(n => n.id)).toContain(agentNode("idle"));
        expect(buildGraph(wiring(), DEFAULT_CHOICES)).toEqual(buildGraph(wiring(), DEFAULT_CHOICES));
    });

    it("draws an agent reading its own topic as one of its readers", () => {
        const loop = {
            agents: [agent("loop", { publishes: ["events/x"], subscribes: ["events/#"] })],
            topics: [topic("events/x", { producers: ["loop"], consumers: ["loop"] })],
            sources: [],
            warnings: [],
        };

        const graph = buildGraph(loop, DEFAULT_CHOICES);

        expect(graph.edges.map(e => e.kind).sort()).toEqual(["publishes", "reads"]);
    });
});
