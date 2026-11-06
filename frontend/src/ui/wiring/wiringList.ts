/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Every row is read from the graph, so it shows what the graph would: an
 * agent the choices hide is not named, nor a warning only about one.
 *
 * The wiring as a list: the warnings first, then each topic with who
 * publishes and who reads it, then the agents. What a phone shows instead of the graph, and
 * the graph's equal for whoever would rather read than look; never shown
 * beside it, so the page holds one set of controls, not two.
 */
import { shownWarnings, topicEnds, topicNode, type Graph, type GraphNode } from "../../wiring/graph";
import type { Wiring } from "../../types/wiring";
import { button, el, list, pressed } from "../dom";
import { namerOf, warningSentence } from "./wiringDetail";
import type { GraphHost } from "./wiringGraph";

/** What one topic's row says: who the graph shows publishing it, then the topic, then who reads it. */
export function flowText(graph: Graph, node: GraphNode): string {
    const topic = node.topic;
    if (!topic) {
        return node.label;
    }
    const { publishers, readers } = topicEnds(graph, node.id);
    // An end the choices hide is said to be hidden, not missing.
    const end = (shown: string[], all: string[], none: string): string =>
        shown.join(", ") || (all.length ? "hidden" : none);
    const from = topic.outside ? "outside" : end(publishers, topic.producers, "nothing");
    return `${from} → ${topic.topic} → ${end(readers, topic.consumers, "nobody")}`;
}

/** One row of the list: a button choosing `id`, found again after a redraw by `key`. */
interface Row {
    id: string;
    text: string;
    className: string;
    key: { name: "key" | "node"; value: string };
}

/** A titled list of rows, each choosing its node. */
function section(title: string, rows: readonly Row[], host: GraphHost): HTMLElement[] {
    const items = list(el("ul", "af-wire-list-items"), title);
    for (const row of rows) {
        const choose = button(`af-wire-list-row ${row.className}`.trim(), row.text);
        choose.dataset[row.key.name] = row.key.value;
        pressed(choose, host.selected() === row.id);
        choose.addEventListener("click", () => host.select(row.id));
        const item = el("li");
        item.append(choose);
        items.append(item);
    }
    return [el("h3", "af-wire-list-title", title), items];
}

function warned(node: GraphNode): string {
    return node.warnings.length ? "warned" : "";
}

/** The list of what `graph` shows of `wiring`. */
export function buildList(graph: Graph, wiring: Wiring, host: GraphHost): HTMLElement {
    const name = namerOf(wiring);
    const root = el("div", "af-wire-list");
    const warnings = shownWarnings(graph);
    if (warnings.length) {
        root.append(
            ...section(
                "Warnings",
                warnings.map(w => ({
                    id: topicNode(w.topic),
                    text: warningSentence(w, name),
                    className: `warning ${w.kind}`,
                    key: { name: "key", value: `warning:${w.kind}:${w.topic}` },
                })),
                host,
            ),
        );
    }
    const topics = graph.nodes.filter(n => n.kind === "topic");
    if (topics.length) {
        const rows = topics.map(n => ({
            id: n.id,
            text: flowText(graph, n),
            className: warned(n),
            key: { name: "node" as const, value: n.id },
        }));
        root.append(...section("Topics", rows, host));
    } else {
        root.append(el("h3", "af-wire-list-title", "Topics"), el("p", "af-wire-empty", "No topic to show."));
    }
    const agents = graph.nodes.filter(n => n.kind === "agent");
    if (agents.length) {
        const rows = agents.map(n => ({
            id: n.id,
            text: n.agent ? `${n.label} on ${n.agent.node}` : n.label,
            className: warned(n),
            key: { name: "node" as const, value: n.id },
        }));
        root.append(...section("Agents", rows, host));
    }
    return root;
}
