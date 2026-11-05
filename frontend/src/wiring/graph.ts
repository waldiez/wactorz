/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The wiring as a graph to draw: which nodes and lines are visible, from what
 * the server said and what the person chose to see. No layout and no DOM: the
 * layout (`layout.ts`) places what this returns, and the view draws it.
 *
 * What a choice hides:
 *
 * - **System agents** (`isInfrastructure`) go with their lines. A topic stays
 *   while a visible agent publishes or reads it; one left with no visible end
 *   goes too.
 * - **Outside sources** hide the topics fed from outside and the boxes that
 *   group them by root, and the lines to the agents that read them.
 * - **A warning kind** shows only the topics with a warning of that kind, with
 *   every agent and source joined to them, whatever else is hidden, so the
 *   problem can be read in its context.
 *
 * Warning counts are of the whole wiring, never of what is shown. A warning
 * about only hidden agents is left off the topic it is about, and so off the
 * list and the details.
 */
import type { Wiring, WiringAgent, WiringTopic, WiringWarning } from "../types/wiring";

/** The kinds of warning, as the server names them. */
export type WarningKind = WiringWarning["kind"];

/** What the person chose to see. */
export interface GraphChoices {
    /** Whether the system's own agents are drawn. */
    system: boolean;
    /** Whether the topics fed from outside, and their sources, are drawn. */
    outside: boolean;
    /** Only the topics with a warning of this kind, in their context; null for all. */
    focus: WarningKind | null;
}

/** What a person sees first. */
export const DEFAULT_CHOICES: GraphChoices = { system: false, outside: true, focus: null };

/** The agents that are the system's own machinery, by name: not part of what an application wires. */
export const INFRASTRUCTURE = new Set(["main", "monitor", "installer", "catalog", "io-gateway"]);

/** Whether `agent` is one of the system's own. */
export function isInfrastructure(agent: Pick<WiringAgent, "name">): boolean {
    return INFRASTRUCTURE.has(agent.name);
}

/** What a node stands for. */
export type NodeKind = "source" | "topic" | "agent" | "operator";

/** One box on the graph. */
export interface GraphNode {
    /** Unique within the graph, and the same across refreshes for the same thing. */
    id: string;
    kind: NodeKind;
    /** What the box says first. */
    label: string;
    /** The machine an agent runs on, or a topic's producer's; "" for anything else. */
    machine: string;
    /** The agent, for an agent node. */
    agent?: WiringAgent;
    /** The topic, for a topic node. */
    topic?: WiringTopic;
    /** The topics a source groups, for a source node. */
    topics?: string[];
    /** The warnings about this topic, or this agent's reading. */
    warnings: WiringWarning[];
}

/** What a line means. */
export type EdgeKind = "publishes" | "reads" | "feeds" | "asks";

/** One line on the graph, from one node to another. */
export interface GraphEdge {
    from: string;
    to: string;
    kind: EdgeKind;
    /** Set for a reader missing fields its topic does not carry. */
    problem: boolean;
    /** What the line says, when it says something: the channels an agent asked on. */
    label: string;
    /** Set for what a model on trial publishes. */
    shadow: boolean;
}

/** What is drawn. */
export interface Graph {
    nodes: GraphNode[];
    edges: GraphEdge[];
}

/** The id of the node for an agent, a topic, a source and the operator. */
export const agentNode = (id: string): string => `agent:${id}`;
export const topicNode = (topic: string): string => `topic:${topic}`;
export const sourceNode = (root: string): string => `source:${root}`;
export const OPERATOR = "operator";

function rootOf(topic: string): string {
    return topic.split("/", 1)[0] ?? topic;
}

/** The warnings about what `graph` shows, once each, in the order of its topics. */
export function shownWarnings(graph: Graph): WiringWarning[] {
    return [...new Set(graph.nodes.filter(n => n.kind === "topic").flatMap(n => n.warnings))];
}

/** The agents, by name, that `graph`'s lines join to the topic node `id`: who publishes it, and who reads it. */
export function topicEnds(graph: Graph, id: string): { publishers: string[]; readers: string[] } {
    const label = new Map(graph.nodes.map(n => [n.id, n.label]));
    const named = (ids: string[]): string[] => ids.map(i => label.get(i) ?? i);
    return {
        publishers: named(graph.edges.filter(e => e.to === id && e.kind === "publishes").map(e => e.from)),
        readers: named(graph.edges.filter(e => e.from === id && e.kind === "reads").map(e => e.to)),
    };
}

