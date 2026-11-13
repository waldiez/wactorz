/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * What the selection is, in words: for a topic, who publishes and reads it,
 * the fields it carries and the last payload seen on it; for an agent, what
 * it publishes, reads and serves, and what it asked; and each warning about
 * it as a sentence. Everything comes from the wiring the view was handed.
 */
import { modelLine, topicEnds, type Graph, type GraphNode } from "../../wiring/graph";
import type { Wiring, WiringAgent, WiringAsks, WiringTopic, WiringWarning } from "../../types/wiring";
import { formatRoute } from "../../router";
import { relTime } from "../overview/agentState";
import { el, labelled } from "../dom";

/** How the details name an agent: by its name, or its id while unknown. */
export type AgentNamer = (id: string) => string;

/** A namer over `wiring`'s agents. */
export function namerOf(wiring: Wiring): AgentNamer {
    const names = new Map(wiring.agents.map(a => [a.id, a.name]));
    return id => names.get(id) ?? id;
}

/** A warning, said as a sentence. */
export function warningSentence(warning: WiringWarning, name: AgentNamer): string {
    if (warning.kind === "unread") {
        return `Nobody reads ${warning.topic}: ${warning.producers.map(name).join(", ")} publishes it.`;
    }
    if (warning.kind === "no_producer") {
        return `${warning.consumers.map(name).join(", ")} reads ${warning.topic}, which nothing publishes and nothing was seen on.`;
    }
    const carried = warning.carried.length ? warning.carried.join(", ") : "no fields";
    const seen =
        warning.carried_from === "declared"
            ? `the producer of ${warning.topic} declares`
            : `messages on ${warning.topic} carry`;
    return `${name(warning.consumer)} reads ${warning.missing.join(", ")}, but ${seen} ${carried}.`;
}

function row(term: string, value: string | HTMLElement): HTMLElement {
    const line = el("div", "af-wire-detail-row");
    line.append(el("dt", "", term));
    const dd = el("dd");
    dd.append(value);
    line.append(dd);
    return line;
}

/** The agents at one end of a topic that are shown, and how many more the choices hide. */
export function endsText(shown: readonly string[], all: readonly string[]): string {
    const hidden = all.length - shown.length;
    const named = shown.join(", ");
    if (hidden <= 0) {
        return named || "none";
    }
    return named ? `${named}, and ${hidden} hidden` : `${hidden} hidden`;
}

/** Who reads a topic: the agents shown, and the dashboard where its feed or a panel does. */
export function readersText(shown: readonly string[], topic: WiringTopic): string {
    if (!topic.dashboard) {
        return endsText(shown, topic.consumers);
    }
    return topic.consumers.length
        ? `${endsText(shown, topic.consumers)}, and the dashboard`
        : "the dashboard";
}

/** What is known of the fields a topic carries, in words. */
export function fieldsText(topic: WiringTopic): string {
    if (topic.fields && Object.keys(topic.fields).length) {
        const fields = Object.keys(topic.fields).sort().join(", ");
        return topic.fields_from === "declared" ? `${fields} (declared, nothing seen yet)` : fields;
    }
    if (!topic.sampled) {
        return `unknown: main keeps no payloads from ${topic.topic.split("/", 1)[0]}/`;
    }
    return "nothing seen yet";
}

/** The last payload seen on a topic, as text: whole, or cut where it was too long. */
export function payloadText(topic: WiringTopic): string | null {
    if (topic.example_cut && topic.example_text) {
        return `${topic.example_text}… (cut)`;
    }
    if (topic.example === null || topic.example === undefined) {
        return null;
    }
    return JSON.stringify(topic.example, null, 1);
}

function topicDetail(topic: WiringTopic, ends: { publishers: string[]; readers: string[] }): HTMLElement[] {
    const facts = el("dl", "af-wire-detail-facts");
    facts.append(
        row(
            "Published by",
            topic.outside ? "outside the system" : endsText(ends.publishers, topic.producers),
        ),
        row("Read by", readersText(ends.readers, topic)),
        row("Fields", fieldsText(topic)),
    );
    if (topic.shadow) {
        facts.append(row("On trial", "what a model on trial publishes, beside the served one"));
    }
    const parts: HTMLElement[] = [facts];
    const payload = payloadText(topic);
    if (payload !== null) {
        const when = topic.seen_at === null ? "" : `, ${relTime(topic.seen_at * 1000)}`;
        parts.push(
            el("p", "af-wire-detail-label", `Last payload${when}`),
            el("pre", "af-wire-payload", payload),
        );
    }
    return parts;
}

/** What an agent asked a person, with the way to answer while any question is open. */
export function asksValue(asks: WiringAsks): string | HTMLElement {
    const where = asks.channels.length ? `, on ${asks.channels.join(", ")}` : "";
    const said = `${asks.open} open, ${asks.answered} answered${where}`;
    if (asks.open === 0) {
        return said;
    }
    const link = el("a", "", "Answer in the Inbox");
    link.href = formatRoute({ view: "inbox", path: [] });
    const value = el("span", "", `${said}. `);
    value.append(link);
    return value;
}

function agentDetail(agent: WiringAgent): HTMLElement[] {
    const facts = el("dl", "af-wire-detail-facts");
    facts.append(row("Runs on", agent.node));
    if (agent.description) {
        facts.append(row("Does", agent.description));
    }
    facts.append(
        row("Publishes", agent.publishes.join(", ") || "nothing declared"),
        row("Subscribes", agent.subscribes.join(", ") || "nothing"),
    );
    if (agent.reads_fields.length) {
        facts.append(row("Reads fields", agent.reads_fields.join(", ")));
    }
    facts.append(...agent.models.map(model => row(`Model (${model.key})`, modelLine(model))));
    if (agent.asks) {
        facts.append(row("Asked a person", asksValue(agent.asks)));
    }
    return [facts];
}

/**
 * The details of `node`, or what to do when nothing is selected. Who is named
 * is who `graph` shows: an agent the choices hide is not.
 */
export function buildDetail(node: GraphNode | null, wiring: Wiring, graph: Graph): HTMLElement {
    const panel = labelled(el("section", "af-wire-detail"), "Selected");
    if (!node) {
        panel.append(
            el(
                "p",
                "af-wire-detail-empty",
                "Select an agent or a topic to see what it publishes, reads and carries.",
            ),
        );
        return panel;
    }
    const name = namerOf(wiring);
    const kind = { agent: "Agent", topic: "Topic", source: "Outside source", operator: "Operator" }[
        node.kind
    ];
    panel.append(
        el("p", "af-wire-detail-kind", kind),
        el("h3", `af-wire-detail-title ${node.kind}`, node.label),
    );
    if (node.topic) {
        panel.append(...topicDetail(node.topic, topicEnds(graph, node.id)));
    } else if (node.agent) {
        panel.append(...agentDetail(node.agent));
    } else if (node.kind === "source") {
        panel.append(
            el("p", "", `Topics no agent publishes under ${node.label}/: ${(node.topics ?? []).join(", ")}.`),
        );
    } else {
        const asking = graph.nodes.filter(n => (n.agent?.asks?.open ?? 0) > 0).map(n => n.label);
        panel.append(el("p", "", `Waiting on a person: ${asking.join(", ") || "nobody"}.`));
    }
    if (node.warnings.length) {
        const list = el("ul", "af-wire-detail-warnings");
        for (const warning of node.warnings) {
            list.append(el("li", `af-wire-warning ${warning.kind}`, warningSentence(warning, name)));
        }
        panel.append(list);
    }
    return panel;
}