/** How many warnings of each kind the whole wiring has. */
export function warningCounts(wiring: Wiring): Record<WarningKind, number> {
    const counts: Record<WarningKind, number> = { field_mismatch: 0, unread: 0, no_producer: 0 };
    for (const warning of wiring.warnings) {
        counts[warning.kind] += 1;
    }
    return counts;
}

/** The topics, by name, that `choices` keeps on the graph, before agents are counted. */
function keptTopics(wiring: Wiring, choices: GraphChoices): WiringTopic[] {
    if (choices.focus !== null) {
        const flagged = new Set(wiring.warnings.filter(w => w.kind === choices.focus).map(w => w.topic));
        return wiring.topics.filter(t => flagged.has(t.topic));
    }
    return wiring.topics.filter(t => choices.outside || !t.outside);
}

/** A line with nothing special about it. */
function line(from: string, to: string, kind: EdgeKind, more: Partial<GraphEdge> = {}): GraphEdge {
    return { from, to, kind, problem: false, label: "", shadow: false, ...more };
}

/** Builds one graph: the visible topics first, then their agents, sources and the operator. */
class GraphBuilder {
    readonly nodes: GraphNode[] = [];
    readonly edges: GraphEdge[] = [];
    private readonly _agents: Map<string, WiringAgent>;
    private readonly _shown = new Set<string>();

    constructor(
        private readonly _wiring: Wiring,
        private readonly _choices: GraphChoices,
    ) {
        this._agents = new Map(_wiring.agents.map(a => [a.id, a]));
    }

    /** Whether the agent `id` is drawn: always in a warning's context, else unless hidden as the system's own. */
    shows = (id: string): boolean => {
        const agent = this._agents.get(id);
        return (
            agent !== undefined &&
            (this._choices.focus !== null || this._choices.system || !isInfrastructure(agent))
        );
    };

    /** The topics drawn: those the choices keep that a drawn agent publishes or reads. */
    topics(): WiringTopic[] {
        return keptTopics(this._wiring, this._choices)
            .filter(t => t.producers.some(this.shows) || t.consumers.some(this.shows))
            .sort((a, b) => a.topic.localeCompare(b.topic));
    }

    addAgent(id: string): void {
        const agent = this._agents.get(id);
        if (!agent || this._shown.has(id)) {
            return;
        }
        this._shown.add(id);
        const warnings = this._wiring.warnings.filter(w => w.kind === "field_mismatch" && w.consumer === id);
        this.nodes.push({
            id: agentNode(id),
            kind: "agent",
            label: agent.name,
            machine: agent.node,
            agent,
            warnings,
        });
    }

    addTopic(topic: WiringTopic): void {
        const producers = topic.producers.filter(this.shows);
        const consumers = topic.consumers.filter(this.shows);
        const machine = this._agents.get(producers[0] ?? consumers[0] ?? "")?.node ?? "";
        this.nodes.push({
            id: topicNode(topic.topic),
            kind: "topic",
            label: topic.topic,
            machine: topic.outside ? "" : machine,
            topic,
            warnings: this._wiring.warnings.filter(w => w.topic === topic.topic && this._involvesShown(w)),
        });
        for (const id of producers) {
            this.addAgent(id);
            this.edges.push(
                line(agentNode(id), topicNode(topic.topic), "publishes", { shadow: topic.shadow }),
            );
        }
        for (const id of consumers) {
            this.addAgent(id);
            const missing = this._missing(topic.topic, id);
            // What is missing is said on the reader's box, once, not along every line into it.
            this.edges.push(
                line(topicNode(topic.topic), agentNode(id), "reads", {
                    problem: missing.length > 0,
                    shadow: topic.shadow,
                }),
            );
        }
    }

    /** One box per root for the topics fed from outside, each feeding its topics. */
    addSources(topics: readonly WiringTopic[]): void {
        const byRoot = new Map<string, string[]>();
        for (const topic of topics.filter(t => t.outside)) {
            const root = rootOf(topic.topic);
            byRoot.set(root, [...(byRoot.get(root) ?? []), topic.topic]);
        }
        for (const [root, names] of [...byRoot].sort(([a], [b]) => a.localeCompare(b))) {
            this.nodes.push({
                id: sourceNode(root),
                kind: "source",
                label: root,
                machine: "",
                topics: names,
                warnings: [],
            });
            for (const name of names) {
                this.edges.push(line(sourceNode(root), topicNode(name), "feeds"));
            }
        }
    }

    /** The operator, with a line from each drawn agent waiting on an answer. */
    addOperator(): void {
        const asking = this.nodes.filter(n => n.kind === "agent" && (n.agent?.asks?.open ?? 0) > 0);
        if (!asking.length) {
            return;
        }
        this.nodes.push({ id: OPERATOR, kind: "operator", label: "Operator", machine: "", warnings: [] });
        for (const node of asking) {
            this.edges.push(
                line(node.id, OPERATOR, "asks", { label: (node.agent?.asks?.channels ?? []).join(", ") }),
            );
        }
    }

    /** Whether a warning is about an agent that is drawn: one about the hidden is hidden with them. */
    private _involvesShown(warning: WiringWarning): boolean {
        if (warning.kind === "unread") {
            return warning.producers.some(this.shows);
        }
        if (warning.kind === "no_producer") {
            return warning.consumers.some(this.shows);
        }
        return this.shows(warning.consumer);
    }

    private _missing(topic: string, consumer: string): string[] {
        return this._wiring.warnings.flatMap(w =>
            w.kind === "field_mismatch" && w.topic === topic && w.consumer === consumer ? w.missing : [],
        );
    }
}

/**
 * The nodes and lines `choices` shows of `wiring`, in a stable order: the same
 * wiring and choices always give the same graph.
 *
 * Only the asking agents waiting on an answer are joined to the operator; what
 * an agent asked before is in its details, not on the graph.
 */
export function buildGraph(wiring: Wiring, choices: GraphChoices): Graph {
    const builder = new GraphBuilder(wiring, choices);
    const topics = builder.topics();
    for (const topic of topics) {
        builder.addTopic(topic);
    }
    // An agent with no visible topic is still part of the system, unless hidden;
    // with a warning in view, only what joins the warnings is drawn.
    if (choices.focus === null) {
        for (const agent of wiring.agents.filter(a => builder.shows(a.id))) {
            builder.addAgent(agent.id);
        }
    }
    builder.addSources(topics);
    builder.addOperator();
    return { nodes: builder.nodes, edges: builder.edges };
}

/** How a model's pointers read on its agent's box: `air-quality: active v1 · shadow v2`. */
export function modelLine(model: { name: string; active: number | null; shadow: number | null }): string {
    const active = model.active === null ? "active not set" : `active v${model.active}`;
    return model.shadow === null
        ? `${model.name}: ${active}`
        : `${model.name}: ${active} · shadow v${model.shadow}`;
}

/** What an agent's box says: its name, where it runs, what it serves, what it misses and waits on. */
function agentLines(node: GraphNode, agent: WiringAgent): string[] {
    const lines = [agent.name, `on ${agent.node}`, ...agent.models.map(modelLine)];
    const missing = [...new Set(node.warnings.flatMap(w => (w.kind === "field_mismatch" ? w.missing : [])))];
    if (missing.length) {
        lines.push(`missing: ${missing.join(", ")}`);
    }
    const open = agent.asks?.open ?? 0;
    if (open > 0) {
        lines.push(`${open} open question${open === 1 ? "" : "s"}`);
    }
    return lines;
}

/** What a topic's box says: its name, and whether it is on trial or nobody reads it. */
function topicLines(node: GraphNode, topic: WiringTopic): string[] {
    if (topic.shadow) {
        return [node.label, "on trial"];
    }
    if (topic.producers.length && !topic.consumers.length) {
        return [node.label, "nobody reads this"];
    }
    return [node.label];
}

/** What a node's box says, line by line: its name first. The layout sizes the box by it. */
export function boxLines(node: GraphNode): string[] {
    if (node.agent) {
        return agentLines(node, node.agent);
    }
    if (node.topic) {
        return topicLines(node, node.topic);
    }
    if (node.kind === "source") {
        const count = node.topics?.length ?? 0;
        return [node.label, `outside · ${count} topic${count === 1 ? "" : "s"}`];
    }
    return [node.label, "asks a person"];
}
